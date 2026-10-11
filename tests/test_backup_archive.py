"""Synthetic behavioral tests for consistent capture and streaming export."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from backup_helpers import (business_digest, make_archive, make_business_fixture,
                            make_constructor_fixture)
from app.backup.maintenance import MaintenanceCoordinator
from app.backup.models import BackupCancelled, BackupError, BackupLimits
from app.backup.paths import resolve_layout
from app.backup.validation import validate_archive
from app.version import APP_VERSION


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tradewind-archive-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.layout = make_business_fixture(self.root / "source")
        self.coordinator = MaintenanceCoordinator(self.layout)
        self.limits = BackupLimits(space_margin_bytes=0)

    def exporter(self):
        # RED is an explicit feature assertion, not a missing-import error.
        self.assertIsNotNone(importlib.util.find_spec("app.backup.archive"),
                             "Task4 capture_snapshot/write_archive implementation is missing")
        return importlib.import_module("app.backup.archive")

    def capture(self, *, photos=False, destination=None, limits=None, cancel=lambda: False,
                progress=lambda *args: None, lease=None, layout=None):
        module = self.exporter()
        layout = layout or self.layout
        args = dict(memory_path=layout.data_dir / "tradewind_memory.db", includes_photos=photos,
                    destination=destination or self.root / "snapshot", limits=limits or self.limits,
                    cancel=cancel, progress=progress)
        if lease is not None:
            return module.capture_snapshot(layout, lease=lease, **args)
        with self.coordinator.exclusive("snapshot") as active:
            result = module.capture_snapshot(layout, lease=active, **args)
            active.assert_active()  # only the caller releases
            return result

    def write(self, snapshot, *, destination=None, limits=None, cancel=lambda: False,
              progress=lambda *args: None):
        return self.exporter().write_archive(
            snapshot, destination or self.root / "export.zip", limits=limits or self.limits,
            cancel=cancel, progress=progress)

    def roundtrip(self, snapshot):
        zipped = self.write(snapshot)
        return validate_archive(zipped, self.root / "restored", limits=self.limits,
                                cancel=lambda: False, progress=lambda *args: None).snapshot

    def test_roundtrip_all_business_tables_ids_and_sequences(self):
        before = business_digest(self.layout)
        restored = self.roundtrip(self.capture())
        self.assertEqual(restored.summary.counts, {
            "customers": 1, "products": 1, "templates": 1, "crawler_seen": 3,
            "emails": 1, "outreach_jobs": 1, "suppressions": 1, "followup_records": 1,
            "followup_deleted_requests": 1, "followup_plans": 2, "active_plans": 1, "photos": 0})
        with closing(sqlite3.connect(self.layout.data_dir / "tradewind_memory.db")) as source, \
                closing(sqlite3.connect(restored.folder / "data/tradewind_memory.db")) as target:
            for table in ("emails", "outreach_jobs", "contact_suppressions",
                          "customer_followup_records", "customer_followup_deleted_requests",
                          "customer_followup_plans", "sqlite_sequence"):
                self.assertEqual(source.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall(),
                                 target.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall())
            self.assertEqual(target.execute("SELECT seq FROM sqlite_sequence WHERE name='customer_followup_records'").fetchone(), (50,))
        self.assertEqual(before, business_digest(self.layout))

    def test_default_excludes_photos_config_logs_workflows_and_old_backups(self):
        excluded = ("config.json", ".env", "logs/private.log", "tradewind_workflows.db",
                    "backups/old.zip", "crawler_photos/private.txt", "photo_scan/private.txt")
        for index, name in enumerate(excluded):
            path = self.layout.data_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"synthetic-private-marker-{index}", encoding="utf-8")
        snapshot = self.capture()
        self.assertFalse(snapshot.summary.includes_photos)
        with zipfile.ZipFile(self.write(snapshot)) as zipped:
            self.assertEqual(zipped.namelist(), ["manifest.json", "data/crawler_seen.json",
                "data/customers.json", "data/emails.json", "data/products.json", "data/tradewind_memory.db"])
            for content in (zipped.read(name) for name in zipped.namelist()):
                for index in range(len(excluded)):
                    self.assertNotIn(f"synthetic-private-marker-{index}".encode(), content)

    def test_wal_committed_rows_captured_without_sidecars(self):
        db = self.layout.data_dir / "tradewind_memory.db"
        with closing(sqlite3.connect(db)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO emails(id,customer,email,created_at) VALUES(99,'WAL店','WAL正文',1704067200)")
            writer.commit()
            self.assertTrue(Path(str(db) + "-wal").exists())
            # SHM is SQLite's transient reader-lock index, not business data.
            # A real RO backup may update read marks; never restore those locks.
            def committed_digest():
                return {name: digest for name, digest in business_digest(self.layout).items()
                        if not name.endswith("-shm")}
            before = committed_digest()
            snapshot = self.capture()
            self.assertEqual(before, committed_digest())
            with closing(sqlite3.connect(snapshot.folder / "data/tradewind_memory.db")) as captured:
                self.assertEqual(captured.execute("SELECT email FROM emails WHERE id=99").fetchone(), ("WAL正文",))
            with zipfile.ZipFile(self.write(snapshot)) as zipped:
                self.assertFalse(any(name.endswith(("-wal", "-shm", "-journal")) for name in zipped.namelist()))
            self.assertEqual(before, committed_digest())

    def test_empty_and_missing_json_recorded_without_live_initialization(self):
        layout = resolve_layout(self.root / "empty")
        layout.data_dir.mkdir(parents=True)
        (layout.data_dir / "customers.json").write_text("[]", encoding="utf-8")
        before = business_digest(layout)
        snapshot = self.capture(layout=layout)
        self.assertEqual(snapshot.summary.items_present, {"customers": True, "products": False,
            "templates": False, "memory": False, "crawler_seen": False,
            "crawler_photos": False, "photo_scan": False})
        self.assertTrue(all(value == 0 for value in snapshot.summary.counts.values()))
        self.assertEqual(snapshot.summary.app_version, APP_VERSION)
        self.assertEqual(datetime.fromisoformat(snapshot.summary.created_at).utcoffset(), timezone.utc.utcoffset(None))
        self.assertEqual(before, business_digest(layout))
        self.roundtrip(snapshot)
        self.assertEqual(before, business_digest(layout))

    def test_optional_photo_bytes_names_and_empty_stores_preserved(self):
        photo = self.layout.data_dir / "crawler_photos/合成甲店/01.gif"
        renamed = photo.with_name("中文照片.gif")
        photo.rename(renamed)
        restored = self.roundtrip(self.capture(photos=True))
        self.assertEqual((restored.folder / "data/crawler_photos/合成甲店/中文照片.gif").read_bytes(), renamed.read_bytes())
        self.assertTrue((restored.folder / "data/crawler_photos/空店").is_dir())
        self.assertTrue((restored.folder / "data/photo_scan").is_dir())
        self.assertEqual(restored.summary.counts["photos"], 1)
        with zipfile.ZipFile(self.root / "export.zip") as zipped:
            self.assertFalse(any(info.is_dir() for info in zipped.infolist()))

    def test_non_photo_file_is_visible_error_not_silent_skip(self):
        path = self.layout.data_dir / "crawler_photos/unknown.txt"
        path.write_text("synthetic-unknown-private-marker", encoding="utf-8")
        before = business_digest(self.layout)
        with self.assertRaises(BackupError):
            self.capture(photos=True)
        self.assertEqual(before, business_digest(self.layout))
        self.assertFalse((self.root / "snapshot").exists())

    def test_source_changes_abort_snapshot(self):
        for change in ("modify", "add", "remove"):
            with self.subTest(change=change):
                changed = False
                def progress(*args):
                    nonlocal changed
                    if changed:
                        return
                    changed = True
                    if change == "modify":
                        (self.layout.data_dir / "products.json").write_text("[]", encoding="utf-8")
                    elif change == "add":
                        (self.layout.data_dir / "crawler_photos/new.gif").write_bytes(
                            (self.layout.data_dir / "crawler_photos/合成甲店/01.gif").read_bytes())
                    else:
                        (self.layout.data_dir / "emails.json").unlink()
                destination = self.root / change
                with self.assertRaises(BackupError) as caught:
                    self.capture(photos=True, destination=destination, progress=progress)
                self.assertEqual(caught.exception.code, "source_changed")
                self.assertFalse(destination.exists())

    def test_cancel_space_error_and_zip_limit_leave_source_unchanged(self):
        before = business_digest(self.layout)
        with self.assertRaises(BackupCancelled):
            self.capture(cancel=lambda: True)
        with patch("app.backup.paths.shutil.disk_usage", return_value=(100, 100, 0)):
            with self.assertRaises(BackupError) as caught:
                self.capture()
            self.assertEqual(caught.exception.code, "insufficient_space")
        snapshot = self.capture()
        with self.assertRaises(BackupError) as caught:
            self.write(snapshot, limits=replace(self.limits, max_archive_bytes=40))
        self.assertEqual(caught.exception.code, "resource_limit")
        self.assertFalse((self.root / "export.zip").exists())
        self.assertEqual(before, business_digest(self.layout))
        self.assertEqual(set(self.root.iterdir()), {self.root / "source", snapshot.folder})

    def test_compression_runs_after_exclusive_lease_release(self):
        snapshot = self.capture()
        def progress(*args):
            with self.coordinator.service(None, mutating=True):
                (self.layout.data_dir / "customers.json").write_text("[]", encoding="utf-8")
        restored_path = self.write(snapshot, progress=progress)
        with zipfile.ZipFile(restored_path) as zipped:
            self.assertEqual(json.loads(zipped.read("data/customers.json"))[0]["id"], "customer-甲")

    def test_prepare_reuses_transferable_lease_and_does_not_release_it(self):
        with self.coordinator.exclusive("prepare") as lease:
            with ThreadPoolExecutor(max_workers=1) as worker:
                snapshot = worker.submit(self.capture, lease=lease).result(timeout=10)
            self.write(snapshot)
            lease.assert_active()
            self.assertEqual(self.coordinator.mode, "prepare")
        with self.assertRaises(BackupError):
            self.capture(destination=self.root / "stale", lease=lease)
        self.assertFalse((self.root / "stale").exists())

    def test_constructor_groups_roundtrip_without_production_initializers(self):
        for groups in ((), ("memory",), ("outreach",), ("followup",),
                       ("memory", "outreach"), ("memory", "followup"),
                       ("outreach", "followup"), ("memory", "outreach", "followup")):
            with self.subTest(groups=groups):
                name = "-".join(groups) or "empty"
                layout = make_constructor_fixture(self.root / name, groups=groups)
                memory = importlib.import_module("app.memory")
                outreach = importlib.import_module("app.outreach_store")
                followup = importlib.import_module("app.customer_followup")
                with patch.object(memory, "_connect", side_effect=AssertionError("initializer called")), \
                        patch.object(outreach, "OutreachStore", side_effect=AssertionError("initializer called")), \
                        patch.object(followup, "FollowupStore", side_effect=AssertionError("initializer called")):
                    snapshot = self.capture(layout=layout, destination=self.root / (name + "-snapshot"))
                    zipped = self.write(snapshot, destination=self.root / (name + ".zip"))
                restored = validate_archive(zipped, self.root / (name + "-restored"), limits=self.limits,
                    cancel=lambda: False, progress=lambda *args: None).snapshot
                self.assertEqual(restored.summary.counts["emails"], int("memory" in groups))
                self.assertEqual(restored.summary.counts["outreach_jobs"], int("outreach" in groups))
                self.assertEqual(restored.summary.counts["followup_records"], int("followup" in groups))

    def test_chunk_read_boundary_and_fault_cleanup(self):
        self.exporter()
        photo = self.layout.data_dir / "crawler_photos/large.gif"
        photo.write_bytes(b"GIF89a" + b"x" * (2 * 1024 * 1024 + 17))
        original_open = Path.open
        reads = []
        fail = False
        class Reader:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def read(self, size=-1):
                reads.append(size)
                if not 0 < size <= 1024 * 1024:
                    raise AssertionError("unbounded archive read")
                data = self.stream.read(size)
                if fail and len(reads) > 2:
                    raise OSError("synthetic-read-fault-private-marker")
                return data
        def opened(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return Reader(stream) if mode == "rb" else stream
        with patch.object(Path, "open", opened):
            snapshot = self.capture(photos=True)
            self.write(snapshot)
            fail = True
            reads.clear()
            with self.assertRaises(BackupError) as caught:
                self.write(snapshot, destination=self.root / "fault.zip")
            reads.clear()
            with self.assertRaises(BackupError):
                self.capture(destination=self.root / "fault-snapshot", photos=True)
        self.assertTrue(reads)
        self.assertNotIn("synthetic-read-fault", str(caught.exception))
        self.assertFalse((self.root / "fault.zip").exists())
        self.assertEqual(set(self.root.iterdir()), {self.root / "source", self.root / "snapshot", self.root / "export.zip"})

    def test_cancellation_mid_copy_and_mid_compression_cleans_only_owned_outputs(self):
        before = business_digest(self.layout)
        cancelled = False
        def progress(*args):
            nonlocal cancelled
            cancelled = True
        with self.assertRaises(BackupCancelled):
            self.capture(cancel=lambda: cancelled, progress=progress)
        self.assertFalse((self.root / "snapshot").exists())
        cancelled = False
        snapshot = self.capture()
        with self.assertRaises(BackupCancelled):
            self.write(snapshot, cancel=lambda: cancelled, progress=progress)
        self.assertEqual(before, business_digest(self.layout))
        self.assertTrue(snapshot.folder.is_dir())
        self.assertEqual(set(self.root.iterdir()), {self.root / "source", snapshot.folder})

    def test_existing_and_source_destinations_are_never_overwritten_or_deleted(self):
        self.exporter()
        before = business_digest(self.layout)
        occupied = self.root / "occupied"
        occupied.mkdir()
        (occupied / "sentinel").write_bytes(b"keep")
        for destination in (occupied, self.layout.data_dir / "crawler_photos/new-snapshot"):
            with self.assertRaises(BackupError):
                self.capture(destination=destination)
        self.assertEqual((occupied / "sentinel").read_bytes(), b"keep")
        snapshot = self.capture()
        existing = self.root / "existing.zip"
        existing.write_bytes(b"keep zip")
        for destination in (existing, snapshot.folder / "new.zip", self.layout.data_dir):
            with self.assertRaises(BackupError):
                self.write(snapshot, destination=destination)
        self.assertEqual(existing.read_bytes(), b"keep zip")
        self.assertEqual(before, business_digest(self.layout))

    def test_strict_json_and_resource_limits_refuse_publication(self):
        self.exporter()
        path = self.layout.data_dir / "customers.json"
        path.write_text('[{"id":"one","id":"two","name":"合成"}]', encoding="utf-8")
        before = business_digest(self.layout)
        with self.assertRaises(BackupError) as caught:
            self.capture()
        self.assertEqual(caught.exception.code, "invalid_json")
        self.assertEqual(before, business_digest(self.layout))
        path.write_text("[]", encoding="utf-8")
        for limits in (replace(self.limits, max_json_bytes=1), replace(self.limits, max_db_bytes=1),
                       replace(self.limits, max_photo_bytes=1), replace(self.limits, max_files=1),
                       replace(self.limits, max_unpacked_bytes=1)):
            with self.subTest(limits=limits), self.assertRaises(BackupError) as caught:
                self.capture(photos=True, limits=limits)
            self.assertEqual(caught.exception.code, "resource_limit")
            self.assertFalse((self.root / "snapshot").exists())

    def test_vacuum_scrubs_deleted_content_only_in_captured_database(self):
        self.exporter()
        db = self.layout.data_dir / "tradewind_memory.db"
        marker = "synthetic-deleted-private-marker-" * 200
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("PRAGMA secure_delete=OFF")
            conn.execute("INSERT INTO emails(id,customer,email,created_at) VALUES(101,'已删除',?,1704067200)", (marker,))
            conn.execute("DELETE FROM emails WHERE id=101")
        self.assertIn(marker[:60].encode(), db.read_bytes())
        before = business_digest(self.layout)
        snapshot = self.capture()
        self.assertNotIn(marker[:60].encode(), (snapshot.folder / "data/tradewind_memory.db").read_bytes())
        self.assertEqual(before, business_digest(self.layout))

    def test_publication_race_preserves_other_actors_file(self):
        snapshot = self.capture()
        destination = self.root / "racing.zip"
        def progress(*args):
            destination.write_bytes(b"other actor owns this")
        with self.assertRaises(BackupError) as caught:
            self.write(snapshot, destination=destination, progress=progress)
        self.assertEqual(caught.exception.code, "destination_exists")
        self.assertEqual(destination.read_bytes(), b"other actor owns this")
        self.assertFalse(list(self.root.glob(".archive-*.tmp")))

    def test_snapshot_change_during_compression_aborts_publication(self):
        snapshot = self.capture()
        def progress(*args):
            (snapshot.folder / "data/customers.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(BackupError) as caught:
            self.write(snapshot, progress=progress)
        self.assertEqual(caught.exception.code, "source_changed")
        self.assertFalse((self.root / "export.zip").exists())
        self.assertTrue(snapshot.folder.exists())
        self.assertFalse(list(self.root.glob(".archive-*.tmp")))

    def test_cleanup_refuses_substituted_capture_root(self):
        module = self.exporter()
        destination = self.root / "snapshot"
        displaced = self.root / "displaced-owned"
        def inspect(*args, **kwargs):
            destination.rename(displaced)
            destination.mkdir()
            (destination / "sentinel").write_bytes(b"other actor owns this")
            raise BackupCancelled()
        with patch.object(module, "inspect_snapshot", inspect), self.assertRaises(BackupError) as caught:
            self.capture()
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertEqual((destination / "sentinel").read_bytes(), b"other actor owns this")
        self.assertTrue(displaced.is_dir())

    def test_capture_never_returns_substituted_root(self):
        module = self.exporter()
        original_inspect = module.inspect_snapshot
        destination = self.root / "snapshot"
        def inspect(*args, **kwargs):
            summary = original_inspect(*args, **kwargs)
            destination.rename(self.root / "displaced-owned")
            destination.mkdir()
            (destination / "sentinel").write_bytes(b"other actor owns this")
            return summary
        with patch.object(module, "inspect_snapshot", inspect), self.assertRaises(BackupError) as caught:
            self.capture()
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertEqual((destination / "sentinel").read_bytes(), b"other actor owns this")

    def test_archive_never_publishes_or_deletes_substituted_temporary(self):
        snapshot = self.capture()
        displaced = self.root / "displaced.tmp"
        def progress(phase, done, total):
            if done == total:
                temporary = next(self.root.glob(".archive-*.tmp"))
                # The final progress call runs with the completed file closed.
                try:
                    temporary.rename(displaced)
                except PermissionError:
                    return
                temporary.write_bytes(b"other actor owns this")
        with self.assertRaises(BackupError) as caught:
            self.write(snapshot, progress=progress)
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertFalse((self.root / "export.zip").exists())
        self.assertEqual(next(self.root.glob(".archive-*.tmp")).read_bytes(), b"other actor owns this")

    def test_cleanup_and_capture_refuse_windows_junctions(self):
        self.exporter()
        victim = self.root / "victim"
        victim.mkdir()
        (victim / "sentinel").write_bytes(b"keep victim")
        link = self.layout.data_dir / "crawler_photos/junction"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(victim)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, "synthetic junction setup failed")
        with self.assertRaises(BackupError) as caught:
            self.capture(photos=True)
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertEqual((victim / "sentinel").read_bytes(), b"keep victim")
        link.rmdir()  # remove the junction itself, never its destination
        def progress(*args):
            junction = self.root / "snapshot/data/junction"
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(victim)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0)
            raise BackupCancelled()
        with self.assertRaises(BackupError) as caught:
            self.capture(progress=progress)
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertEqual((victim / "sentinel").read_bytes(), b"keep victim")

    def test_limits_include_manifest_and_central_directory(self):
        snapshot = self.capture()
        for limits in (replace(self.limits, max_manifest_bytes=100),
                       replace(self.limits, max_files=5),
                       replace(self.limits, max_unpacked_bytes=sum(
                           path.stat().st_size for path in (snapshot.folder / "data").iterdir()))):
            with self.subTest(limits=limits), self.assertRaises(BackupError) as caught:
                self.write(snapshot, limits=limits)
            self.assertEqual(caught.exception.code, "resource_limit")
            self.assertFalse((self.root / "export.zip").exists())
        zipped = self.write(snapshot)
        with self.assertRaises(BackupError) as caught:
            self.write(snapshot, destination=self.root / "one-byte-short.zip",
                       limits=replace(self.limits, max_archive_bytes=zipped.stat().st_size - 1))
        self.assertEqual(caught.exception.code, "resource_limit")
        self.assertFalse((self.root / "one-byte-short.zip").exists())

    def test_snapshot_changed_during_validation_is_not_exported(self):
        module = self.exporter()
        snapshot = self.capture()
        original_inspect = module.inspect_snapshot
        def inspect(*args, **kwargs):
            summary = original_inspect(*args, **kwargs)
            (snapshot.folder / "data/customers.json").write_text("[]", encoding="utf-8")
            return summary
        with patch.object(module, "inspect_snapshot", inspect):
            with self.assertRaises(BackupError) as caught:
                self.write(snapshot)
        self.assertEqual(caught.exception.code, "source_changed")
        self.assertFalse((self.root / "export.zip").exists())

    def test_cancel_during_sqlite_backup_leaves_committed_source_unchanged(self):
        before = business_digest(self.layout)
        cancelled = False
        def progress(phase, done, total):
            nonlocal cancelled
            if (self.root / "snapshot/data/tradewind_memory.db").exists():
                cancelled = True
        with self.assertRaises(BackupCancelled):
            self.capture(cancel=lambda: cancelled, progress=progress)
        self.assertEqual(before, business_digest(self.layout))
        self.assertFalse((self.root / "snapshot").exists())

    def test_earliest_seven_column_legacy_is_preserved_separately(self):
        layout = make_constructor_fixture(self.root / "legacy", groups=())
        with closing(sqlite3.connect(layout.data_dir / "tradewind_memory.db")) as conn, conn:
            # Historical literal fixture, not derived from exporter/validator DDL.
            conn.execute("CREATE TABLE emails(id INTEGER PRIMARY KEY AUTOINCREMENT,customer TEXT NOT NULL,country TEXT DEFAULT '',product TEXT DEFAULT '',email TEXT NOT NULL,score REAL DEFAULT 0,created_at REAL NOT NULL)")
            conn.execute("INSERT INTO emails(id,customer,email,created_at) VALUES(23,'合成旧店','旧文案',1704067200)")
            conn.execute("UPDATE sqlite_sequence SET seq=80 WHERE name='emails'")
        before = business_digest(layout)
        restored = self.roundtrip(self.capture(layout=layout))
        with closing(sqlite3.connect(restored.folder / "data/tradewind_memory.db")) as conn:
            self.assertEqual(conn.execute("SELECT * FROM emails").fetchall(),
                             [(23, "合成旧店", "", "", "旧文案", 0.0, 1704067200.0)])
            self.assertEqual(conn.execute("SELECT seq FROM sqlite_sequence WHERE name='emails'").fetchone(), (80,))
            self.assertEqual(len(conn.execute("PRAGMA table_info(emails)").fetchall()), 7)
        self.assertEqual(before, business_digest(layout))

    def test_archive_preserves_validated_manifest_metadata(self):
        original = make_archive(self.layout, self.root / "fixture.zip", photos=True)
        snapshot = validate_archive(original, self.root / "validated", limits=self.limits,
            cancel=lambda: False, progress=lambda *args: None).snapshot
        with zipfile.ZipFile(self.write(snapshot)) as zipped:
            manifest = json.loads(zipped.read("manifest.json"))
        self.assertEqual(manifest["created_at"], "2026-10-07T00:00:00Z")
        self.assertEqual(manifest["app_version"], "0.2.0")
        self.assertEqual(manifest["counts"]["photos"], 1)

    def test_program_control_destination_and_missing_data_root(self):
        self.layout.work_dir.mkdir()
        before = business_digest(self.layout)
        snapshot = self.capture(destination=self.layout.work_dir / "operation-snapshot")
        self.write(snapshot, destination=self.layout.work_dir / "operation.zip")
        self.assertEqual(before, business_digest(self.layout))
        layout = resolve_layout(self.root / "never-initialized")
        snapshot = self.capture(layout=layout, destination=self.root / "missing-snapshot")
        self.assertFalse(layout.data_dir.exists())
        self.assertTrue(all(not value for value in snapshot.summary.items_present.values()))
        self.write(snapshot, destination=self.root / "missing.zip")
        self.assertFalse(layout.data_dir.exists())

    def test_compression_space_error_preserves_snapshot_and_destination_neighbors(self):
        snapshot = self.capture()
        sentinel = self.root / "neighbor"
        sentinel.write_bytes(b"keep neighbor")
        before = business_digest(self.layout)
        with patch("app.backup.paths.shutil.disk_usage", return_value=(100, 100, 0)):
            with self.assertRaises(BackupError) as caught:
                self.write(snapshot)
        self.assertEqual(caught.exception.code, "insufficient_space")
        self.assertTrue(snapshot.folder.is_dir())
        self.assertEqual(sentinel.read_bytes(), b"keep neighbor")
        self.assertEqual(before, business_digest(self.layout))
        self.assertFalse((self.root / "export.zip").exists())
        self.assertFalse(list(self.root.glob(".archive-*.tmp")))

    def test_late_source_inventory_fault_has_only_safe_public_error(self):
        self.exporter()
        before = business_digest(self.layout)
        original_iterdir = Path.iterdir
        fault = False
        def progress(*args):
            nonlocal fault
            fault = True
        def iterdir(path):
            if fault and path == self.layout.data_dir / "crawler_photos":
                raise OSError("synthetic-inventory-private-marker")
            return original_iterdir(path)
        caught = None
        with patch.object(Path, "iterdir", iterdir):
            try:
                self.capture(photos=True, progress=progress)
            except Exception as error:
                caught = error
        self.assertIsInstance(caught, BackupError)
        self.assertNotIn("synthetic-inventory-private-marker", str(caught))
        self.assertEqual(before, business_digest(self.layout))
        self.assertFalse((self.root / "snapshot").exists())

    def test_capture_destination_race_is_refused_without_cleanup_authority(self):
        module = self.exporter()
        destination = self.root / "snapshot"
        original_space = module.require_space
        def require_space(*args):
            original_space(*args)
            destination.mkdir()
            (destination / "sentinel").write_bytes(b"other actor owns this")
        with patch.object(module, "require_space", require_space), self.assertRaises(BackupError) as caught:
            self.capture()
        self.assertEqual(caught.exception.code, "destination_exists")
        self.assertEqual((destination / "sentinel").read_bytes(), b"other actor owns this")


if __name__ == "__main__":
    unittest.main()
