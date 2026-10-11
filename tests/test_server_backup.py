from __future__ import annotations

import asyncio
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import ExitStack
import uuid

from app.backup.maintenance import MaintenanceCoordinator
from app.backup.models import BackupError, BackupLimits
from backup_helpers import make_business_fixture, make_archive, business_digest, open_fixture_server


class BackupAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="task6-api-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.layout = make_business_fixture(self.root / "local")
        incoming = make_business_fixture(self.root / "incoming")
        self.zip = make_archive(incoming, self.root / "upload.zip", photos=False)

    def require_feature(self):
        self.assertIsNotNone(importlib.util.find_spec("app.backup.service"), "Task6 service is missing")
        self.assertIsNotNone(importlib.util.find_spec("app.backup.http"), "Task6 HTTP integration is missing")

    def wait(self, client, operation):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = client.get(f"/api/backup/operations/{operation}")
            self.assertEqual(response.status_code, 200, response.text)
            view = response.json()
            if view["status"] != "running":
                return view
            time.sleep(.01)
        self.fail("owned operation did not finish")

    def preview(self, client):
        response = client.post("/api/backup/preview", content=self.zip.read_bytes(), headers={"Content-Type": "application/zip"})
        self.assertEqual(response.status_code, 200, response.text)
        view = self.wait(client, response.json()["id"])
        self.assertEqual(view["status"], "done", view)
        return view

    def test_export_raw_preview_prepare_retry_cancel_and_rescue_group(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            initial = client.get("/api/backup/state").json()
            self.assertFalse(initial["require_header"])
            export = client.post("/api/backup/export", json={"includes_photos": False})
            self.assertEqual(export.status_code, 200, export.text)
            exported = self.wait(client, export.json()["id"])
            self.assertEqual(exported["status"], "done", exported)
            download = client.get(f"/api/backup/operations/{exported['id']}/download")
            self.assertEqual(download.status_code, 200)
            self.assertTrue(download.content.startswith(b"PK"))
            view = self.preview(client)
            body = {"preview_id": view["preview"]["id"], "epoch": initial["epoch"], "request_id": "same-request"}
            first = client.post("/api/backup/restore/prepare", json=body)
            self.assertEqual(first.status_code, 200, first.text)
            prepared = self.wait(client, first.json()["id"])
            self.assertEqual(prepared["status"], "done", prepared)
            retry = client.post("/api/backup/restore/prepare", json=body)
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertEqual(retry.json()["id"], first.json()["id"])
            rescue_id = prepared["prepared"]["rescue_id"]
            self.assertEqual(client.delete(f"/api/backup/rescues/{rescue_id}").status_code, 503)
            self.assertEqual(client.get("/api/customers").status_code, 503)
            conflict = client.post("/api/backup/restore/prepare", json={**body, "request_id": "different"})
            self.assertEqual(conflict.status_code, 409)
            with client.server.backup_service.download(rescue_id, rescue=True):
                self.assertEqual(client.post("/api/backup/restore/cancel", json={"operation_id": rescue_id}).status_code, 503)
            self.assertEqual(client.post("/api/backup/restore/cancel", json={"operation_id": rescue_id}).status_code, 200)
            consumed = client.post("/api/backup/restore/prepare", json={**body, "request_id": "consumed-preview"})
            self.assertEqual(consumed.status_code, 409, consumed.text)
            folder = self.layout.rescue_dir / rescue_id
            (folder / "original").mkdir(exist_ok=True)
            (folder / "original/synthetic").write_bytes(b"owned")
            with client.server.backup_service.download(rescue_id, rescue=True):
                self.assertEqual(client.delete(f"/api/backup/rescues/{rescue_id}").status_code, 503)
            self.assertEqual(client.get(f"/api/backup/rescues/{rescue_id}/download").status_code, 200)
            self.assertEqual(client.delete(f"/api/backup/rescues/{rescue_id}").status_code, 200)
            self.assertFalse(folder.exists())

    def test_errors_origin_unknown_ids_and_no_private_paths(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            before = business_digest(self.layout)
            unknown = str(uuid.uuid4())
            for method, path, kwargs, status in (
                ("post", "/api/backup/export", {"json": {"includes_photos": False}, "headers": {"Origin": "https://attacker.example"}}, 403),
                ("get", f"/api/backup/operations/{unknown}", {}, 404),
                ("get", f"/api/backup/operations/{unknown}/download", {}, 404),
                ("get", f"/api/backup/rescues/{unknown}/download", {}, 404),
                ("delete", f"/api/backup/rescues/{unknown}", {}, 404),
                ("post", "/api/backup/preview", {"content": b"bad", "headers": {"Content-Type": "multipart/form-data"}}, 422),
                ("post", "/api/backup/restore/prepare", {"json": {}}, 422),
            ):
                with self.subTest(path=path):
                    response = getattr(client, method)(path, **kwargs)
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertNotIn(str(self.root), response.text)
            self.assertEqual(business_digest(self.layout), before)
            bad = client.post("/api/backup/preview", content=b"not zip", headers={"Content-Type": "application/zip"})
            view = self.wait(client, bad.json()["id"])
            self.assertEqual(view["status"], "error")
            self.assertNotIn(str(self.root), json.dumps(view))

    def test_prepared_retry_survives_temp_cleanup_failure(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            view = self.preview(client)["preview"]
            body = {"preview_id": view["id"], "epoch": view["epoch"], "request_id": "cleanup-retry"}
            with patch.object(client.server.backup_service, "_cleanup", side_effect=BackupError("operation_failed", "合成清理失败")):
                operation = client.post("/api/backup/restore/prepare", json=body).json()["id"]
                self.wait(client, operation)
                retry = client.post("/api/backup/restore/prepare", json=body)
                self.assertEqual(retry.status_code, 200, retry.text)
                self.assertEqual(retry.json()["id"], operation)
                self.assertEqual(retry.json()["status"], "done")
                self.assertEqual(retry.json()["prepared"]["operation_id"], client.server.backup_coordinator.prepared_operation_id)
            self.assertEqual(len(client.get("/api/backup/state").json()["rescues"]), 1)

    def test_prepared_retry_reads_durable_result_when_worker_acknowledgement_is_lost(self):
        self.require_feature()
        from app.backup import service as service_module
        original = service_module.prepare_restore
        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise BackupError("prepare_failed", "合成完成响应丢失")
        with open_fixture_server(self.layout) as client:
            view = self.preview(client)["preview"]
            body = {"preview_id": view["id"], "epoch": view["epoch"], "request_id": "durable-retry"}
            with patch.object(service_module, "prepare_restore", side_effect=lost_ack):
                operation = client.post("/api/backup/restore/prepare", json=body).json()["id"]
                failed = self.wait(client, operation)
                self.assertEqual(failed["status"], "error")
                retry = client.post("/api/backup/restore/prepare", json=body)
                self.assertEqual(retry.status_code, 200, retry.text)
                self.assertEqual((retry.json()["id"], retry.json()["status"]), (operation, "done"))
                self.assertEqual(retry.json()["prepared"]["operation_id"], client.server.backup_coordinator.prepared_operation_id)
            self.assertEqual(len(client.get("/api/backup/state").json()["rescues"]), 1)

    def assert_cancel_without_retry_consumes_preview(self, client, body, operation):
        before = business_digest(self.layout)
        state = client.get("/api/backup/state").json()
        self.assertEqual(state["mode"], "prepared")
        self.assertEqual(len(state["rescues"]), 1)
        rescue = state["prepared"]["operation_id"]
        # No intervening prepare retry: cancellation must reconcile durable success.
        cancelled = client.post("/api/backup/restore/cancel", json={"operation_id": rescue})
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        reused = client.post("/api/backup/restore/prepare", json={**body, "request_id": "new-after-cancel"})
        self.assertEqual(reused.status_code, 409, reused.text)
        self.assertEqual(reused.json()["code"], "invalid_preview")
        self.assertIsNone(client.get(f"/api/backup/operations/{body['preview_id']}").json()["preview"])
        self.assertEqual(client.get(f"/api/backup/operations/{operation}").json()["status"], "cancelled")
        state = client.get("/api/backup/state").json()
        self.assertEqual((state["mode"], state["prepared"], len(state["rescues"])), ("ready", None, 1))
        self.assertFalse(state["rescues"][0]["active"])
        self.assertEqual(business_digest(self.layout), before)
        # Failed scratch deletion must not keep the single preview slot occupied.
        self.assertNotEqual(self.preview(client)["preview"]["id"], body["preview_id"])

    def test_prepared_ack_failure_cancel_without_retry_consumes_preview(self):
        self.require_feature()
        from app.backup import service as service_module
        original = service_module.prepare_restore
        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise BackupError("prepare_failed", "合成完成响应丢失")
        with open_fixture_server(self.layout) as client:
            view = self.preview(client)["preview"]
            body = {"preview_id": view["id"], "epoch": view["epoch"], "request_id": "ack-cancel-no-retry"}
            with patch.object(service_module, "prepare_restore", side_effect=lost_ack):
                operation = client.post("/api/backup/restore/prepare", json=body).json()["id"]
                self.assertEqual(self.wait(client, operation)["status"], "error")
            self.assert_cancel_without_retry_consumes_preview(client, body, operation)

    def test_prepared_cleanup_failure_cancel_without_retry_consumes_preview(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            view = self.preview(client)["preview"]
            body = {"preview_id": view["id"], "epoch": view["epoch"], "request_id": "cleanup-cancel-no-retry"}
            service = client.server.backup_service
            cleanup = service._cleanup
            def fail_source_cleanup(item):
                if item.view.id == view["id"]:
                    raise BackupError("operation_failed", "合成源清理失败")
                return cleanup(item)
            with patch.object(service, "_cleanup", side_effect=fail_source_cleanup):
                operation = client.post("/api/backup/restore/prepare", json=body).json()["id"]
                self.assertEqual(self.wait(client, operation)["status"], "error")
            self.assertTrue((self.layout.work_dir / view["id"]).exists())
            self.assert_cancel_without_retry_consumes_preview(client, body, operation)

    def test_only_one_preview_ttl_and_cancel_release_material(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            view = self.preview(client)
            self.assertEqual(client.post("/api/backup/preview", content=b"x", headers={"Content-Type": "application/zip"}).status_code, 503)
            self.assertEqual(client.post(f"/api/backup/operations/{view['id']}/cancel").status_code, 200)
            second = self.preview(client)
            with patch("app.backup.service.time.time", return_value=second["preview"]["expires_at"] + 1):
                response = client.post("/api/backup/restore/prepare", json={"preview_id": second["preview"]["id"], "epoch": second["preview"]["epoch"], "request_id": "expired"})
                self.assertEqual(response.status_code, 409)
                ticket = client.server.backup_service.begin_upload()
                ticket.abort()
            self.assertFalse(list(self.layout.work_dir.iterdir()))

    def test_preview_requires_valid_current_summary_and_free_exclusive_lease(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            customers = self.layout.data_dir / "customers.json"
            original = customers.read_bytes()
            customers.write_bytes(b"malformed-owned-current")
            operation = self.preview_start(client)
            invalid = self.wait(client, operation)
            self.assertEqual(invalid["status"], "error")
            self.assertIsNone(invalid["preview"])
            self.assertFalse(list(self.layout.work_dir.iterdir()))
            customers.write_bytes(original)
            ticket = client.server.backup_coordinator.reserve_worker("owned-preview-conflict", mutating=True)
            try:
                operation = self.preview_start(client)
                busy = self.wait(client, operation)
                self.assertEqual((busy["status"], busy["code"]), ("error", "maintenance_busy"))
                self.assertIsNone(busy["preview"])
            finally:
                ticket.abort_start()
            self.assertFalse(list(self.layout.work_dir.iterdir()))
            self.assertEqual(self.preview(client)["status"], "done")

    def test_old_epoch_rejected_for_all_mutations_before_side_effects(self):
        self.require_feature()
        epoch = str(uuid.uuid4())
        self.layout.epoch_file.write_text(json.dumps({"format_version": 1, "epoch": epoch, "require_header": True}))
        with open_fixture_server(self.layout) as client:
            before = business_digest(self.layout)
            mutations = [route for route in client.server.app.routes if getattr(route, "path", "").startswith("/api/") and not route.path.startswith("/api/backup/") and set(getattr(route, "methods", [])) & {"POST", "PUT", "PATCH", "DELETE"}]
            self.assertGreater(len(mutations), 35)
            for route in mutations:
                for method in set(route.methods) & {"POST", "PUT", "PATCH", "DELETE"}:
                    path = route.path
                    import re
                    path = re.sub(r"\{[^}]+\}", "1", path)
                    for headers in ({}, {"X-Tradewind-Data-Epoch": "old-epoch"}):
                        response = client.request(method, path, json={}, headers=headers)
                        self.assertEqual(response.status_code, 409, (method, path, response.text))
                        self.assertEqual(response.json()["code"], "stale_epoch")
            self.assertEqual(business_digest(self.layout), before)

    def test_missing_epoch_before_first_restore_remains_compatible(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            response = client.post("/api/products", json={"title": "synthetic", "content": "owned"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(self.layout.epoch_file.exists())

    def test_lazy_get_initialization_advances_revision_but_polling_does_not(self):
        self.require_feature()
        from backup_helpers import make_constructor_fixture
        layout = make_constructor_fixture(self.root / "partial", groups=("outreach",))
        with open_fixture_server(layout) as client:
            view = self.preview(client)
            before = client.get("/api/backup/state").json()["revision"]
            self.assertEqual(client.get("/api/history").status_code, 200)
            after = client.get("/api/backup/state").json()["revision"]
            self.assertGreater(after, before)
            client.get("/api/history")
            self.assertEqual(client.get("/api/backup/state").json()["revision"], after)
            response = client.post("/api/backup/restore/prepare", json={"preview_id": view["preview"]["id"], "epoch": view["preview"]["epoch"], "request_id": "changed-schema"})
            self.assertEqual((response.status_code, response.json()["code"]), (409, "invalid_preview"))

    def test_state_is_nonvalidating_and_custom_memory_does_not_block_business(self):
        self.require_feature()
        from app.backup.service import BackupService
        (self.layout.data_dir / "customers.json").write_bytes(b"malformed synthetic")
        coor = MaintenanceCoordinator(self.layout)
        service = BackupService(self.layout, memory_path=self.root / "custom.db", app_version="0.2.0", coordinator=coor, limits=BackupLimits())
        self.assertEqual(service.state().mode, "ready")
        with self.assertRaises(BackupError) as caught:
            service.start_export(False)
        self.assertEqual(caught.exception.code, "custom_memory_path")
        with coor.service(None, mutating=False):
            pass
        with open_fixture_server(self.layout, memory_path=self.layout.data_dir / "custom-owned.db") as client:
            bootstrap = client.get("/api/backup/state")
            self.assertEqual((bootstrap.status_code, bootstrap.json()["mode"]), (200, "ready"))
            self.assertEqual(client.get("/api/health").json()["status"], "ok")
            self.assertEqual(client.get("/api/products").status_code, 200)
            response = client.post("/api/backup/export", json={})
            self.assertEqual((response.status_code, response.json()["code"]), (422, "custom_memory_path"))

    def test_lazy_get_after_restore_needs_no_header_but_records_initialization(self):
        self.require_feature()
        from backup_helpers import make_constructor_fixture
        layout = make_constructor_fixture(self.root / "restored-partial", groups=("outreach",))
        layout.epoch_file.write_text(json.dumps({"format_version": 1, "epoch": str(uuid.uuid4()), "require_header": True}))
        with open_fixture_server(layout) as client:
            before = client.server.backup_coordinator.version()[1]
            try:
                response = client.get("/api/history")
            except Exception as exc:
                response = exc
            self.assertFalse(isinstance(response, BaseException), response)
            self.assertEqual(response.status_code, 200)
            self.assertGreater(client.server.backup_coordinator.version()[1], before)

    def test_startup_initialization_records_real_writes_and_repeated_cleanup_is_readonly(self):
        self.require_feature()
        from backup_helpers import make_constructor_fixture
        layout = make_constructor_fixture(self.root / "startup-empty", groups=())
        with open_fixture_server(layout) as client:
            coor = client.server.backup_coordinator
            self.assertGreater(coor.version()[1], 0)
            before = coor.version()[1]
            for handler in client.server.app.router.on_startup:
                handler()
            self.assertEqual(coor.version()[1], before)

    def test_upload_limit_abort_disconnect_and_thread_start_failure_release(self):
        self.require_feature()
        from app.backup.service import BackupService
        coor = MaintenanceCoordinator(self.layout)
        service = BackupService(self.layout, memory_path=self.layout.data_dir / "tradewind_memory.db", app_version="0.2.0", coordinator=coor, limits=replace(BackupLimits(), max_archive_bytes=3))
        ticket = service.begin_upload()
        ticket.write(b"123")
        with self.assertRaises(BackupError) as caught:
            ticket.write(b"4")
        self.assertEqual(caught.exception.code, "resource_limit")
        ticket.abort(); ticket.abort()
        self.assertFalse(list(self.layout.work_dir.iterdir()))
        with patch("app.backup.service.threading.Thread.start", side_effect=RuntimeError("private path")):
            with self.assertRaises(BackupError):
                service.start_export(False)
        with coor.exclusive("snapshot"):
            pass

    def test_download_ownership_rejects_cancel_until_context_closed(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            export = client.post("/api/backup/export", json={}).json()
            view = self.wait(client, export["id"])
            service = client.server.backup_service
            with service.download(view["id"], rescue=False) as path:
                self.assertTrue(path.is_file())
                self.assertEqual(client.post(f"/api/backup/operations/{view['id']}/cancel").status_code, 503)
            service.cancel(view["id"])
            self.assertFalse(path.exists())

    def test_photo_estimate_is_cached_from_capture_and_service_version_is_authoritative(self):
        self.require_feature()
        import io
        import zipfile
        with open_fixture_server(self.layout) as client:
            client.server.backup_service.app_version = "synthetic-version"
            export = client.post("/api/backup/export", json={"includes_photos": True}).json()["id"]
            self.assertEqual(self.wait(client, export)["status"], "done")
            response = client.get(f"/api/backup/operations/{export}/download")
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["app_version"], "synthetic-version")
            self.assertEqual(client.get("/api/backup/state").json()["photo_count"], 1)

    def test_export_expiry_waits_for_download_and_restart_cleanup_retains_rescues(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            service = client.server.backup_service
            operation = client.post("/api/backup/export", json={}).json()["id"]
            self.wait(client, operation)
            with service.download(operation, rescue=False) as path:
                with patch("app.backup.service.time.time", return_value=time.time() + 24 * 3600 + 1):
                    self.assertEqual(service.operation(operation).status, "done")
                    self.assertTrue(path.exists())
            with patch("app.backup.service.time.time", return_value=time.time() + 24 * 3600 + 1):
                self.assertEqual(service.operation(operation).status, "cancelled")
            self.assertFalse(path.exists())
            preview = self.preview(client)["preview"]
            prepared = client.post("/api/backup/restore/prepare", json={"preview_id": preview["id"], "epoch": preview["epoch"], "request_id": "rescue-retain"}).json()["id"]
            rescue = self.wait(client, prepared)["prepared"]["rescue_id"]
            service.cancel_restore(rescue)
            export = client.post("/api/backup/export", json={}).json()["id"]
            self.wait(client, export)
            from app.backup.service import BackupService
            new = BackupService(self.layout, memory_path=service.memory_path, app_version="0.2.0", coordinator=service.coordinator, limits=service.limits)
            self.assertFalse(list(self.layout.work_dir.iterdir()))
            with patch("app.backup.service.time.time", return_value=time.time() + 30 * 24 * 3600):
                self.assertEqual([item.id for item in new.state().rescues], [rescue])

    def test_streamed_response_and_download_hold_ownership_until_close(self):
        self.require_feature()
        from starlette.responses import StreamingResponse
        from starlette.routing import Route
        from app.backup.http import OwnedFileResponse
        with open_fixture_server(self.layout) as client, patch.object(OwnedFileResponse, "chunk_size", 32):
            server = client.server
            async def stream(request):
                async def chunks():
                    yield b"first"
                    yield b"last"
                return StreamingResponse(chunks())
            server.app.router.routes.append(Route("/lease-stream", stream))
            export = client.post("/api/backup/export", json={}).json()
            self.wait(client, export["id"])

            async def exercise(path, probe, *, disconnect=False):
                scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"", "headers": [], "scheme": "http", "server": ("testserver", 80), "client": ("127.0.0.1", 1)}
                request_sent = False
                body_seen = asyncio.Event()
                async def receive():
                    nonlocal request_sent
                    if not request_sent:
                        request_sent = True
                        return {"type": "http.request", "body": b"", "more_body": False}
                    await body_seen.wait()
                    if disconnect:
                        return {"type": "http.disconnect"}
                    await asyncio.Event().wait()
                async def send(message):
                    if message["type"] == "http.response.body":
                        probe()
                        body_seen.set()
                        if disconnect:
                            raise asyncio.CancelledError()
                try:
                    await server.app(scope, receive, send)
                except asyncio.CancelledError:
                    pass
            def business_probe():
                with self.assertRaises(BackupError):
                    server.backup_coordinator.reserve_exclusive("snapshot")
            def download_probe():
                with self.assertRaises(BackupError):
                    server.backup_service.cancel(export["id"])
            for disconnect in (False, True):
                asyncio.run(exercise("/lease-stream", business_probe, disconnect=disconnect))
                with server.backup_coordinator.exclusive("snapshot"):
                    pass
                asyncio.run(exercise(f"/api/backup/operations/{export['id']}/download", download_probe, disconnect=disconnect))
            server.backup_service.cancel(export["id"])

    def test_all_six_actual_worker_starts_hold_ticket_after_http_returns(self):
        self.require_feature()
        from test_server_outreach import EMAIL_CONFIG, PROFILE, EMAIL_CONTENT, FakeCredentialStore
        from app.outreach_smtp import SendOutcome
        with open_fixture_server(self.layout) as client:
            server = client.server
            for kind in ("single_email", "batch", "document", "photo", "crawler", "smtp"):
                with self.subTest(kind=kind), ExitStack() as stack:
                    entered, release = threading.Event(), threading.Event()
                    def wait(*args, **kwargs):
                        entered.set()
                        if not release.wait(10):
                            raise AssertionError("owned worker not released")
                    stack.enter_context(patch.object(server, "get_company_profile", return_value=PROFILE))
                    stack.enter_context(patch.object(server, "get_email_outreach_config", return_value=EMAIL_CONFIG))
                    if kind == "single_email":
                        class Workflow:
                            def start(self, *args):
                                wait()
                                return {"status": "done"}
                        stack.enter_context(patch.object(server, "_email_workflow", return_value=Workflow()))
                        response = client.post("/api/email/start", json={"customer": "owned", "country": "test", "product": "synthetic"})
                    elif kind == "batch":
                        stack.enter_context(patch.object(server, "_run_batch", side_effect=wait))
                        response = client.post("/api/email/batch", json={"rows": [{"name": "owned"}], "product": "synthetic"})
                    elif kind == "document":
                        def extract(task_id, path, kind):
                            try:
                                wait()
                            finally:
                                path.unlink(missing_ok=True)
                        stack.enter_context(patch.object(server, "_run_extract", side_effect=extract))
                        response = client.post("/api/products/extract", files={"file": ("owned.txt", b"synthetic")})
                    elif kind == "photo":
                        def photo(*args):
                            try:
                                wait()
                            finally:
                                server.PHOTO_RUN_LOCK.release()
                        stack.enter_context(patch.object(server, "_run_photo_scan", side_effect=photo))
                        stack.enter_context(patch.object(server, "get_vision_config", return_value={"api_key": "synthetic-not-a-key"}))
                        response = client.post("/api/photos/start", json={"images": [{"filename": "owned.gif", "data_base64": "AA=="}]})
                    elif kind == "crawler":
                        def hunt(*args, **kwargs):
                            wait()
                            return []
                        stack.enter_context(patch("app.crawler.webs_hunter.hunt_websites", side_effect=hunt))
                        response = client.post("/api/crawler/start", json={"source": "webs", "queries": "synthetic"})
                    else:
                        created = server.api_outreach_create(server.OutreachCreateRequest(customer_id="customer-甲", channel="email", content=EMAIL_CONTENT))["item"]
                        FakeCredentialStore.values = {("gmail_smtp", "sender@example.test"): "synthetic-secret"}
                        stack.enter_context(patch.object(server, "CredentialStore", FakeCredentialStore))
                        def send(*args):
                            wait()
                            return SendOutcome("accepted", "server_accepted", "owned", "synthetic")
                        stack.enter_context(patch.object(server.SmtpSender, "send", side_effect=send))
                        response = client.post(f"/api/outreach/jobs/{created['id']}/send-email")
                    try:
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertTrue(entered.wait(3), (kind, response.text))
                        self.assertEqual(client.post("/api/backup/export", json={}).status_code, 503)
                        # A cancellation/status change cannot release the worker ticket.
                        for tasks in (server.TASKS, server.EXTRACT_TASKS, server.PHOTO_TASKS, server.CRAWLER_TASKS):
                            for task in tasks.values():
                                task["cancel"] = True
                        self.assertEqual(client.post("/api/backup/export", json={}).status_code, 503)
                    finally:
                        release.set()
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            try:
                                lease = server.backup_coordinator.reserve_exclusive("snapshot")
                            except BackupError:
                                time.sleep(.01)
                            else:
                                lease.release()
                                break
                        else:
                            self.fail("worker ticket leaked")

    def test_download_receive_disconnect_stops_reading_and_closes_file(self):
        self.require_feature()
        from app.backup.http import OwnedFileResponse
        with open_fixture_server(self.layout) as client, patch.object(OwnedFileResponse, "chunk_size", 32):
            operation = client.post("/api/backup/export", json={}).json()["id"]
            self.wait(client, operation)
            service = client.server.backup_service
            with service.download(operation, rescue=False) as path:
                size = path.stat().st_size
            sent = 0
            async def exercise():
                scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "method": "GET", "path": f"/api/backup/operations/{operation}/download", "query_string": b"", "headers": [], "scheme": "http", "server": ("testserver", 80), "client": ("127.0.0.1", 1)}
                first_body = asyncio.Event()
                request_sent = False
                async def receive():
                    nonlocal request_sent
                    if not request_sent:
                        request_sent = True
                        return {"type": "http.request", "body": b"", "more_body": False}
                    await first_body.wait()
                    return {"type": "http.disconnect"}
                async def send(message):
                    nonlocal sent
                    if message["type"] == "http.response.body":
                        with self.assertRaises(BackupError):
                            service.cancel(operation)
                        sent += len(message.get("body", b""))
                        first_body.set()
                        await asyncio.sleep(.001)
                await client.server.app(scope, receive, send)
            asyncio.run(exercise())
            self.assertLess(sent, size)
            service.cancel(operation)
            self.assertFalse(path.exists())

    def test_registered_thread_start_failure_leaves_coordinator_available(self):
        self.require_feature()
        from app.backup.http import start_registered_worker
        coordinator = MaintenanceCoordinator(self.layout)
        with patch("threading.Thread.start", side_effect=RuntimeError("synthetic-start-failure")):
            with self.assertRaises(RuntimeError):
                start_registered_worker(coordinator, "batch", lambda: None)
        with coordinator.exclusive("snapshot"):
            pass

    def test_standalone_asgi_gate_releases_lease_after_final_send_without_app_state(self):
        self.require_feature()
        from app.backup.http import BackupMaintenanceMiddleware
        coor = MaintenanceCoordinator(self.layout)
        async def endpoint(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"owned", "more_body": False})
        async def exercise():
            async def receive():
                return {"type": "http.disconnect"}
            async def send(message):
                with self.assertRaises(BackupError):
                    coor.reserve_exclusive("snapshot")
            await BackupMaintenanceMiddleware(endpoint, coor)({"type": "http", "method": "GET", "path": "/business", "headers": []}, receive, send)
        try:
            asyncio.run(exercise())
        except Exception as exc:
            result = exc
        else:
            result = None
        self.assertIsNone(result)
        with coor.exclusive("snapshot"):
            pass

    def test_raw_upload_disconnect_drains_owned_ticket_and_maps_limits_and_space(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            server = client.server
            server.backup_service.limits = replace(BackupLimits(), max_archive_bytes=3)
            limited = client.post("/api/backup/preview", content=b"1234", headers={"Content-Type": "application/zip"})
            self.assertEqual((limited.status_code, limited.json()["code"]), (413, "resource_limit"))
            with patch("app.backup.service.require_space", side_effect=BackupError("insufficient_space", "磁盘空间不足")):
                full = client.post("/api/backup/preview", content=b"1", headers={"Content-Type": "application/zip"})
                self.assertEqual((full.status_code, full.json()["code"]), (507, "insufficient_space"))
            async def disconnect():
                from starlette.requests import ClientDisconnect
                scope = {"type": "http", "asgi": {"version": "3.0"}, "method": "POST", "path": "/api/backup/preview", "raw_path": b"/api/backup/preview", "query_string": b"", "headers": [(b"content-type", b"application/zip")], "scheme": "http", "server": ("testserver", 80), "client": ("127.0.0.1", 1)}
                messages = iter([{"type": "http.request", "body": b"1", "more_body": True}, {"type": "http.disconnect"}])
                async def receive():
                    return next(messages)
                async def send(message):
                    pass
                try:
                    await server.app(scope, receive, send)
                except ClientDisconnect:
                    pass
            asyncio.run(disconnect())
            self.assertFalse(list(self.layout.work_dir.iterdir()))
            ticket = server.backup_service.begin_upload()
            ticket.abort()

    def test_backup_control_mutations_reject_stale_epoch_and_invalid_bodies_are_safe(self):
        self.require_feature()
        epoch = str(uuid.uuid4())
        self.layout.epoch_file.write_text(json.dumps({"format_version": 1, "epoch": epoch, "require_header": True}))
        with open_fixture_server(self.layout) as client:
            before = business_digest(self.layout)
            for path, method in (("/api/backup/export", "POST"), ("/api/backup/preview", "POST"),
                ("/api/backup/operations/unknown/cancel", "POST"), ("/api/backup/restore/prepare", "POST"),
                ("/api/backup/restore/cancel", "POST"), ("/api/backup/rescues/unknown", "DELETE")):
                for header in ({}, {"X-Tradewind-Data-Epoch": "old"}):
                    response = client.request(method, path, json={}, headers=header)
                    self.assertEqual((response.status_code, response.json().get("code")), (409, "stale_epoch"), path)
            self.assertEqual(business_digest(self.layout), before)

    def test_invalid_body_maps_safe_code_without_echoing_input(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            invalid = client.post("/api/backup/export", json={"includes_photos": "private-input-should-not-echo"})
            self.assertEqual((invalid.status_code, invalid.json().get("code")), (422, "invalid_request"))
            self.assertNotIn("private-input-should-not-echo", invalid.text)

    def test_cancel_disk_failure_is_sanitized_and_download_remains_owned(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            operation = client.post("/api/backup/export", json={}).json()["id"]
            self.wait(client, operation)
            with patch("app.backup.service._remove_owned", side_effect=OSError("private-path-should-not-print")):
                try:
                    result = client.post(f"/api/backup/operations/{operation}/cancel")
                except Exception as exc:
                    result = exc
            self.assertFalse(isinstance(result, BaseException), result)
            self.assertEqual(result.status_code, 422)
            self.assertNotIn("private-path-should-not-print", result.text)

    def test_cancel_during_upload_and_validation_and_export_releases_after_worker_exit(self):
        self.require_feature()
        with open_fixture_server(self.layout) as client:
            from app.backup import service as service_module
            service = client.server.backup_service
            ticket = service.begin_upload()
            ticket.write(b"owned")
            service.cancel(ticket._item.view.id)
            ticket.abort()
            self.assertFalse(list(self.layout.work_dir.iterdir()))
            for function, start in (("validate_archive", lambda: self.preview_start(client)),
                                    ("write_archive", lambda: client.post("/api/backup/export", json={}).json()["id"])):
                original = getattr(service_module, function)
                entered, release = threading.Event(), threading.Event()
                def delayed(*args, **kwargs):
                    entered.set()
                    self.assertTrue(release.wait(5))
                    return original(*args, **kwargs)
                with patch.object(service_module, function, side_effect=delayed):
                    operation = start()
                    try:
                        self.assertTrue(entered.wait(3))
                        service.cancel(operation)
                        self.assertEqual(service.operation(operation).status, "running")
                    finally:
                        release.set()
                    view = self.wait(client, operation)
                    self.assertEqual(view["status"], "cancelled", view)
                self.assertFalse(list(self.layout.work_dir.iterdir()))

    def preview_start(self, client):
        return client.post("/api/backup/preview", content=self.zip.read_bytes(), headers={"Content-Type": "application/zip"}).json()["id"]


if __name__ == "__main__":
    unittest.main()
