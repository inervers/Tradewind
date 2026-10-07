from __future__ import annotations

from dataclasses import replace
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import re
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app.backup.models import BackupCancelled, BackupError, BackupLimits
from app.backup.paths import resolve_layout
from backup_helpers import business_digest, make_archive, make_business_fixture


class BackupValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tw-validation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.layout = make_business_fixture(self.root / "source")
        self.archive = make_archive(self.layout, self.root / "input.zip", photos=True)
        self.destination = self.root / "staged"
        self.before = business_digest(self.layout)

    def validate(self, *, limits=None, cancel=lambda: False, progress=lambda *args: None):
        self.assertIsNotNone(importlib.util.find_spec("app.backup.validation"), "strict staged validator is not implemented")
        validator = importlib.import_module("app.backup.validation")
        return validator.validate_archive(self.archive, self.destination, limits=limits or BackupLimits(),
                                          cancel=cancel, progress=progress)

    def rewrite(self, change):
        with zipfile.ZipFile(self.archive) as archive:
            entries = [(info, archive.read(info)) for info in archive.infolist()]
        manifest = json.loads(entries[0][1])
        entries = change(manifest, entries)
        entries[0] = ("manifest.json", json.dumps(manifest, ensure_ascii=False).encode())
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, data in entries:
                archive.writestr(name, data)

    def replace_content(self, name, content):
        def change(manifest, entries):
            for entry in manifest["files"]:
                if entry["path"] == name:
                    entry.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
            return [(info, content if info.filename == name else data) for info, data in entries]
        self.rewrite(change)

    def reject(self, *, limits=None, cancel=lambda: False, error=BackupError):
        with self.assertRaises(error) as caught:
            self.validate(limits=limits, cancel=cancel)
        self.assertFalse(self.destination.exists())
        self.assertEqual(business_digest(self.layout), self.before)
        return caught.exception

    def update_db(self, sql):
        with closing(sqlite3.connect(self.layout.data_dir / "tradewind_memory.db")) as conn, conn:
            conn.executescript(sql)
        self.archive = make_archive(self.layout, self.archive, photos=True)
        self.before = business_digest(self.layout)

    def test_valid_archive_preserves_ids_counts_and_empty_directories(self):
        # Dropping any staged business row/directory or reassigning IDs must fail.
        validated = self.validate()
        summary = validated.snapshot.summary
        self.assertEqual(summary.counts, dict(customers=1, products=1, templates=1, crawler_seen=3,
            emails=1, outreach_jobs=1, suppressions=1, followup_records=1,
            followup_deleted_requests=1, followup_plans=2, active_plans=1, photos=1))
        self.assertTrue(all(summary.items_present.values()))
        self.assertEqual(summary.created_at, "2026-10-07T00:00:00Z")
        self.assertEqual(summary.app_version, "0.2.0")
        self.assertTrue((self.destination / "data/crawler_photos/空店").is_dir())
        self.assertTrue((self.destination / "data/photo_scan").is_dir())
        self.assertEqual(business_digest(resolve_layout(self.destination)), self.before)
        self.assertEqual(business_digest(self.layout), self.before)
        self.assertEqual(validated.archive_sha256, hashlib.sha256(self.archive.read_bytes()).hexdigest())
        with closing(sqlite3.connect(self.destination / "data/tradewind_memory.db")) as conn:
            self.assertEqual(conn.execute("SELECT id,workflow_id FROM emails").fetchall(), [(7, "history-workflow")])
            self.assertEqual(conn.execute("SELECT id,previous_plan_id FROM customer_followup_plans ORDER BY id").fetchall(), [(9, None), (13, 9)])
            self.assertEqual(conn.execute("SELECT seq FROM sqlite_sequence WHERE name='customer_followup_records'").fetchone(), (50,))

    def test_missing_and_empty_are_distinct(self):
        (self.layout.data_dir / "products.json").unlink()
        (self.layout.data_dir / "customers.json").write_text("[]", encoding="utf-8")
        (self.layout.data_dir / "tradewind_memory.db").unlink()
        self.archive = make_archive(self.layout, self.archive, photos=False)
        self.before = business_digest(self.layout)
        summary = self.validate().snapshot.summary
        self.assertFalse(summary.items_present["products"])
        self.assertTrue(summary.items_present["customers"])
        self.assertEqual(summary.counts["customers"], 0)
        self.assertFalse(summary.items_present["memory"])
        self.assertFalse(summary.items_present["crawler_photos"])
        self.assertEqual(business_digest(self.layout), self.before)

    def test_nfc_case_alias_members_rejected(self):
        for names in (("é", "e\u0301"), ("Shop", "shop")):
            with self.subTest(names=names):
                self.archive = make_archive(self.layout, self.archive, photos=True)
                def change(manifest, entries):
                    for name in names:
                        path = f"data/crawler_photos/{name}/01.gif"
                        content = b"GIF89a"
                        manifest["files"].append(dict(path=path, size=len(content), sha256=hashlib.sha256(content).hexdigest()))
                        entries.append((path, content))
                    return entries
                self.rewrite(change)
                self.reject()

    def test_crc_hash_missing_extra_encrypted_and_future_schema_rejected(self):
        for kind in ("hash", "missing", "extra", "version", "encrypted", "crc"):
            with self.subTest(kind=kind):
                self.archive = make_archive(self.layout, self.archive, photos=True)
                def change(manifest, entries):
                    if kind == "hash": manifest["files"][0]["sha256"] = "0" * 64
                    if kind == "version": manifest["format_version"] = 2
                    if kind == "missing": entries.pop()
                    if kind == "extra": entries.append(("data/config.json", b"{}"))
                    return entries
                self.rewrite(change)
                if kind in {"encrypted", "crc"}:
                    data = bytearray(self.archive.read_bytes())
                    if kind == "encrypted":
                        local = data.index(b"PK\x03\x04")
                        central = data.index(b"PK\x01\x02")
                        struct.pack_into("H", data, local + 6, 1)
                        struct.pack_into("H", data, central + 8, 1)
                    else:
                        start = data.index(b"PK\x03\x04")
                        offset = start + 30 + struct.unpack_from("H", data, start + 26)[0] + struct.unpack_from("H", data, start + 28)[0]
                        data[offset] ^= 1
                    self.archive.write_bytes(data)
                self.reject()

    def test_malformed_json_and_duplicate_customer_ids_rejected(self):
        for content in (b"[", b"{}", b'[{"id":"x","name":"A"},{"id":"x","name":"B"}]',
                        b'[{"id":true,"name":"A"}]', b'[{"id":"x","name":"A","status":1}]',
                        b'[{"id":"x","id":"y","name":"A"}]', b'[NaN]'):
            with self.subTest(content=content):
                self.archive = make_archive(self.layout, self.archive, photos=True)
                self.replace_content("data/customers.json", content)
                self.reject()

    def test_orphan_followup_and_duplicate_active_plan_rejected(self):
        self.update_db("UPDATE customer_followup_records SET customer_id='absent';")
        self.reject()
        self.update_db("UPDATE customer_followup_records SET customer_id='customer-甲'; DROP INDEX uq_followup_active_customer; UPDATE customer_followup_plans SET status='active';")
        self.reject()

    def test_outreach_history_for_deleted_customer_allowed(self):
        self.assertEqual(self.validate().snapshot.summary.counts["outreach_jobs"], 1)

    def test_sqlite_trigger_view_virtual_table_and_unknown_table_rejected(self):
        side_effect = self.root / "evil.txt"
        target = str(side_effect).replace("'", "''")
        mutations = [f"CREATE TRIGGER evil AFTER INSERT ON emails BEGIN SELECT writefile('{target}','side effect'); END;",
                     f"CREATE VIEW evil AS SELECT writefile('{target}','side effect');",
                     "CREATE VIRTUAL TABLE evil USING fts5(content);", "CREATE TABLE future(value TEXT);"]
        db = self.layout.data_dir / "tradewind_memory.db"
        original = db.read_bytes()
        for sql in mutations:
            with self.subTest(sql=sql):
                db.write_bytes(original)
                self.update_db(sql)
                real_connect = sqlite3.connect
                def guarded_connect(*args, **kwargs):
                    connection = real_connect(*args, **kwargs)
                    # A real connection with an executable trap, not a fake DB.
                    connection.create_function("writefile", 2,
                        lambda name, text: Path(name).write_text(text))
                    return connection
                with patch.object(sqlite3, "connect", guarded_connect):
                    self.reject()
                self.assertFalse(side_effect.exists())

    def test_actual_stream_limits_and_cancel_clean_staging(self):
        for field in ("max_archive_bytes", "max_unpacked_bytes", "max_files", "max_photo_bytes", "max_json_bytes", "max_db_bytes", "max_manifest_bytes"):
            with self.subTest(field=field):
                self.reject(limits=replace(BackupLimits(), **{field: 1}))
        checks = 0
        def cancel():
            nonlocal checks
            checks += 1
            return checks >= 5
        self.reject(cancel=cancel, error=BackupCancelled)

    def test_summary_tampering_rejected_and_inspection_recounts(self):
        self.rewrite(lambda m, e: (m["counts"].update(customers=9), e)[1])
        self.reject()
        self.archive = make_archive(self.layout, self.archive, photos=True)
        self.validate()
        metadata = self.destination / "manifest.json"
        value = json.loads(metadata.read_text(encoding="utf-8"))
        value["counts"]["customers"] = 99
        metadata.write_text(json.dumps(value), encoding="utf-8")
        validator = importlib.import_module("app.backup.validation")
        summary = validator.inspect_snapshot(self.destination, includes_photos=True, limits=BackupLimits())
        self.assertEqual(summary.counts["customers"], 1)
        self.assertEqual(summary.created_at, "2026-10-07T00:00:00Z")

    def test_metadata_free_inspection_uses_current_version_and_bounded_utc_time(self):
        # Returning blank/stale metadata or a file mtime must fail this test.
        self.validate()
        (self.destination / "manifest.json").unlink()
        validator = importlib.import_module("app.backup.validation")
        before = datetime.now(timezone.utc)
        summary = validator.inspect_snapshot(self.destination, includes_photos=True, limits=BackupLimits())
        after = datetime.now(timezone.utc)
        self.assertEqual(summary.app_version, "0.2.0")
        self.assertTrue(summary.created_at)
        inspected_at = datetime.fromisoformat(summary.created_at.replace("Z", "+00:00"))
        self.assertIsNotNone(inspected_at.tzinfo)
        self.assertEqual(inspected_at.utcoffset(), timedelta(0))
        self.assertLessEqual(before, inspected_at)
        self.assertLessEqual(inspected_at, after)
        self.assertEqual(summary.counts["customers"], 1)
        self.assertFalse((self.destination / "manifest.json").exists())
        self.assertEqual(business_digest(self.layout), self.before)

    def test_existing_manifest_preserves_historical_version_and_utc_time(self):
        # A default current version/time must not overwrite recorded metadata.
        self.rewrite(lambda m, e: (m.update(app_version="0.1.7", created_at="2020-01-02T03:04:05+00:00"), e)[1])
        validated = self.validate()
        self.assertEqual(validated.snapshot.summary.app_version, "0.1.7")
        self.assertEqual(validated.snapshot.summary.created_at, "2020-01-02T03:04:05+00:00")
        validator = importlib.import_module("app.backup.validation")
        summary = validator.inspect_snapshot(self.destination, includes_photos=True, limits=BackupLimits())
        self.assertEqual(summary.app_version, "0.1.7")
        self.assertEqual(summary.created_at, "2020-01-02T03:04:05+00:00")

    def test_unsupported_compression_links_directories_and_photo_types_rejected(self):
        for kind in ("compression", "link", "directory", "photo", "no-photos", "file-prefix"):
            with self.subTest(kind=kind):
                self.archive = make_archive(self.layout, self.archive, photos=True)
                def change(manifest, entries):
                    if kind == "no-photos": manifest["includes_photos"] = False
                    if kind == "directory": entries.append(("data/photo_scan/", b""))
                    if kind == "file-prefix": manifest["directories"].append("data/crawler_photos/合成甲店/01.gif/sub")
                    if kind == "photo": entries.append(("data/photo_scan/x.txt", b"hello"))
                    if kind == "compression": entries[1][0].compress_type = zipfile.ZIP_BZIP2
                    if kind == "link":
                        entries[1][0].create_system = 3
                        entries[1][0].external_attr = (stat.S_IFLNK | 0o777) << 16
                    return entries
                self.rewrite(change)
                self.reject()

    def test_destination_must_be_new_and_never_removes_existing_content(self):
        self.destination.mkdir()
        sentinel = self.destination / "keep.txt"
        sentinel.write_text("owned by someone else")
        with self.assertRaises(BackupError): self.validate()
        self.assertEqual(sentinel.read_text(), "owned by someone else")

    def test_future_columns_missing_constraints_and_wrong_indexes_rejected(self):
        db = self.layout.data_dir / "tradewind_memory.db"
        original = db.read_bytes()
        for sql in ("ALTER TABLE emails ADD COLUMN future TEXT;",
                    "DROP INDEX uq_emails_workflow; CREATE INDEX uq_emails_workflow ON emails(workflow_id);",
                    "DROP INDEX idx_followup_plan_customer; CREATE INDEX idx_followup_plan_customer ON customer_followup_plans(id);",
                    "PRAGMA writable_schema=ON; UPDATE sqlite_master SET sql=replace(sql,'TEXT NOT NULL','TEXT') WHERE name='customer_followup_records'; PRAGMA writable_schema=OFF;"):
            with self.subTest(sql=sql):
                db.write_bytes(original)
                self.update_db(sql)
                self.reject()

    def test_invalid_business_values_and_relations_rejected(self):
        db = self.layout.data_dir / "tradewind_memory.db"
        original = db.read_bytes()
        for sql in ("UPDATE customer_followup_records SET outcome='future';",
                    "UPDATE customer_followup_plans SET due_date='2026-02-30';",
                    "UPDATE customer_followup_plans SET previous_plan_id=999 WHERE id=13;",
                    "UPDATE customer_followup_deleted_requests SET request_id='record-request';",
                    "UPDATE outreach_jobs SET status='future';",
                    "UPDATE outreach_jobs SET provider='whatsapp_link';",
                    "UPDATE contact_suppressions SET normalized_recipient='UPPER@SHOP.EXAMPLE';",
                    "UPDATE sqlite_sequence SET seq=0 WHERE name='emails';"):
            with self.subTest(sql=sql):
                db.write_bytes(original)
                self.update_db(sql)
                self.reject()

    def test_explicit_legacy_memory_only_schema_accepted(self):
        db = self.layout.data_dir / "tradewind_memory.db"
        db.unlink()
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE emails(id INTEGER PRIMARY KEY AUTOINCREMENT,customer TEXT NOT NULL,country TEXT DEFAULT '',product TEXT DEFAULT '',email TEXT NOT NULL,score REAL DEFAULT 0,created_at REAL NOT NULL)")
            conn.execute("INSERT INTO emails(id,customer,email,created_at) VALUES(23,'合成旧店','旧文案',1704067200)")
        self.archive = make_archive(self.layout, self.archive, photos=True)
        self.before = business_digest(self.layout)
        summary = self.validate().snapshot.summary
        self.assertEqual(summary.counts["emails"], 1)
        self.assertEqual(summary.counts["followup_records"], 0)

    def test_migrated_column_order_accepted(self):
        # Current schemas can have ALTER-added columns after created_at.
        self.update_db("ALTER TABLE emails RENAME TO old_emails; DROP INDEX uq_emails_workflow; CREATE TABLE emails(id INTEGER PRIMARY KEY AUTOINCREMENT,customer TEXT NOT NULL,country TEXT DEFAULT '',product TEXT DEFAULT '',email TEXT NOT NULL,score REAL DEFAULT 0,created_at REAL NOT NULL,language TEXT DEFAULT 'zh-hant',format TEXT DEFAULT 'email',workflow_id TEXT DEFAULT ''); INSERT INTO emails SELECT id,customer,country,product,email,score,created_at,language,format,workflow_id FROM old_emails; DROP TABLE old_emails; CREATE UNIQUE INDEX uq_emails_workflow ON emails(workflow_id) WHERE workflow_id <> '';")
        self.assertEqual(self.validate().snapshot.summary.counts["emails"], 1)

    def test_actual_deflate_bytes_exceed_forged_declarations(self):
        # Forged central/local sizes must not hide the real deflate output.
        photo = "data/crawler_photos/合成甲店/01.gif"
        payload = b"GIF89a" + b"A" * (3 * 1024 * 1024)
        self.replace_content(photo, payload)
        def change(manifest, entries):
            for entry in manifest["files"]:
                if entry["path"] == photo:
                    entry["size"] = 6
                    entry["sha256"] = hashlib.sha256(b"GIF89a").hexdigest()
            return entries
        self.rewrite(change)
        data = bytearray(self.archive.read_bytes())
        encoded = photo.encode("utf-8")
        pos = 0
        while (pos := data.find(b"PK\x01\x02", pos)) >= 0:
            length = struct.unpack_from("<H", data, pos + 28)[0]
            if data[pos + 46:pos + 46 + length] == encoded:
                local = struct.unpack_from("<L", data, pos + 42)[0]
                struct.pack_into("<L", data, pos + 24, 6)
                struct.pack_into("<L", data, local + 22, 6)
                break
            pos += 4
        self.archive.write_bytes(data)
        error = self.reject(limits=replace(BackupLimits(), max_photo_bytes=1024 * 1024))
        self.assertEqual(error.code, "resource_limit")

    def test_cancel_after_extraction_and_progress_failure_remove_only_staging(self):
        checks = []
        def progress(*args): checks.append(args)
        with self.assertRaises(BackupCancelled):
            self.validate(cancel=lambda: bool(checks), progress=progress)
        self.assertTrue(checks)
        self.assertFalse(self.destination.exists())
        self.assertEqual(business_digest(self.layout), self.before)
        def broken_progress(*args): raise RuntimeError("synthetic callback failure")
        with self.assertRaises(RuntimeError): self.validate(progress=broken_progress)
        self.assertFalse(self.destination.exists())

    def test_streaming_export_zip64_local_headers_and_descriptors_supported(self):
        import io
        with zipfile.ZipFile(self.archive) as archive:
            entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
        class NonSeekable(io.BytesIO):
            def seek(self, *args): raise OSError("non-seekable export stream")
        output = NonSeekable()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                with archive.open(name, "w", force_zip64=True) as target:
                    target.write(content)
        self.archive.write_bytes(output.getvalue())
        self.assertEqual(self.validate().snapshot.summary.counts["customers"], 1)

    def test_inspection_refuses_unknown_files_and_links(self):
        self.validate()
        validator = importlib.import_module("app.backup.validation")
        (self.destination / "data/config.json").write_text("{}")
        with self.assertRaises(BackupError):
            validator.inspect_snapshot(self.destination, includes_photos=True, limits=BackupLimits())

    def test_empty_photo_stores_are_not_missing_stores(self):
        (self.layout.data_dir / "crawler_photos/合成甲店/01.gif").unlink()
        self.archive = make_archive(self.layout, self.archive, photos=True)
        self.before = business_digest(self.layout)
        summary = self.validate().snapshot.summary
        self.assertTrue(summary.includes_photos)
        self.assertTrue(summary.items_present["crawler_photos"])
        self.assertTrue(summary.items_present["photo_scan"])
        self.assertEqual(summary.counts["photos"], 0)

    def test_forged_item_presence_missing_tables_and_corrupt_database_refused(self):
        self.rewrite(lambda m, e: (m["items_present"].update(memory=False), e)[1])
        self.reject()
        self.archive = make_archive(self.layout, self.archive, photos=True)
        self.replace_content("data/tradewind_memory.db", b"not a database")
        self.reject()
        self.update_db("DROP TABLE customer_followup_records;")
        self.assertEqual(self.reject().code, "unsupported_schema")

    def test_manifest_must_be_first_and_have_exact_wire_types(self):
        for field, value in (("format_version", True), ("created_at", "2026-10-07"),
                             ("includes_photos", 1), ("directories", ["data/customers.json"]),
                             ("counts", {"customers": 0})):
            with self.subTest(field=field):
                self.archive = make_archive(self.layout, self.archive, photos=True)
                self.rewrite(lambda m, e: (m.update({field: value}), e)[1])
                self.reject()
        self.archive = make_archive(self.layout, self.archive, photos=True)
        with zipfile.ZipFile(self.archive) as archive:
            entries = [(info, archive.read(info)) for info in archive.infolist()]
        with zipfile.ZipFile(self.archive, "w") as archive:
            for info, content in entries[1:] + entries[:1]: archive.writestr(info, content)
        self.reject()

    def test_all_reads_are_bounded_and_production_import_is_initializer_free(self):
        # Fresh interpreter avoids the fixture's intentionally lazy business imports.
        import subprocess
        import sys
        code = "import sys; import app.backup.validation; assert not any(n in sys.modules for n in ('app.config','app.memory','app.outreach_store','app.customer_followup','server'))"
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        import io
        original_open = Path.open
        sizes = []
        class BoundedReader(io.BufferedReader):
            def read(self, size=-1):
                sizes.append(size)
                if not 0 <= size <= 1024 * 1024:
                    raise AssertionError(f"unbounded archive read: {size}")
                return super().read(size)
        def open_path(path, mode="r", *args, **kwargs):
            if path == self.archive and mode == "rb":
                return BoundedReader(io.FileIO(path, "r"))
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, "open", open_path):
            self.validate()
        self.assertTrue(sizes)

    def test_crc_failure_during_file_extraction_cleans_destination(self):
        # Corrupt a STORED business member, after a valid manifest was read.
        with zipfile.ZipFile(self.archive) as archive:
            entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, content in entries: archive.writestr(name, content)
        with zipfile.ZipFile(self.archive) as archive:
            info = archive.getinfo("data/customers.json")
        data = bytearray(self.archive.read_bytes())
        start = info.header_offset
        offset = start + 30 + struct.unpack_from("<H", data, start + 26)[0] + struct.unpack_from("<H", data, start + 28)[0]
        data[offset] ^= 1
        self.archive.write_bytes(data)
        self.reject()

    def test_duplicate_active_outreach_jobs_and_request_ids_refused(self):
        self.update_db("DROP INDEX uq_outreach_active_customer_channel; UPDATE outreach_jobs SET status='pending_review'; INSERT INTO outreach_jobs(customer_id,customer_name_snapshot,channel,provider,recipient,body,content_hash,status,created_at,updated_at) SELECT customer_id,customer_name_snapshot,channel,provider,recipient,body,content_hash,status,created_at,updated_at FROM outreach_jobs;")
        self.reject()

    def test_unmatched_photo_extension_and_signature_refused(self):
        self.replace_content("data/crawler_photos/合成甲店/01.gif", b"not a GIF")
        self.reject()

    def test_links_in_staged_tree_are_refused_without_following_target(self):
        self.validate()
        external = self.root / "outside"
        external.mkdir()
        sentinel = external / "private.gif"
        sentinel.write_bytes(b"private synthetic bytes")
        # Junction creation does not need Windows symlink privilege.
        import subprocess
        link = self.destination / "data/photo_scan/junction"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        validator = importlib.import_module("app.backup.validation")
        with self.assertRaises(BackupError):
            validator.inspect_snapshot(self.destination, includes_photos=True, limits=BackupLimits())
        self.assertEqual(sentinel.read_bytes(), b"private synthetic bytes")

    def test_large_central_directory_is_read_in_bounded_chunks(self):
        import io
        gif = (self.layout.data_dir / "crawler_photos/合成甲店/01.gif").read_bytes()
        for index in range(20):
            (self.layout.data_dir / f"photo_scan/{index}.gif").write_bytes(gif)
        self.before = business_digest(self.layout)
        self.archive = make_archive(self.layout, self.archive, photos=True)
        def change(manifest, entries):
            for info, content in entries: info.comment = b"x" * 65535
            return entries
        self.rewrite(change)
        original_open = Path.open
        class BoundedReader(io.BufferedReader):
            def read(self, size=-1):
                if not 0 <= size <= 1024 * 1024:
                    raise AssertionError(f"unbounded central-directory read: {size}")
                return super().read(size)
        def open_path(path, mode="r", *args, **kwargs):
            if path == self.archive and mode == "rb": return BoundedReader(io.FileIO(path, "r"))
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, "open", open_path):
            self.assertEqual(self.validate().snapshot.summary.counts["photos"], 21)

    def test_semantic_sqlite_types_checks_and_extra_constraints_refused(self):
        db = self.layout.data_dir / "tradewind_memory.db"
        original = db.read_bytes()
        mutations = [(r"score\s+REAL", "score TEXT", "emails"),
                     (r" CHECK\(channel IN \('whatsapp', 'email'\)\)", "", "outreach_jobs"),
                     ("reason TEXT NOT NULL", "reason TEXT NOT NULL CHECK(length(reason)<1)", "contact_suppressions")]
        for before, after, table in mutations:
            with self.subTest(table=table):
                db.write_bytes(original)
                with closing(sqlite3.connect(db)) as conn, conn:
                    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
                    self.assertRegex(sql, before)
                    conn.execute("PRAGMA writable_schema=ON")
                    conn.execute("UPDATE sqlite_master SET sql=? WHERE name=?", (re.sub(before, after, sql), table))
                self.archive = make_archive(self.layout, self.archive, photos=True)
                self.before = business_digest(self.layout)
                self.reject()

    def test_json_templates_products_and_crawler_known_shapes_are_strict(self):
        for name, value in (("products.json", [{"id": "p", "title": "x", "content": "x", "tags": [1]}]),
                            ("emails.json", [{"id": "e", "title": "x"}]),
                            ("crawler_seen.json", [{"domain": "example.test", "first_seen_at": "today"}])):
            with self.subTest(name=name):
                self.archive = make_archive(self.layout, self.archive, photos=True)
                self.replace_content("data/" + name, json.dumps(value).encode())
                self.reject()
