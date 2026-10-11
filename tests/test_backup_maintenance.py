"""Maintenance behavior on owned synthetic roots; no live runtime imports."""

from contextlib import contextmanager
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import unicodedata
import uuid

from app.backup.models import BackupError
from app.backup.paths import resolve_layout


PERSISTED_EPOCH = "12345678-1234-4234-8234-123456789abc"
INVALID_MESSAGE = "数据版本文件无效，请保留本机资料并联系维护人员。"


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        # Turn the missing feature into an explicit RED assertion, not a
        # discovery/import error; exercise the real implementation once present.
        try:
            self.module = importlib.import_module("app.backup.maintenance")
        except ModuleNotFoundError as error:
            if error.name != "app.backup.maintenance":
                raise
            self.fail("Task3 maintenance coordinator is not implemented")
        temporary = tempfile.TemporaryDirectory(prefix="tw-maintenance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.layout = resolve_layout(self.root)
        self.coordinator = self.module.MaintenanceCoordinator(self.layout)

    @contextmanager
    def error_code(self, code):
        with self.assertRaises(BackupError) as caught:
            yield
        self.assertEqual(caught.exception.code, code)

    def write_epoch(self, require_header=True):
        self.layout.data_dir.mkdir(exist_ok=True)
        self.layout.epoch_file.write_text(json.dumps({
            "format_version": 1, "epoch": PERSISTED_EPOCH,
            "require_header": require_header,
        }), encoding="utf-8")
        return self.module.MaintenanceCoordinator(self.layout)

    def start_thread(self, target):
        errors = []

        def guarded():
            try:
                target()
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=guarded, daemon=True)
        thread.start()
        return thread, errors

    def finish_thread(self, thread, errors):
        thread.join(5)
        self.assertFalse(thread.is_alive(), "worker did not finish")
        self.assertEqual(errors, [])

    def assert_ready(self):
        self.assertEqual(self.coordinator.mode, "ready")
        with self.coordinator.exclusive("snapshot") as lease:
            lease.assert_active()

    def test_active_request_or_worker_rejects_exclusive_without_wait(self):
        with self.coordinator.service(None, mutating=False):
            with self.error_code("maintenance_busy"):
                with self.coordinator.exclusive("snapshot"):
                    self.fail("active request must reject maintenance")
        ticket = self.coordinator.reserve_worker("photo", mutating=True)
        try:
            with self.error_code("maintenance_busy"):
                self.coordinator.reserve_exclusive("prepare")
        finally:
            ticket.abort_start()
        self.assert_ready()

    def test_worker_reservation_survives_parent_http_completion(self):
        with self.coordinator.service(None, mutating=True):
            ticket = self.coordinator.reserve_worker("photo", mutating=True)
        try:
            with self.error_code("maintenance_busy"):
                self.coordinator.reserve_exclusive("snapshot")
            ticket.run(lambda: None)
        finally:
            ticket.abort_start()
        self.assert_ready()

    def test_ui_terminal_status_does_not_release_real_worker(self):
        entered, terminal, finish = threading.Event(), threading.Event(), threading.Event()
        status = {"value": "running"}
        ticket = self.coordinator.reserve_worker("ocr", mutating=True)

        def worker():
            entered.set()
            status["value"] = "cancelled"
            terminal.set()
            self.assertTrue(finish.wait(5))
            # Cleanup can still write after the UI has reported cancellation.
            self.layout.data_dir.mkdir(exist_ok=True)
            (self.layout.data_dir / "products.json").write_bytes(b"[]")

        thread, errors = self.start_thread(lambda: ticket.run(worker))
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(terminal.wait(5))
            self.assertEqual(status["value"], "cancelled")
            ticket.abort_start()  # Too late to abort a running worker.
            ticket.run(lambda: self.fail("ticket must not run twice"))
            with self.error_code("maintenance_busy"):
                self.coordinator.reserve_exclusive("snapshot")
        finally:
            finish.set()
            self.finish_thread(thread, errors)
        self.assertEqual((self.layout.data_dir / "products.json").read_bytes(), b"[]")
        self.assert_ready()

    def test_worker_start_failure_releases_ticket(self):
        ticket = self.coordinator.reserve_worker("photo", mutating=True)
        # Fault injection at the external thread-start boundary only.
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("start failed")):
            with self.assertRaises(RuntimeError):
                try:
                    threading.Thread(target=ticket.run, args=(lambda: None,)).start()
                except BaseException:
                    ticket.abort_start()
                    raise
        ticket.abort_start()
        ticket.run(lambda: self.fail("aborted ticket must not invoke target"))
        self.assertEqual(self.coordinator.version()[1], 2)
        self.assert_ready()

    def test_creation_race_has_one_winner(self):
        for unused in range(12):
            gate = threading.Barrier(3)
            attempted = threading.Barrier(3)
            release = threading.Event()
            results = []

            def compete(worker):
                gate.wait(5)
                resource = None
                try:
                    resource = (self.coordinator.reserve_worker("photo", mutating=True)
                                if worker else self.coordinator.reserve_exclusive("snapshot"))
                    results.append("worker" if worker else "snapshot")
                except BackupError as error:
                    results.append(error.code)
                finally:
                    attempted.wait(5)
                    self.assertTrue(release.wait(5))
                    if resource is not None:
                        resource.abort_start() if worker else resource.release()

            first, first_errors = self.start_thread(lambda: compete(True))
            second, second_errors = self.start_thread(lambda: compete(False))
            try:
                gate.wait(5)
                attempted.wait(5)
                self.assertEqual(results.count("maintenance_busy"), 1)
                self.assertEqual(len(results), 2)
                self.assertEqual(sum(item in {"worker", "snapshot"} for item in results), 1)
            finally:
                release.set()
                self.finish_thread(first, first_errors)
                self.finish_thread(second, second_errors)
            self.assert_ready()

    def test_revision_changes_on_possible_partial_write_and_worker_finish(self):
        self.assertEqual(self.coordinator.version()[1], 0)
        with self.assertRaisesRegex(RuntimeError, "partial write"):
            with self.coordinator.service(None, mutating=True):
                self.assertEqual(self.coordinator.version()[1], 1)
                self.layout.data_dir.mkdir()
                (self.layout.data_dir / "customers.json").write_bytes(b"[")
                raise RuntimeError("partial write")
        self.assertEqual(self.coordinator.version()[1], 2)
        ticket = self.coordinator.reserve_worker("photo", mutating=True)
        self.assertEqual(self.coordinator.version()[1], 3)
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            ticket.run(lambda: (_ for _ in ()).throw(RuntimeError("worker failed")))
        self.assertEqual(self.coordinator.version()[1], 4)
        ticket.abort_start()
        ticket.run(lambda: self.fail("finished target repeated"))
        self.assertEqual(self.coordinator.version()[1], 4)
        self.assert_ready()

    def test_read_leases_do_not_invalidate_their_own_version(self):
        before = self.coordinator.version()
        with self.coordinator.service(None, mutating=False):
            self.assertEqual(self.coordinator.version(), before)
        ticket = self.coordinator.reserve_worker("read", mutating=False)
        ticket.run(lambda: self.assertEqual(self.coordinator.version(), before))
        with self.coordinator.exclusive("snapshot"):
            self.assertEqual(self.coordinator.version(), before)
        self.assertEqual(self.coordinator.version(), before)

    def test_normal_business_leases_run_in_parallel(self):
        entered, release = threading.Event(), threading.Event()

        def other_request():
            with self.coordinator.service(None, mutating=True):
                ticket = self.coordinator.reserve_worker("photo", mutating=True)
                try:
                    entered.set()
                    self.assertTrue(release.wait(5))
                finally:
                    ticket.abort_start()

        with self.coordinator.service(None, mutating=False):
            thread, errors = self.start_thread(other_request)
            try:
                self.assertTrue(entered.wait(5))
            finally:
                release.set()
                self.finish_thread(thread, errors)
        self.assert_ready()

    def test_exclusive_lease_can_be_handed_to_internal_thread(self):
        lease = self.coordinator.reserve_exclusive("snapshot")
        self.assertEqual(self.coordinator.mode, "snapshot")

        def capture():
            lease.assert_active()
            with self.error_code("maintenance_busy"):
                self.coordinator.reserve_exclusive("snapshot")
            with self.error_code("maintenance_busy"):
                with self.coordinator.service(None, mutating=False):
                    self.fail("exclusive must exclude reads")
            with self.error_code("maintenance_busy"):
                self.coordinator.reserve_worker("photo", mutating=True)
            lease.release()

        thread, errors = self.start_thread(capture)
        self.finish_thread(thread, errors)
        lease.release()
        with self.error_code("maintenance_busy"):
            lease.assert_active()
        self.assert_ready()

    def test_stale_lease_cannot_release_new_generation_or_other_coordinator(self):
        old = self.coordinator.reserve_exclusive("snapshot")
        old.release()
        other = self.module.MaintenanceCoordinator(self.layout)
        current = self.coordinator.reserve_exclusive("snapshot")
        foreign = other.reserve_exclusive("snapshot")
        try:
            old.release()
            with self.error_code("maintenance_busy"):
                old.assert_active()
            current.assert_active()
            foreign.assert_active()
            current.release()
            foreign.assert_active()
        finally:
            current.release()
            foreign.release()
        self.assert_ready()

    def test_prepare_lease_does_not_invalidate_its_bound_preview_revision(self):
        # Prepare validates a preview before writing control/rescue metadata;
        # reserving maintenance is not itself a business-data mutation.
        before = self.coordinator.version()
        with self.assertRaises(RuntimeError):
            with self.coordinator.exclusive("prepare"):
                self.assertEqual(self.coordinator.version(), before)
                raise RuntimeError("prepare control write failed")
        self.assertEqual(self.coordinator.version(), before)
        self.assert_ready()

    def test_prepared_blocks_business_but_can_cancel(self):
        epoch = self.coordinator.version()[0]
        lease = self.coordinator.reserve_exclusive("prepare")
        self.coordinator.hold_prepared("operation-1")
        self.coordinator.hold_prepared("operation-1")
        lease.release()
        self.assertEqual(self.coordinator.mode, "prepared")
        self.assertEqual(self.coordinator.prepared_operation_id, "operation-1")
        with self.error_code("maintenance_busy"):
            with self.coordinator.service(None, mutating=True):
                self.fail("prepared must block business")
        with self.error_code("maintenance_busy"):
            self.coordinator.reserve_worker("photo", mutating=False)
        with self.error_code("maintenance_busy"):
            self.coordinator.reserve_exclusive("snapshot")
        with self.error_code("maintenance_busy"):
            self.coordinator.cancel_prepared("wrong-operation")
        self.assertEqual(self.coordinator.prepared_operation_id, "operation-1")
        self.coordinator.cancel_prepared("operation-1")
        self.coordinator.cancel_prepared("operation-1")
        self.assertIsNone(self.coordinator.prepared_operation_id)
        self.assertEqual(self.coordinator.version()[0], epoch)
        self.assert_ready()

    def test_exclusive_exit_retains_prepared_hold(self):
        with self.coordinator.exclusive("prepare") as lease:
            self.coordinator.hold_prepared("operation-1")
            lease.assert_active()
        self.assertEqual(self.coordinator.mode, "prepared")
        with self.error_code("maintenance_busy"):
            self.coordinator.reserve_exclusive("prepare")
        self.coordinator.cancel_prepared("operation-1")
        self.assert_ready()

    def test_cancel_prepared_does_not_release_still_active_exclusive(self):
        with self.coordinator.exclusive("prepare") as lease:
            self.coordinator.hold_prepared("operation-1")
            self.coordinator.cancel_prepared("operation-1")
            self.assertEqual(self.coordinator.mode, "prepare")
            lease.assert_active()
            with self.error_code("maintenance_busy"):
                with self.coordinator.service(None, mutating=False):
                    self.fail("cancel cannot release active lease")
        self.assert_ready()

    def test_prepared_hold_requires_prepare_and_cannot_be_replaced(self):
        with self.error_code("maintenance_busy"):
            self.coordinator.hold_prepared("operation-1")
        with self.coordinator.exclusive("snapshot"):
            with self.error_code("maintenance_busy"):
                self.coordinator.hold_prepared("operation-1")
        with self.coordinator.exclusive("prepare"):
            self.coordinator.hold_prepared("operation-1")
            with self.error_code("maintenance_busy"):
                self.coordinator.hold_prepared("operation-2")
        self.coordinator.cancel_prepared("operation-1")
        self.assert_ready()

    def test_epoch_header_optional_before_restore_required_after_restore(self):
        initial = self.coordinator.version()[0]
        self.assertFalse(initial.require_header)
        for header in (None, initial.epoch):
            with self.coordinator.service(header, mutating=True):
                pass
        with self.error_code("stale_epoch"):
            with self.coordinator.service("old", mutating=True):
                self.fail("present mismatching header must be rejected")
        self.coordinator = self.write_epoch()
        self.assertEqual(self.coordinator.version()[0].epoch, PERSISTED_EPOCH)
        self.assertTrue(self.coordinator.version()[0].require_header)
        for header in (None, initial.epoch, ""):
            with self.error_code("stale_epoch"):
                with self.coordinator.service(header, mutating=True):
                    self.fail("missing/old restored header must be rejected")
        with self.coordinator.service(PERSISTED_EPOCH, mutating=True):
            pass
        # Read-only business requests do not require a mutation header.
        with self.coordinator.service(None, mutating=False):
            pass
        self.assert_ready()

    def test_failed_epoch_check_does_not_allocate_lease(self):
        self.coordinator = self.write_epoch()
        before = self.coordinator.version()
        for header in (None, "old"):
            with self.error_code("stale_epoch"):
                with self.coordinator.service(header, mutating=True):
                    self.fail("stale request reached business")
            self.assertEqual(self.coordinator.version(), before)
            self.assert_ready()

    def test_ordinary_restart_stable_initial_epoch_and_different_roots(self):
        initial = self.coordinator.version()[0]
        # Independently spell the binding formula, not a production helper.
        expected = str(uuid.uuid5(uuid.NAMESPACE_URL, "tradewind-initial:" +
                       unicodedata.normalize("NFC", str(self.layout.data_dir)).casefold()))
        self.assertEqual(initial.epoch, expected)
        self.assertEqual(uuid.UUID(initial.epoch).version, 5)
        second = self.module.MaintenanceCoordinator(self.layout)
        self.assertEqual(second.version()[0], initial)
        other = self.module.MaintenanceCoordinator(resolve_layout(self.root / "other"))
        self.assertNotEqual(other.version()[0].epoch, initial.epoch)
        result = subprocess.run([
            sys.executable, "-B", "-c",
            "from pathlib import Path; from app.backup.paths import resolve_layout; "
            "from app.backup.maintenance import MaintenanceCoordinator; import sys; "
            "print(MaintenanceCoordinator(resolve_layout(Path(sys.argv[1]))).version()[0].epoch)",
            str(self.root),
        ], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), initial.epoch)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_initial_epoch_normalizes_root_nfc_and_casefold(self):
        upper = self.module.MaintenanceCoordinator(resolve_layout(self.root / "CAFÉ"))
        decomposed = self.module.MaintenanceCoordinator(resolve_layout(self.root / "cafe\u0301"))
        self.assertEqual(upper.version()[0], decomposed.version()[0])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_valid_persisted_false_is_accepted_without_rewriting(self):
        coordinator = self.write_epoch(False)
        before = self.layout.epoch_file.read_bytes()
        self.assertEqual(coordinator.version()[0].epoch, PERSISTED_EPOCH)
        self.assertFalse(coordinator.version()[0].require_header)
        with coordinator.service(None, mutating=True):
            pass
        self.assertEqual(self.layout.epoch_file.read_bytes(), before)

    def test_strict_bad_epoch_metadata_refuses_construction_without_overwrite(self):
        self.layout.data_dir.mkdir()
        good = {"format_version": 1, "epoch": PERSISTED_EPOCH, "require_header": True}
        bad_values = [[], None, {}, {**good, "extra": 0},
                      {key: value for key, value in good.items() if key != "require_header"}]
        for key, values in (
            ("format_version", [True, 1.0, "1", 2, None]),
            ("require_header", [1, 0, "true", None]),
            ("epoch", [None, 1, "", "not-a-uuid", PERSISTED_EPOCH.upper(),
                       PERSISTED_EPOCH.replace("-", ""), "{" + PERSISTED_EPOCH + "}",
                       "urn:uuid:" + PERSISTED_EPOCH]),
        ):
            bad_values.extend({**good, key: value} for value in values)
        payloads = [json.dumps(value).encode() for value in bad_values]
        payloads += [b"", b"{", b"\xff", b"[" * 1024,
                     ('{"format_version":1,"epoch":"' + PERSISTED_EPOCH +
                      '","require_header":true,"require_header":false}').encode(),
                     b'{"format_version":1,"format_version":1,"epoch":"' +
                     PERSISTED_EPOCH.encode() + b'","require_header":true}']
        for payload in payloads:
            with self.subTest(payload=payload):
                self.layout.epoch_file.write_bytes(payload)
                with self.error_code("invalid_epoch_state"):
                    self.module.MaintenanceCoordinator(self.layout)
                self.assertEqual(self.layout.epoch_file.read_bytes(), payload)

    def test_epoch_size_limit_counts_actual_bytes_and_bounds_read(self):
        self.write_epoch()
        original = self.layout.epoch_file.read_bytes()
        self.layout.epoch_file.write_bytes(original + b" " * (1024 - len(original)))
        self.module.MaintenanceCoordinator(self.layout)
        self.layout.epoch_file.write_bytes(original + b" " * (1025 - len(original)))
        actual_open = Path.open
        read_sizes = []

        class BoundedReader:
            def __init__(inner, stream):
                inner.stream = stream

            def __enter__(inner):
                return inner

            def __exit__(inner, *args):
                inner.stream.close()

            def read(inner, size=-1):
                read_sizes.append(size)
                return inner.stream.read(size)

        def opening(path, *args, **kwargs):
            stream = actual_open(path, *args, **kwargs)
            return BoundedReader(stream) if path == self.layout.epoch_file else stream

        with patch.object(Path, "open", opening):
            with self.error_code("invalid_epoch_state"):
                self.module.MaintenanceCoordinator(self.layout)
        self.assertEqual(read_sizes, [1025])

    def test_epoch_unreadable_and_directory_refuse_with_safe_message(self):
        self.write_epoch()
        actual_open = Path.open

        def opening(path, *args, **kwargs):
            if path == self.layout.epoch_file:
                raise PermissionError("synthetic-private-path")
            return actual_open(path, *args, **kwargs)

        with patch.object(Path, "open", opening):
            with self.assertRaises(BackupError) as caught:
                self.module.MaintenanceCoordinator(self.layout)
        self.assertEqual(caught.exception.code, "invalid_epoch_state")
        self.assertEqual(str(caught.exception), INVALID_MESSAGE)
        self.assertNotIn(str(self.root), str(caught.exception))
        self.layout.epoch_file.unlink()
        self.layout.epoch_file.mkdir()
        with self.error_code("invalid_epoch_state"):
            self.module.MaintenanceCoordinator(self.layout)

    def test_epoch_read_failure_cannot_fall_back_to_initial_version(self):
        self.write_epoch()
        before = self.layout.epoch_file.read_bytes()
        actual_open = Path.open

        class BrokenReader:
            def __enter__(inner):
                return inner

            def __exit__(inner, *args):
                return False

            def read(inner, size):
                raise FileNotFoundError("synthetic-private-marker")

        def opening(path, *args, **kwargs):
            if path == self.layout.epoch_file:
                return BrokenReader()
            return actual_open(path, *args, **kwargs)

        with patch.object(Path, "open", opening):
            with self.error_code("invalid_epoch_state"):
                self.module.MaintenanceCoordinator(self.layout)
        self.assertEqual(self.layout.epoch_file.read_bytes(), before)

    def test_epoch_path_helper_errors_remain_sanitized(self):
        with patch.object(Path, "lstat", side_effect=PermissionError("synthetic-private-marker")):
            with self.assertRaises(BackupError) as caught:
                self.module.MaintenanceCoordinator(self.layout)
        self.assertEqual(caught.exception.code, "path_unavailable")
        self.assertNotIn("synthetic-private-marker", str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_lease_identity_rejects_forged_coordinator_binding(self):
        lease = self.coordinator.reserve_exclusive("snapshot")
        other = self.module.MaintenanceCoordinator(self.layout)
        foreign = other.reserve_exclusive("snapshot")
        try:
            # A copied capability with a plausible generation is not the
            # coordinator's registered exclusive identity.
            forged = self.module.MaintenanceLease(other, foreign._generation, "snapshot")
            with self.error_code("maintenance_busy"):
                forged.assert_active()
            with self.error_code("maintenance_busy"):
                forged.release()
            lease.assert_active()
            foreign.assert_active()
        finally:
            lease.release()
            foreign.release()

    def test_epoch_path_and_reparse_safety_fail_before_read(self):
        self.write_epoch()
        outside = self.root / "outside.json"
        outside.write_bytes(b"do not read or alter")
        with self.error_code("unsafe_path"):
            self.module.MaintenanceCoordinator(replace(self.layout, epoch_file=outside))
        actual_lstat = Path.lstat
        for flagged in (self.layout.epoch_file, self.layout.data_dir):
            def flagged_lstat(path, *args, **kwargs):
                value = actual_lstat(path, *args, **kwargs)
                if path == flagged:
                    return SimpleNamespace(st_mode=value.st_mode,
                                           st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                return value

            with self.subTest(flagged=flagged), patch.object(Path, "lstat", flagged_lstat):
                with self.error_code("unsafe_path"):
                    self.module.MaintenanceCoordinator(self.layout)
        self.assertEqual(outside.read_bytes(), b"do not read or alter")

    def test_data_root_junction_introduced_after_layout_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        junction = self.layout.data_dir
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(junction.rmdir)
        else:
            junction.symlink_to(outside, target_is_directory=True)
            self.addCleanup(junction.unlink)
        with self.error_code("unsafe_path"):
            self.module.MaintenanceCoordinator(self.layout)
        self.assertEqual(list(outside.iterdir()), [])

    def test_import_maintenance_does_not_load_runtime_initializers(self):
        result = subprocess.run([sys.executable, "-B", "-c",
            "import sys; import app.backup.maintenance; "
            "assert not ({'app.config', 'app.memory', 'app.llm', 'app.email_agent', "
            "'app.credential_store', 'app.outreach_smtp', 'server'} & sys.modules.keys())"],
            capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
