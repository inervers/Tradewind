"""Actual random-port processes, synthetic A/B/rescue and disconnect acceptance.

Run only through scripts/test-backup-core.py so the fence precedes ALL imports.
TASK9_MUTANT=unknown or suppression deliberately breaks the child restore safety
to demonstrate these assertions detect defects; not a pre-existing product RED.
"""
from __future__ import annotations

from contextlib import closing
import http.client
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import Request, ProxyHandler, build_opener
from urllib.parse import quote
import zipfile

from backup_helpers import make_business_fixture, make_archive

ROOT = Path(__file__).resolve().parents[1]
BODY = "Hello\n\nDemo User\nDemoMed\nIf you prefer not to receive further messages, please let me know."


def wait_until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("owned acceptance condition timed out")


class OwnedServer:
    def __init__(self, root):
        self.root = root
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        assert self.port != 8101
        self.base = f"http://127.0.0.1:{self.port}"
        self.opener = build_opener(ProxyHandler({}))
        self.process = None
        self.epoch = None

    def start(self):
        # install_fence is prepended by the parent runner to this -c bootstrap.
        bootstrap = '''
import os, json, pathlib, anyio
root = pathlib.Path(os.environ['TRADEWIND_DATA_DIR'])
evidence = {'smtp_calls': 0, 'model_calls': 0, 'credential_calls': 0}
mutant = os.environ.get('TASK9_MUTANT')
if mutant:
    import app.backup.restore as restore, sqlite3
    original_safety = restore.apply_contact_safety
    def faulty_safety(restored, rescue):
        result = original_safety(restored, rescue)
        if restored.exists():
            with sqlite3.connect(restored) as conn:
                if mutant == 'unknown':
                    conn.execute("UPDATE outreach_jobs SET status='pending_review' WHERE status='unknown'")
                elif mutant == 'suppression':
                    conn.execute("DELETE FROM contact_suppressions WHERE normalized_recipient='local-only@shop.example'")
        return result
    restore.apply_contact_safety = faulty_safety
    if mutant == 'download-owner-leak':
        from contextlib import contextmanager
        from app.backup.service import BackupService
        original_download = BackupService.download
        retained = []
        @contextmanager
        def leaked_download(self, id, *, rescue):
            owner = original_download(self, id, rescue=rescue)
            path = owner.__enter__()
            retained.append(owner)
            yield path
        BackupService.download = leaked_download
import server, uvicorn
server.LOCAL_UI_ORIGINS.add(os.environ['TASK9_ORIGIN'])
server.get_email_outreach_config = lambda: {'provider':'gmail_smtp','address':'sender@example.test','transport':'ssl'}
server.get_company_profile = lambda: {'sender_name':'Demo User','company_name':'DemoMed','email':'sender@example.test'}
class SyntheticCredentials:
    def has(self, *args):
        evidence['credential_calls'] += 1
        return True
    def get(self, *args):
        evidence['credential_calls'] += 1
        return 'synthetic-credential'
server.CredentialStore = SyntheticCredentials
def no_smtp(*args, **kwargs):
    evidence['smtp_calls'] += 1
    raise AssertionError('SMTP prohibited')
server.SmtpSender.send = no_smtp
def no_model(*args, **kwargs):
    evidence['model_calls'] += 1
    raise AssertionError('model prohibited')
server.generate_email = no_model
from app.backup.http import OwnedFileResponse
original_response = OwnedFileResponse.__call__
OwnedFileResponse.chunk_size = 256
async def observed_response(self, scope, receive, send):
    slow = dict(scope.get('headers', [])).get(b'x-task9-slow-stream') == b'1'
    complete = False
    async def observed_send(message):
        nonlocal complete
        await send(message)
        if message['type'] == 'http.response.body':
            complete = not message.get('more_body', False)
            if slow and message.get('body') and not complete:
                (root/'stream-started').touch()
                while not (root/'stream-release').exists():
                    await anyio.sleep(.02)
    try:
        await original_response(self, scope, receive, observed_send)
    finally:
        if slow:
            (root/'stream-ended.json').write_text(json.dumps({'complete':complete}), encoding='utf-8')
OwnedFileResponse.__call__ = observed_response
@server.app.get('/__task9/evidence')
def owned_evidence():
    import socket
    return {**evidence, 'external_requests': list(socket._task9_denied),
            'rescue_owners': sum(server.backup_service._rescue_owners.values())}
# The production static mount is already last at server import. This test-only
# route must precede it; do not change production routing for instrumentation.
server.app.router.routes.insert(0, server.app.router.routes.pop())
uvicorn.run(server.app, host='127.0.0.1', port=int(os.environ['TASK9_PORT']), log_level='warning')
'''
        env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_API_KEY")
               and k not in {"PYTHONPATH", "MEMORY_DB_PATH", "LOCAL_KB_PATH", "TRADEWIND_DATA_DIR", "TRADEWIND_NO_BROWSER"}}
        env.update(TRADEWIND_DATA_DIR=str(self.root), TRADEWIND_NO_BROWSER="1",
                   TASK9_ORIGIN=self.base, TASK9_PORT=str(self.port))
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen([sys.executable, "-B", "-c", bootstrap],
                                        cwd=ROOT, env=env, stdout=self.log, stderr=self.log)
        def healthy():
            if self.process.poll() is not None:
                self.log.seek(0)
                raise AssertionError("Owned server exited: " + self.log.read().decode(errors="replace")[-2000:])
            try:
                return self.request("/api/health")[1].get("service") == "tradewind"
            except OSError:
                return False
        wait_until(healthy, 60)
        self.epoch = self.request("/api/backup/state")[1]["epoch"]

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=15)
            self.log.close()
            self.process = None

    def restart(self):
        self.stop()
        self.start()

    def request(self, path, method="GET", payload=None, *, raw=False, epoch=None):
        headers = {"Origin": self.base}
        if self.epoch:
            headers["X-Tradewind-Data-Epoch"] = epoch or self.epoch
        if payload is not None:
            headers["Content-Type"] = "application/zip" if raw else "application/json"
            payload = payload if raw else json.dumps(payload).encode()
        from urllib.error import HTTPError
        try:
            response = self.opener.open(Request(self.base + quote(path, safe="/?=&%"), data=payload, headers=headers, method=method), timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            return response.status, body if raw and method == "GET" else json.loads(body)

    def operation(self, value):
        def done():
            current = self.request("/api/backup/operations/" + value["id"])[1]
            return current if current["status"] != "running" else None
        current = wait_until(done)
        if current["status"] != "done":
            raise AssertionError(current)
        return current

    def export(self, photos):
        status, value = self.request("/api/backup/export", "POST", {"includes_photos": photos})
        assert status == 200, value
        operation = self.operation(value)
        status, body = self.request("/api/backup/operations/" + operation["id"] + "/download", raw=True)
        assert status == 200
        return body

    def prepare(self, archive, request_id):
        status, value = self.request("/api/backup/preview", "POST", archive, raw=True)
        assert status == 200, value
        preview = self.operation(value)["preview"]
        status, value = self.request("/api/backup/restore/prepare", "POST",
                                     {"preview_id": preview["id"], "epoch": preview["epoch"], "request_id": request_id})
        assert status == 200, value
        return self.operation(value)["prepared"]


class BackupRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="task9-roundtrip-")
        self.root = Path(self.temp.name)
        self.layout = make_business_fixture(self.root / "source")
        self.config_bytes = b'{ "protected_fake_config": "local-only", "providers": {} }\n'
        (self.layout.data_dir / "config.json").write_bytes(self.config_bytes)
        (self.layout.data_dir.parent / ".env").write_text("", encoding="utf-8")
        with closing(sqlite3.connect(self.layout.data_dir / "tradewind_memory.db")) as conn, conn:
            conn.execute("INSERT INTO outreach_jobs(id,workflow_id,customer_id,customer_name_snapshot,channel,provider,recipient,subject,body,content_hash,status,sender_account_snapshot,created_at,updated_at) VALUES(12,'task9-mail','customer-甲','合成甲店','email','gmail_smtp','owner@shop.example','Demo',?,?,'pending_review','sender@example.test',1704067200,1704067200)",
                         (BODY, hashlib.sha256(('Demo\0' + BODY).encode()).hexdigest()))
        self.server = OwnedServer(self.layout.data_dir.parent)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.server.stop)
        self.server.start()
        self.assertEqual(self.server.request("/api/backup/state")[1]["photo_count"], 0)

    def query(self, sql):
        with closing(sqlite3.connect(self.layout.data_dir / "tradewind_memory.db")) as conn:
            return conn.execute(sql).fetchall()

    def local_suppression(self):
        status, result = self.server.request("/api/outreach/suppressions", "POST",
                                            {"channel": "email", "recipient": "local-only@shop.example", "reason": "synthetic local DNC"})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])

    def assert_safety(self):
        self.assertIn(("email", "local-only@shop.example"), self.query("SELECT channel,normalized_recipient FROM contact_suppressions"))
        self.assertEqual((self.layout.data_dir / "config.json").read_bytes(), self.config_bytes)
        evidence = self.server.request("/__task9/evidence")[1]
        self.assertEqual(evidence["smtp_calls"], 0)
        self.assertEqual(evidence["model_calls"], 0)
        self.assertEqual(evidence["external_requests"], [])

    def test_actual_A_B_rescue_restore_A_then_rescue_B_exact_ids_old_page_checkpoint_and_roots(self):
        # A real SQLite checkpoint is populated using the persisted saver, not a
        # fake workflow projection. Restore must remove, never replay, this DB.
        from langgraph.checkpoint.sqlite import SqliteSaver
        checkpoint = self.layout.data_dir / "tradewind_workflows.db"
        with closing(sqlite3.connect(checkpoint)) as conn:
            saver = SqliteSaver(conn)
            saver.setup()
            from langgraph.checkpoint.base import empty_checkpoint
            saved = empty_checkpoint()
            saved["channel_values"] = {"workflow_id": "old-checkpoint", "customer_id": "customer-甲", "email": "synthetic paused"}
            saver.put({"configurable": {"thread_id": "old-checkpoint", "checkpoint_ns": ""}}, saved, {"source": "input", "step": -1, "parents": {}}, {})
        archive_a = self.server.export(True)
        cached = self.server.request("/api/backup/state")[1]
        self.assertEqual(cached["photo_count"], 1)
        photo = self.layout.data_dir / "crawler_photos/合成甲店/01.gif"
        extra_photo = photo.with_name("cache-only.gif")
        extra_photo.write_bytes(photo.read_bytes())
        stale = self.server.request("/api/backup/state")[1]
        self.assertEqual(stale["photo_count"], 1)  # cached, NOT a fresh scan
        self.assertEqual(stale["estimated_bytes"], cached["estimated_bytes"])
        extra_photo.unlink()
        with zipfile.ZipFile(io.BytesIO(archive_a)) as archive:
            self.assertNotIn("data/config.json", archive.namelist())
            self.assertNotIn("data/tradewind_workflows.db", archive.namelist())
        old_epoch = self.server.epoch
        self.server.stop()
        customers = json.loads((self.layout.data_dir / "customers.json").read_text(encoding="utf-8"))
        customers[0].update(id="customer-B", name="合成乙店")
        (self.layout.data_dir / "customers.json").write_text(json.dumps(customers), encoding="utf-8")
        with closing(sqlite3.connect(self.layout.data_dir / "tradewind_memory.db")) as conn, conn:
            conn.execute("UPDATE customer_followup_records SET id=6,customer_id='customer-B',request_id='record-B'")
            conn.execute("UPDATE customer_followup_deleted_requests SET customer_id='customer-B'")
            conn.execute("UPDATE customer_followup_plans SET id=id+1,customer_id='customer-B',previous_plan_id=CASE WHEN previous_plan_id IS NULL THEN NULL ELSE previous_plan_id+1 END")
            conn.execute("UPDATE sqlite_sequence SET seq=14 WHERE name='customer_followup_plans'")
            conn.execute("UPDATE outreach_jobs SET customer_id='customer-B' WHERE id=12")
        photo_a = self.layout.data_dir / "crawler_photos/合成甲店/01.gif"
        photo_b = photo_a.with_name("B.gif")
        photo_b.write_bytes(photo_a.read_bytes())
        photo_a.unlink()
        self.server.start()
        self.local_suppression()
        prepared = self.server.prepare(archive_a, "task9-A")
        self.assertEqual(self.server.request("/api/backup/state")[1]["mode"], "prepared")
        self.assertEqual(self.server.request("/api/customers")[0], 503)
        self.assertEqual(json.loads((self.layout.data_dir / "customers.json").read_text())[0]["id"], "customer-B")
        rescue_b = self.server.request("/api/backup/rescues/" + prepared["rescue_id"] + "/download", raw=True)[1]
        self.server.restart()
        self.assertEqual([c["id"] for c in self.server.request("/api/customers")[1]], ["customer-甲"])
        self.assertEqual(self.query("SELECT id FROM customer_followup_records"), [(5,)])
        self.assertEqual(self.query("SELECT id FROM customer_followup_plans ORDER BY id"), [(9,), (13,)])
        self.assertEqual(self.query("SELECT id FROM outreach_jobs ORDER BY id"), [(11,), (12,)])
        self.assertTrue(photo_a.is_file())
        self.assertFalse(photo_b.exists())
        self.assertNotEqual(old_epoch, self.server.epoch)
        status, error = self.server.request("/api/customers/customer-甲", "PUT", {"name": "stale edit"}, epoch=old_epoch)
        self.assertEqual((status, error["code"]), (409, "stale_epoch"))
        self.assertEqual(self.server.request("/api/email/tasks/old-checkpoint")[1]["status"], "not_found")
        self.assert_safety()
        self.server.prepare(rescue_b, "task9-B")
        self.server.restart()
        self.assertEqual([c["id"] for c in self.server.request("/api/customers")[1]], ["customer-B"])
        self.assertEqual(self.query("SELECT id FROM customer_followup_records"), [(6,)])
        self.assertEqual(self.query("SELECT id FROM customer_followup_plans ORDER BY id"), [(10,), (14,)])
        self.assertTrue(photo_b.is_file())
        self.assertFalse(photo_a.exists())
        self.assert_safety()
        # The same source code, two synthetic data roots; no real portable launch.
        portable = make_business_fixture(self.root / "portable-root")
        portable_config = b'{"protected_fake_config":"other-root"}'
        (portable.data_dir / "config.json").write_bytes(portable_config)
        other = OwnedServer(portable.data_dir.parent)
        try:
            other.start()
            self.assertNotEqual(other.epoch, self.server.epoch)
            self.assertEqual([c["id"] for c in other.request("/api/customers")[1]], ["customer-甲"])
            self.assertEqual(other.request("/api/email/tasks/old-checkpoint")[1]["status"], "not_found")
            self.assertEqual((portable.data_dir / "config.json").read_bytes(), portable_config)
        finally:
            other.stop()
        self.assertEqual([c["id"] for c in self.server.request("/api/customers")[1]], ["customer-B"])

    def test_unknown_mail_retry_still_requires_sentbox_decision(self):
        archive = self.server.export(False)
        self.local_suppression()
        self.server.prepare(archive, "task9-unknown")
        self.server.restart()
        item = next(j for j in self.server.request("/api/outreach/jobs")[1] if j["id"] == 12)
        self.assertEqual((item["status"], item["error_code"]), ("unknown", "restore_sentbox_check"))
        attempts = 0
        for path, payload in (("/api/outreach/jobs/12/send-email", {}),
                              ("/api/outreach/jobs/12/transition", {"status": "pending_review"})):
            status, result = self.server.request(path, "POST", payload)
            self.assertEqual(status, 200)
            self.assertFalse(result["ok"])
            attempts += 1
        self.assertGreater(attempts, 0)
        self.assertEqual(self.query("SELECT status FROM outreach_jobs WHERE id=12"), [("unknown",)])
        decision = self.server.request("/api/outreach/jobs/12/transition", "POST", {"status": "accepted", "reason": "synthetic sentbox checked"})[1]
        self.assertTrue(decision["ok"])
        self.assertEqual(decision["item"]["status"], "accepted")
        self.assert_safety()

    def test_restore_with_missing_memory_preserves_local_suppression(self):
        incoming = make_business_fixture(self.root / "incoming")
        (incoming.data_dir / "tradewind_memory.db").unlink()
        archive = make_archive(incoming, self.root / "missing-memory.zip", photos=False).read_bytes()
        self.local_suppression()
        self.server.prepare(archive, "task9-missing")
        self.server.restart()
        self.assertEqual(self.query("SELECT count(*) FROM outreach_jobs"), [(0,)])
        self.assertTrue((self.layout.data_dir / "crawler_photos/合成甲店/01.gif").is_file())
        self.server.request("/api/customers/customer-甲", "PUT", {"name": "合成甲店", "email": "local-only@shop.example"})
        result = self.server.request("/api/outreach/jobs", "POST", {"customer_id": "customer-甲", "channel": "email", "content": "Subject: Demo\n\n" + BODY})[1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "suppressed")
        self.assert_safety()

    def test_complete_response_disconnect_never_announces_partial_success(self):
        prepared = self.server.prepare(self.server.export(True), "task9-stream")
        self.server.request("/api/backup/restore/cancel", "POST", {"operation_id": prepared["operation_id"]})
        path = "/api/backup/rescues/" + prepared["rescue_id"]
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        connection.request("GET", path + "/download", headers={"X-Task9-Slow-Stream": "1"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        first = response.read(64)
        total = int(response.getheader("Content-Length"))
        wait_until(lambda: (self.layout.data_dir.parent / "stream-started").exists())
        self.assertLess(len(first), total)
        status, result = self.server.request(path, "DELETE")
        self.assertEqual((status, result["code"]), (503, "maintenance_busy"))
        response.close()
        connection.close()
        ended = self.layout.data_dir.parent / "stream-ended.json"
        wait_until(ended.exists)
        self.assertFalse(json.loads(ended.read_text())["complete"])
        status, result = self.server.request(path, "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(self.server.request(path + "/download")[0], 404)
        self.assert_safety_without_suppression()

    def assert_safety_without_suppression(self):
        self.assertEqual((self.layout.data_dir / "config.json").read_bytes(), self.config_bytes)
        evidence = self.server.request("/__task9/evidence")[1]
        self.assertEqual(evidence["smtp_calls"], 0)
        self.assertEqual(evidence["model_calls"], 0)
        self.assertEqual(evidence["external_requests"], [])


class IsolationRunnerTests(unittest.TestCase):
    def test_real_environment_paths_and_keys_are_stripped_before_config_import(self):
        env = dict(os.environ, DEEPSEEK_API_KEY="synthetic-tainted", MEMORY_DB_PATH="forbidden-synthetic",
                   LOCAL_KB_PATH="forbidden-synthetic", PYTHONPATH="forbidden-synthetic",
                   TRADEWIND_DATA_DIR="forbidden-synthetic", TRADEWIND_NO_BROWSER="0")
        # This is the runner itself, not a product child. Parent's fence wraps
        # only -c children; execute through -c to keep inherited fencing intact.
        command = [sys.executable, "-B", "-c", "import runpy,sys; sys.argv=['test-backup-core.py','--probe']; runpy.run_path('scripts/test-backup-core.py',run_name='__main__')"]
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"owned_config": True, "keys_absent": True, "paths_absent": True, "no_browser": True, "neutral_browser_env": True})

    def test_fence_rejects_external_dns_live_port_and_unowned_loopback(self):
        for host, port in (("provider.example", 443), ("127.0.0.1", 8101), ("127.0.0.1", 1)):
            with self.subTest(host=host, port=port), self.assertRaises(PermissionError):
                socket.getaddrinfo(host, port)

    def test_crash_matrix_bootstrap_imports_argv_and_env_ignore_no_browser(self):
        """Actual unchanged matrix child programs, no 300-trial matrix rerun."""
        import ast
        import inspect
        import shutil
        from test_backup_crash_recovery import CrashRecoveryTests
        fixture = CrashRecoveryTests("test_epoch_absence_is_restored_on_rollback")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.old_state()
        fixture.prepare(validated=fixture.validated(photos=True))
        forward = fixture.root / "flag-forward-seed"
        shutil.copytree(fixture.layout.data_dir.parent, forward)
        fixture.interrupt_before_commit()
        rollback = fixture.root / "flag-rollback-seed"
        shutil.copytree(fixture.layout.data_dir.parent, rollback)
        methods = (
            ("test_every_journal_boundary_recovers_whole_old_or_committed_new_state", forward, ["-1", "exception"], "restored"),
            ("test_rollback_resumes_after_force_exit_restoring_original_and_undo_publication", rollback, ["-1"], "rolled_back"),
        )
        tree = ast.parse(Path(inspect.getsourcefile(CrashRecoveryTests)).read_text(encoding="utf-8"))
        matrix_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                            and node.name == "CrashRecoveryTests")
        for name, seed, argv, status in methods:
            # Execute the literal program actually shipped in the matrix test,
            # not a rewritten approximation; only the probe tail observes it.
            method = next(node for node in matrix_class.body if isinstance(node, ast.FunctionDef) and node.name == name)
            assignment = next(node for node in ast.walk(method) if isinstance(node, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == "code" for target in node.targets))
            code = ast.literal_eval(assignment.value)
            probe = "\nprint(__import__('json').dumps({'flag':os.environ['TRADEWIND_NO_BROWSER'],'argv':sys.argv[2:],'business_imports':[n for n in ('app.config','app.memory','server','run') if n in sys.modules],'status':r.apply_pending_restore(layout,memory_path=layout.data_dir/'tradewind_memory.db',limits=BackupLimits(space_margin_bytes=0)).status}))"
            for flag in ("0", "1"):
                with self.subTest(program=name, flag=flag):
                    target = fixture.root / ("flag-" + status + "-" + flag)
                    shutil.copytree(seed, target)
                    result = subprocess.run([sys.executable, "-B", "-c", code + probe, str(target), *argv],
                                            env={**os.environ, "TRADEWIND_NO_BROWSER": flag},
                                            capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    observed = json.loads(result.stdout.splitlines()[-1])
                    self.assertEqual(observed, {"flag": flag, "argv": argv, "business_imports": [], "status": status})
