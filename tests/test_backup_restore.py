"""Task5 behavior against owned synthetic roots, never application runtime data."""
from contextlib import closing
from dataclasses import replace
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile

from backup_helpers import business_digest, make_archive, make_business_fixture, make_constructor_fixture
from app.backup.maintenance import MaintenanceCoordinator
from app.backup.models import BackupCancelled, BackupError, BackupLimits, RestorePreview
from app.backup.validation import validate_archive


class RestoreFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tw-restore-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.layout = make_business_fixture(self.root / "live")
        self.incoming = make_business_fixture(self.root / "incoming")
        (self.incoming.data_dir / "products.json").write_text(
            '[{"id":"new-product","title":"新合成设备","content":"新资料"}]', encoding="utf-8")
        self.limits = BackupLimits(space_margin_bytes=0)
        self.coordinator = MaintenanceCoordinator(self.layout)

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("app.backup.restore"),
                             "Task5 rescue/restore implementation is missing")
        return importlib.import_module("app.backup.restore")

    def validated(self, *, photos=False):
        token = uuid.uuid4().hex
        zipped = make_archive(self.incoming, self.root / (token + ".zip"), photos=photos)
        return validate_archive(zipped, self.root / token, limits=self.limits,
                                cancel=lambda: False, progress=lambda *args: None)

    def preview(self, validated):
        epoch, revision = self.coordinator.version()
        return RestorePreview(str(uuid.uuid4()), validated.archive_sha256, epoch.epoch, revision,
                              time.time() + 1800, validated.snapshot.summary, validated.snapshot.summary)

    def prepare(self, *, validated=None, preview=None, request="synthetic-request", cancel=lambda: False,
                progress=lambda *args: None, lease=None):
        module = self.module()
        validated = validated or self.validated()
        preview = preview or self.preview(validated)
        arguments = dict(request_id=request, coordinator=self.coordinator,
                         memory_path=self.layout.data_dir / "tradewind_memory.db", limits=self.limits,
                         cancel=cancel, progress=progress)
        if lease is not None:
            return module.prepare_restore(self.layout, validated, preview, lease=lease, **arguments)
        with self.coordinator.exclusive("prepare") as active:
            return module.prepare_restore(self.layout, validated, preview, lease=active, **arguments)

    def apply(self):
        return self.module().apply_pending_restore(self.layout,
            memory_path=self.layout.data_dir / "tradewind_memory.db", limits=self.limits)


class RestoreTests(RestoreFixture):
    def test_prepare_requires_current_epoch_revision_valid_preview_and_rescue(self):
        self.module()
        validated = self.validated()
        preview = self.preview(validated)
        before = business_digest(self.layout)
        for bad in (replace(preview, epoch=str(uuid.uuid4())), replace(preview, revision=1),
                    replace(preview, expires_at=time.time() - 1), replace(preview, id=""),
                    replace(preview, archive_sha256="0" * 64),
                    replace(preview, backup=replace(preview.backup, includes_photos=True))):
            with self.subTest(preview=bad), self.assertRaises(BackupError):
                self.prepare(validated=validated, preview=bad)
            self.assertEqual(self.coordinator.mode, "ready")
            self.assertEqual(before, business_digest(self.layout))
        with self.coordinator.exclusive("snapshot") as lease, self.assertRaises(BackupError):
            self.prepare(validated=validated, preview=preview, lease=lease)
        prepared = self.prepare(validated=validated, preview=preview)
        rescue = self.layout.rescue_dir / prepared.rescue_id / "rescue.zip"
        self.assertTrue(rescue.is_file())
        checked = validate_archive(rescue, self.root / "checked-rescue", limits=self.limits,
                                   cancel=lambda: False, progress=lambda *args: None)
        self.assertTrue(checked.snapshot.summary.includes_photos)
        self.assertEqual(self.coordinator.mode, "prepared")

    def test_prepare_is_idempotent_and_cancel_keeps_live_files(self):
        module = self.module()
        validated = self.validated()
        preview = self.preview(validated)
        before = business_digest(self.layout)
        lease = self.coordinator.reserve_exclusive("prepare")
        prepared = self.prepare(validated=validated, preview=preview, lease=lease)
        lease.release()
        # Service can perform this lookup before trying to reserve a new lease.
        repeated = self.prepare(validated=validated, preview=preview, lease=lease)
        self.assertEqual(repeated, prepared)
        other = self.validated(photos=True)
        with self.assertRaises(BackupError) as caught:
            self.prepare(validated=other, lease=lease)
        self.assertEqual(caught.exception.code, "prepare_conflict")
        module.cancel_prepared_restore(self.layout, prepared.operation_id, self.coordinator)
        self.assertEqual(self.coordinator.mode, "ready")
        self.assertEqual(before, business_digest(self.layout))
        self.assertTrue((self.layout.rescue_dir / prepared.rescue_id / "rescue.zip").is_file())
        self.assertFalse((self.layout.rescue_dir / prepared.operation_id / "pending").exists())
        self.assertEqual(self.apply().status, "none")
        module.cancel_prepared_restore(self.layout, prepared.operation_id, self.coordinator)

    def test_cancel_is_durable_before_coordinator_release_and_failure_keeps_hold(self):
        module = self.module()
        prepared = self.prepare()
        real_cancel = self.coordinator.cancel_prepared
        observed = []
        def release(operation):
            observed.append(module._read_recovery_state(self.layout)["state"])
            real_cancel(operation)
        with patch.object(module.os, "replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(BackupError):
                module.cancel_prepared_restore(self.layout, prepared.operation_id, self.coordinator)
        self.assertEqual(self.coordinator.mode, "prepared")
        self.assertEqual(module._read_recovery_state(self.layout)["state"], "prepared")
        with patch.object(self.coordinator, "cancel_prepared", release):
            module.cancel_prepared_restore(self.layout, prepared.operation_id, self.coordinator)
        self.assertEqual(observed, ["cancelled"])

    def test_rescue_always_contains_current_photos_and_lease_through_publication(self):
        module = self.module()
        lease = self.coordinator.reserve_exclusive("prepare")
        seen = set()
        original = module._persist
        def persist(*args):
            lease.assert_active()
            with self.assertRaises(BackupError):
                self.coordinator.reserve_worker("synthetic", mutating=True)
            seen.add("publication")
            return original(*args)
        def progress(phase, done, total):
            lease.assert_active()
            seen.add(phase)
            with self.assertRaises(BackupError):
                self.coordinator.reserve_exclusive("snapshot")
        with patch.object(module, "_persist", persist):
            prepared = self.prepare(lease=lease, progress=progress)
        lease.release()
        self.assertTrue({"capture", "compress", "publication"} <= seen)
        with zipfile.ZipFile(self.layout.rescue_dir / prepared.rescue_id / "rescue.zip") as archive:
            self.assertIn("data/crawler_photos/合成甲店/01.gif", archive.namelist())
            self.assertTrue(json.loads(archive.read("manifest.json"))["includes_photos"])

    def test_prepare_failure_does_not_hold_maintenance_or_publish_partial_rescue(self):
        module = self.module()
        before = business_digest(self.layout)
        for function in ("capture_snapshot", "write_archive", "validate_archive", "_persist"):
            with self.subTest(function=function), patch.object(module, function,
                    side_effect=BackupError("synthetic_failure", "合成故障。")):
                with self.assertRaises(BackupError):
                    self.prepare()
            self.assertEqual(self.coordinator.mode, "ready")
            self.assertEqual(before, business_digest(self.layout))
            self.assertIsNone(module._read_recovery_state(self.layout))
        cancelled = False
        def progress(*args):
            nonlocal cancelled
            cancelled = True
        with self.assertRaises(BackupCancelled):
            self.prepare(cancel=lambda: cancelled, progress=progress)
        self.assertEqual(self.coordinator.mode, "ready")

    def test_tampered_prepared_payload_blocks_without_changing_live_data(self):
        self.module()
        prepared = self.prepare()
        before = business_digest(self.layout)
        payload = self.layout.rescue_dir / prepared.operation_id / "pending/data/products.json"
        payload.write_text('[{"id":"tampered","title":"合法但已改变","content":"合成"}]', encoding="utf-8")
        self.assertEqual(self.apply().status, "blocked")
        self.assertEqual(business_digest(self.layout), before)

    def test_epoch_changed_on_disk_refuses_preparation_even_with_cached_coordinator(self):
        self.module()
        validated = self.validated()
        preview = self.preview(validated)
        self.layout.epoch_file.write_text(json.dumps(dict(format_version=1, epoch=str(uuid.uuid4()),
                                                        require_header=True)), encoding="utf-8")
        before = business_digest(self.layout)
        with self.assertRaises(BackupError):
            self.prepare(validated=validated, preview=preview)
        self.assertEqual(self.coordinator.mode, "ready")
        self.assertEqual(business_digest(self.layout), before)

    def test_exception_after_prepared_publication_keeps_durable_hold_and_rescue(self):
        module = self.module()
        replace = module.os.replace
        before = business_digest(self.layout)
        def published_then_failed(source, target):
            result = replace(source, target)
            if Path(target).name == "recovery.json":
                raise OSError("synthetic publication acknowledgement failure")
            return result
        with patch.object(module.os, "replace", published_then_failed), self.assertRaises(BackupError):
            self.prepare()
        self.assertEqual(self.coordinator.mode, "prepared")
        state = module._read_recovery_state(self.layout)
        self.assertEqual(state["state"], "prepared")
        self.assertTrue((self.layout.rescue_dir / state["operation_id"] / "rescue.zip").is_file())
        self.assertEqual(business_digest(self.layout), before)

    def test_revision_changed_by_real_business_ticket_refuses_preparation(self):
        self.module()
        validated = self.validated()
        preview = self.preview(validated)
        ticket = self.coordinator.reserve_worker("synthetic-writer", mutating=True)
        ticket.run(lambda: (self.layout.data_dir / "products.json").write_bytes(b"[]"))
        before = business_digest(self.layout)
        with self.assertRaises(BackupError):
            self.prepare(validated=validated, preview=preview)
        self.assertEqual(business_digest(self.layout), before)

    def test_prepare_never_changes_source_main_or_wal_business_rows(self):
        self.module()
        db_path = self.layout.data_dir / "tradewind_memory.db"
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("INSERT INTO emails(id,customer,email,created_at) VALUES(31,'合成WAL店','已提交WAL资料',1704067200)")
            db.commit()
            main = db_path.read_bytes()
            wal = Path(str(db_path) + "-wal").read_bytes()
            prepared = self.prepare()
            self.assertEqual(db_path.read_bytes(), main)
            self.assertEqual(Path(str(db_path) + "-wal").read_bytes(), wal)
            with zipfile.ZipFile(self.layout.rescue_dir / prepared.rescue_id / "rescue.zip") as archive:
                restored = self.root / "synthetic-rescue-memory.db"
                restored.write_bytes(archive.read("data/tradewind_memory.db"))
            with closing(sqlite3.connect(restored)) as rescue:
                self.assertEqual(rescue.execute("SELECT email FROM emails WHERE id=31").fetchone(), ("已提交WAL资料",))

    def test_prepare_cleanup_failure_has_only_sanitized_error_and_preserves_live_files(self):
        module = self.module()
        before = business_digest(self.layout)
        with patch.object(module, "write_archive", side_effect=BackupError("prepare_failed", "合成故障。")), \
                patch.object(module, "_remove_owned", side_effect=OSError("synthetic-private-cleanup-path")):
            caught = None
            try:
                self.prepare()
            except Exception as error:
                caught = error
        self.assertIsInstance(caught, BackupError)
        self.assertEqual(caught.code, "prepare_failed")
        self.assertNotIn("synthetic-private", str(caught))
        self.assertEqual(self.coordinator.mode, "ready")
        self.assertEqual(before, business_digest(self.layout))



    def test_roundtrip_preserves_private_files_ids_sequences_relations_and_removes_checkpoint(self):
        self.module()
        (self.layout.data_dir / "config.json").write_bytes(b"synthetic config bytes")
        (self.layout.data_dir.parent / ".env").write_bytes(b"SYNTHETIC_ONLY=yes\r\n")
        for name in ("tradewind_workflows.db", "tradewind_workflows.db-wal",
                     "tradewind_workflows.db-shm", "tradewind_workflows.db-journal"):
            # Not a SQLite hot journal; a harmless synthetic side-file sentinel.
            (self.layout.data_dir / name).write_bytes(b"synthetic obsolete side file")
        (self.incoming.data_dir / "crawler_seen.json").unlink()
        self.prepare()
        # An invalid synthetic journal must not be opened as a live SQLite
        # recovery journal during rescue capture; introduce it only afterward.
        (self.layout.data_dir / "tradewind_memory.db-journal").write_bytes(b"synthetic obsolete side file")
        old_epoch = self.coordinator.version()[0].epoch
        outcome = self.apply()
        self.assertEqual(outcome.status, "restored")
        self.assertEqual((self.layout.data_dir / "config.json").read_bytes(), b"synthetic config bytes")
        self.assertEqual((self.layout.data_dir.parent / ".env").read_bytes(), b"SYNTHETIC_ONLY=yes\r\n")
        self.assertEqual(json.loads((self.layout.data_dir / "crawler_seen.json").read_bytes()), [])
        self.assertTrue((self.layout.data_dir / "crawler_photos/合成甲店/01.gif").exists())
        self.assertFalse((self.layout.data_dir / "tradewind_workflows.db").exists())
        self.assertFalse((self.layout.data_dir / "tradewind_memory.db-journal").exists())
        epoch = json.loads(self.layout.epoch_file.read_bytes())
        self.assertEqual(set(epoch), {"format_version", "epoch", "require_header"})
        self.assertEqual(epoch["format_version"], 1)
        self.assertTrue(epoch["require_header"])
        self.assertEqual(uuid.UUID(epoch["epoch"]).version, 4)
        self.assertNotEqual(epoch["epoch"], old_epoch)
        with closing(sqlite3.connect(self.layout.data_dir / "tradewind_memory.db")) as db:
            self.assertEqual(db.execute("SELECT id FROM emails").fetchall(), [(7,)])
            self.assertEqual(db.execute("SELECT seq FROM sqlite_sequence WHERE name='customer_followup_records'").fetchone(), (50,))
            self.assertEqual(db.execute("SELECT id,previous_plan_id FROM customer_followup_plans ORDER BY id").fetchall(), [(9, None), (13, 9)])
        # A later ordinary write cannot cause a committed transaction to replay.
        (self.layout.data_dir / "products.json").write_bytes(b"[]")
        self.assertEqual(self.apply().status, "restored")
        self.assertEqual((self.layout.data_dir / "products.json").read_bytes(), b"[]")

    def test_photos_option_replaces_both_roots_including_missing_incoming_root(self):
        self.module()
        photo = self.incoming.data_dir / "crawler_photos/合成甲店/01.gif"
        photo.unlink()
        photo.parent.rmdir()
        (self.incoming.data_dir / "crawler_photos/空店").rmdir()
        (self.incoming.data_dir / "crawler_photos").rmdir()
        self.prepare(validated=self.validated(photos=True))
        self.assertEqual(self.apply().status, "restored")
        self.assertFalse((self.layout.data_dir / "crawler_photos").exists())
        self.assertTrue((self.layout.data_dir / "photo_scan").is_dir())

    def test_missing_incoming_memory_carries_local_dnc_and_missing_json_defaults(self):
        self.module()
        for name in ("tradewind_memory.db", "products.json", "emails.json", "crawler_seen.json"):
            (self.incoming.data_dir / name).unlink()
        self.prepare()
        self.assertEqual(self.apply().status, "restored")
        for name in ("products.json", "emails.json", "crawler_seen.json"):
            self.assertEqual((self.layout.data_dir / name).read_bytes(), b"[]")
        with closing(sqlite3.connect(self.layout.data_dir / "tradewind_memory.db")) as db:
            self.assertEqual(db.execute("SELECT channel,normalized_recipient,reason FROM contact_suppressions").fetchall(),
                             [("email", "blocked@shop.example", "合成规则")])

    def test_current_constructor_groups_and_early_memory_remain_valid(self):
        self.module()
        for groups in ((), ("memory",), ("outreach",), ("followup",), ("memory", "followup"),
                       ("memory", "outreach"), ("outreach", "followup")):
            with self.subTest(groups=groups):
                self.incoming = make_constructor_fixture(self.root / uuid.uuid4().hex, groups=groups)
                self.prepare(request=uuid.uuid4().hex)
                self.assertEqual(self.apply().status, "restored")
                self.coordinator = MaintenanceCoordinator(self.layout)
        self.incoming = make_constructor_fixture(self.root / "early", groups=())
        with closing(sqlite3.connect(self.incoming.data_dir / "tradewind_memory.db")) as db, db:
            db.execute("CREATE TABLE emails(id INTEGER PRIMARY KEY AUTOINCREMENT,customer TEXT NOT NULL,country TEXT DEFAULT '',product TEXT DEFAULT '',email TEXT NOT NULL,score REAL DEFAULT 0,created_at REAL NOT NULL)")
            db.execute("INSERT INTO emails(id,customer,email,created_at) VALUES(23,'合成店','文案',1704067200)")
            db.execute("UPDATE sqlite_sequence SET seq=80 WHERE name='emails'")
        self.prepare(request="early")
        self.assertEqual(self.apply().status, "restored")
        with closing(sqlite3.connect(self.layout.data_dir / "tradewind_memory.db")) as db:
            self.assertEqual(db.execute("SELECT id FROM emails").fetchall(), [(23,)])
            self.assertEqual(db.execute("SELECT seq FROM sqlite_sequence WHERE name='emails'").fetchone(), (80,))


class ContactSafetyTests(RestoreFixture):
    def jobs(self, layout):
        with closing(sqlite3.connect(layout.data_dir / "tradewind_memory.db")) as db, db:
            db.execute("DELETE FROM outreach_jobs")
            statuses = {"email": ["pending_review", "sending", "failed", "unknown", "opened", "accepted", "sent_manual", "replied", "invalid", "skipped", "suppressed"],
                        "whatsapp": ["pending_review", "opened", "failed", "sent_manual", "replied", "invalid", "skipped", "suppressed"]}
            for channel, values in statuses.items():
                for index, status in enumerate(values):
                    recipient = f"{status}@shop.example" if channel == "email" else str(100000 + index)
                    db.execute("INSERT INTO outreach_jobs(customer_id,customer_name_snapshot,channel,provider,recipient,body,content_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1704067200,1704067200)",
                        (channel + status, "合成店", channel, "qq_smtp" if channel == "email" else "whatsapp_link", recipient, "合成正文", hashlib.sha256(b"body").hexdigest(), status))

    def test_spec_email_opened_becomes_unknown_preserving_id_message_reference_and_times(self):
        module = self.module()
        self.jobs(self.incoming)
        db_path = self.incoming.data_dir / "tradewind_memory.db"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("UPDATE outreach_jobs SET message_id='<synthetic-opened@shop.example>',provider_reference='synthetic-reference',opened_at=1704067201,submitted_at=1704067202,updated_at=1704067203 WHERE customer_id='emailopened'")
            before = dict(db.execute("SELECT * FROM outreach_jobs WHERE customer_id='emailopened'").fetchone())
        counts = module.apply_contact_safety(db_path, self.layout.data_dir / "tradewind_memory.db")
        with closing(sqlite3.connect(db_path)) as db:
            db.row_factory = sqlite3.Row
            after = dict(db.execute("SELECT * FROM outreach_jobs WHERE customer_id='emailopened'").fetchone())
        self.assertEqual(after["status"], "unknown")
        self.assertEqual(after["error_code"], "restore_sentbox_check")
        for key in before.keys() - {"status", "error_code", "error_summary"}:
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(counts["email_requires_sentbox_check"], 4)

    def test_spec_prior_unknown_and_terminal_rows_continue_without_rewriting_references(self):
        module = self.module()
        self.jobs(self.incoming)
        db_path = self.incoming.data_dir / "tradewind_memory.db"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("UPDATE outreach_jobs SET message_id='<synthetic-old@shop.example>',provider_reference='prior-reference',error_code='prior-unknown-code',error_summary='合成既有核查说明',opened_at=1704067201,submitted_at=1704067202,updated_at=1704067203 WHERE customer_id='emailunknown'")
            before = db.execute("SELECT * FROM outreach_jobs WHERE status IN ('unknown','accepted','sent_manual','replied','invalid','skipped','suppressed') ORDER BY id").fetchall()
        module.apply_contact_safety(db_path, self.layout.data_dir / "tradewind_memory.db")
        with closing(sqlite3.connect(db_path)) as db:
            # Use original IDs, so newly conservative rows cannot accidentally
            # enter the expected prior-unknown/terminal selection.
            ids = [row[0] for row in before]
            after = db.execute("SELECT * FROM outreach_jobs WHERE id IN (" + ",".join("?" for _ in ids) + ") ORDER BY id", ids).fetchall()
        self.assertEqual(after, before)

    def test_restored_email_requires_sentbox_check_and_whatsapp_requires_review(self):
        module = self.module()
        self.jobs(self.incoming)
        counts = module.apply_contact_safety(self.incoming.data_dir / "tradewind_memory.db",
                                            self.layout.data_dir / "tradewind_memory.db")
        with closing(sqlite3.connect(self.incoming.data_dir / "tradewind_memory.db")) as db:
            jobs = dict(db.execute("SELECT customer_id,status FROM outreach_jobs"))
        for old in ("pending_review", "opened", "sending", "failed", "unknown"):
            self.assertEqual(jobs["email" + old], "unknown")
        for old in ("pending_review", "opened", "failed"):
            self.assertEqual(jobs["whatsapp" + old], "skipped")
        for channel in ("email", "whatsapp"):
            for terminal in ("sent_manual", "replied", "invalid", "skipped", "suppressed"):
                self.assertEqual(jobs[channel + terminal], terminal)
        self.assertEqual(jobs["emailaccepted"], "accepted")
        self.assertEqual(counts["email_requires_sentbox_check"], 4)
        self.assertEqual(counts["whatsapp_requires_review"], 3)

    def test_local_suppression_union_keeps_current_reason_and_blocks_jobs(self):
        module = self.module()
        self.jobs(self.incoming)
        with closing(sqlite3.connect(self.layout.data_dir / "tradewind_memory.db")) as db, db:
            db.execute("UPDATE contact_suppressions SET reason='本机优先',created_at=1705000000")
            db.execute("INSERT INTO contact_suppressions(channel,normalized_recipient,reason,created_at) VALUES('email','pending_review@shop.example','新本机规则',1705000001)")
            db.execute("INSERT INTO contact_suppressions(channel,normalized_recipient,reason,created_at) VALUES('whatsapp','100001','WA本机规则',1705000002)")
        counts = module.apply_contact_safety(self.incoming.data_dir / "tradewind_memory.db",
                                            self.layout.data_dir / "tradewind_memory.db")
        with closing(sqlite3.connect(self.incoming.data_dir / "tradewind_memory.db")) as db:
            self.assertEqual(db.execute("SELECT reason,created_at FROM contact_suppressions WHERE id=3").fetchone(), ("本机优先", 1705000000.0))
            self.assertEqual(db.execute("SELECT count(*) FROM contact_suppressions").fetchone(), (3,))
            jobs = dict(db.execute("SELECT customer_id,status FROM outreach_jobs"))
        self.assertEqual(jobs["emailpending_review"], "suppressed")
        self.assertEqual(jobs["whatsappopened"], "suppressed")
        self.assertEqual(counts["suppressed_jobs"], 2)
        self.assertEqual(counts["suppressions_added"], 2)


if __name__ == "__main__":
    unittest.main()
