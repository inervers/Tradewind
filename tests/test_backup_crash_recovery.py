"""Real exception and force-exit recovery at each persisted transaction boundary."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

from backup_helpers import business_digest
from test_backup_restore import RestoreFixture
from app.backup.models import BackupError
from app.backup.paths import resolve_layout


class CrashRecoveryTests(RestoreFixture):
    def old_state(self):
        self.layout.epoch_file.write_text(json.dumps(dict(format_version=1, epoch=str(uuid.uuid4()), require_header=False)), encoding="utf-8")
        # Recreate coordinator after establishing the synthetic persisted epoch.
        from app.backup.maintenance import MaintenanceCoordinator
        self.coordinator = MaintenanceCoordinator(self.layout)
        for name in ("tradewind_workflows.db", "tradewind_workflows.db-wal", "tradewind_workflows.db-shm",
                     "tradewind_workflows.db-journal"):
            (self.layout.data_dir / name).write_bytes(b"old synthetic side state")

    def test_every_journal_boundary_recovers_whole_old_or_committed_new_state(self):
        module = self.module()
        # Trace actual I/O: before/after each fsynced journal publication and
        # before/after every rename (including new publication and epoch).
        self.old_state()
        prepared = self.prepare(validated=self.validated(photos=True))
        (self.layout.data_dir / "tradewind_memory.db-journal").write_bytes(b"old synthetic side state")
        seed = self.root / "seed"
        shutil.copytree(self.layout.data_dir.parent, seed)
        events = []
        replace = module.os.replace
        def traced(source, target):
            events.append((Path(source).name, Path(target).name))
            return replace(source, target)
        with patch.object(module.os, "replace", traced):
            self.assertEqual(self.apply().status, "restored")
        self.assertTrue(any(target == ".data-epoch.json" for _, target in events))
        self.assertTrue(any(target == "recovery.json" for _, target in events))
        committed = business_digest(self.layout)
        count = len(events)
        # Clones of one genuinely prepared synthetic root isolate every trial.
        # Each child owns exactly one clone; bounded parallelism does not patch
        # global I/O in the test process or touch the application's runtime.
        code = '''import os,sys,json
from pathlib import Path
from app.backup.paths import resolve_layout
from app.backup.models import BackupLimits
import app.backup.restore as r
layout=resolve_layout(Path(sys.argv[1])); stop=int(sys.argv[2]); mode=sys.argv[3]; index=0
original=os.replace
def fault():
 if mode=='exit': os._exit(73)
 raise OSError('synthetic transaction boundary failure')
def broken(source,target):
 global index
 current=index; index+=2
 if current==stop: fault()
 original(source,target)
 if current+1==stop: fault()
r.os.replace=broken
outcome=r.apply_pending_restore(layout,memory_path=layout.data_dir/'tradewind_memory.db',limits=BackupLimits(space_margin_bytes=0))
print(json.dumps({'status':outcome.status}))
'''
        def trial(case):
            mode, boundary = case
            trial_root = self.root / (mode + "-" + str(boundary))
            shutil.copytree(seed, trial_root)
            layout = resolve_layout(trial_root)
            before = business_digest(layout)
            old_epoch = layout.epoch_file.read_bytes()
            result = subprocess.run([sys.executable, "-B", "-c", code, str(trial_root), str(boundary), mode],
                                    capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 73 if mode == "exit" else 0,
                             result.stderr.decode(errors="replace"))
            if mode == "exception":
                self.assertIn(json.loads(result.stdout)["status"], {"rolled_back", "restored"})
            outcome = module.apply_pending_restore(layout, memory_path=layout.data_dir / "tradewind_memory.db",
                                                   limits=self.limits)
            if outcome.status == "rolled_back":
                self.assertEqual(business_digest(layout), before, case)
                self.assertEqual(layout.epoch_file.read_bytes(), old_epoch, case)
                # Once rolled back, never automatically retry.
                self.assertEqual(module.apply_pending_restore(layout,
                    memory_path=layout.data_dir / "tradewind_memory.db", limits=self.limits).status, "rolled_back")
                self.assertEqual(business_digest(layout), before, case)
            else:
                self.assertEqual(outcome.status, "restored", case)
                digest = business_digest(layout)
                digest.pop(".data-epoch.json")
                expected = dict(committed)
                expected.pop(".data-epoch.json")
                self.assertEqual(digest, expected, case)
                self.assertTrue(json.loads(layout.epoch_file.read_bytes())["require_header"])
            return case
        cases = [(mode, boundary) for mode in ("exception", "exit") for boundary in range(2 * count)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            completed_cases = list(pool.map(trial, cases))
        self.assertEqual(len(completed_cases), 4 * count)
        print(f"Crash matrix: {count} atomic publications/renames, {len(completed_cases)} before/after exception/force-exit trials")

    def test_epoch_absence_is_restored_on_rollback(self):
        module = self.module()
        self.prepare()
        before = business_digest(self.layout)
        replace = module.os.replace
        def fail_after_epoch(source, target):
            result = replace(source, target)
            if Path(target) == self.layout.epoch_file:
                raise OSError("synthetic epoch boundary")
            return result
        with patch.object(module.os, "replace", fail_after_epoch):
            self.assertEqual(self.apply().status, "rolled_back")
        self.assertFalse(self.layout.epoch_file.exists())
        self.assertEqual(before, business_digest(self.layout))

    def test_failure_before_first_intent_durably_abandons_without_retry(self):
        module = self.module()
        self.prepare()
        before = business_digest(self.layout)
        replace = module.os.replace
        failed = False
        def fail_once(source, target):
            nonlocal failed
            if not failed and Path(target).name == "recovery.json":
                failed = True
                raise OSError("synthetic first intent disk failure")
            return replace(source, target)
        with patch.object(module.os, "replace", fail_once):
            self.assertEqual(self.apply().status, "rolled_back")
        self.assertEqual(self.apply().status, "rolled_back")
        self.assertEqual(before, business_digest(self.layout))

    def test_rolled_back_report_never_claims_unapplied_contact_changes(self):
        module = self.module()
        with closing(sqlite3.connect(self.incoming.data_dir / "tradewind_memory.db")) as db, db:
            db.execute("INSERT INTO outreach_jobs(customer_id,customer_name_snapshot,channel,provider,recipient,body,content_hash,status,created_at,updated_at) VALUES('synthetic-active','合成店','email','qq_smtp','active@shop.example','合成文案',?,'pending_review',1704067200,1704067200)", (hashlib.sha256(b"body").hexdigest(),))
        self.prepare()
        self.interrupt_before_commit()
        self.assertEqual(self.apply().counts, {})
        self.assertEqual(self.apply().counts, {})

    def test_tampered_journal_path_and_unrecoverable_rollback_fail_closed(self):
        module = self.module()
        self.old_state()
        prepared = self.prepare()
        journal = self.layout.rescue_dir / prepared.operation_id / "recovery.json"
        outside = self.root / "outside-sentinel"
        outside.write_bytes(b"external file untouched")
        good = journal.read_bytes()
        document = json.loads(good)
        document["target"] = str(outside)
        journal.write_text(json.dumps(document), encoding="utf-8")
        before = business_digest(self.layout)
        self.assertEqual(self.apply().status, "blocked")
        self.assertEqual(before, business_digest(self.layout))
        self.assertEqual(outside.read_bytes(), b"external file untouched")
        journal.write_bytes(good)
        replace = module.os.replace
        def interrupt(source, target):
            result = replace(source, target)
            if Path(target).parent.name == "original" and Path(target).name == "products.json":
                Path(target).unlink()  # genuine missing rollback material
                raise OSError("synthetic lost original")
            return result
        with patch.object(module.os, "replace", interrupt):
            self.assertEqual(self.apply().status, "blocked")
        self.assertEqual(self.apply().status, "blocked")
        self.assertEqual(outside.read_bytes(), b"external file untouched")
        self.assertTrue((journal.parent / "rescue.zip").is_file())

    def test_junction_in_pending_or_original_blocks_without_external_writes(self):
        self.module()
        prepared = self.prepare()
        outside = self.root / "external"
        outside.mkdir()
        (outside / "sentinel").write_bytes(b"external untouched")
        link = self.layout.rescue_dir / prepared.operation_id / "original"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        try:
            self.assertEqual(self.apply().status, "blocked")
            self.assertEqual((outside / "sentinel").read_bytes(), b"external untouched")
        finally:
            link.rmdir()

    def test_no_pending_custom_memory_path_does_not_block_ordinary_startup(self):
        self.assertEqual(self.module().apply_pending_restore(self.layout,
            memory_path=self.root / "custom.db", limits=self.limits).status, "none")
        self.prepare()
        self.assertEqual(self.module().apply_pending_restore(self.layout,
            memory_path=self.root / "custom.db", limits=self.limits).status, "blocked")

    def interrupt_before_commit(self):
        code = '''import os,sys
from pathlib import Path
from app.backup.paths import resolve_layout
from app.backup.models import BackupLimits
import app.backup.restore as r
layout=resolve_layout(Path(sys.argv[1])); original=r._persist
def interrupted(layout,document):
 if document['state']=='committed': os._exit(74)
 return original(layout,document)
r._persist=interrupted
r.apply_pending_restore(layout,memory_path=layout.data_dir/'tradewind_memory.db',limits=BackupLimits(space_margin_bytes=0))
'''
        result = subprocess.run([sys.executable, "-B", "-c", code, str(self.layout.data_dir.parent)],
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 74, result.stderr.decode(errors="replace"))

    def test_incompatible_memory_path_during_rollback_blocks_without_writes(self):
        module = self.module()
        self.prepare()
        self.interrupt_before_commit()
        before = business_digest(self.layout)
        outcome = module.apply_pending_restore(self.layout, memory_path=self.root / "custom.db", limits=self.limits)
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(business_digest(self.layout), before)
        self.assertEqual(self.apply().status, "rolled_back")

    def test_rollback_resumes_after_force_exit_restoring_original_and_undo_publication(self):
        module = self.module()
        self.old_state()
        self.prepare(validated=self.validated(photos=True))
        before = business_digest(self.layout)
        self.interrupt_before_commit()
        seed = self.root / "rollback-seed"
        shutil.copytree(self.layout.data_dir.parent, seed)
        events = []
        replace = module.os.replace
        def traced(source, target):
            events.append((Path(source), Path(target)))
            return replace(source, target)
        with patch.object(module.os, "replace", traced):
            self.assertEqual(self.apply().status, "rolled_back")
        # Independently select rollback log boundaries and original-data moves.
        selected = [index for index, (source, target) in enumerate(events)
                    if target.name == "recovery.json" or source.parent.name == "original"]
        code = '''import os,sys
from pathlib import Path
from app.backup.paths import resolve_layout
from app.backup.models import BackupLimits
import app.backup.restore as r
layout=resolve_layout(Path(sys.argv[1])); stop=int(sys.argv[2]); index=0; original=os.replace
def broken(source,target):
 global index
 current=index; index+=2
 if current==stop: os._exit(75)
 original(source,target)
 if current+1==stop: os._exit(75)
r.os.replace=broken
r.apply_pending_restore(layout,memory_path=layout.data_dir/'tradewind_memory.db',limits=BackupLimits(space_margin_bytes=0))
'''
        def trial(boundary):
            trial_root = self.root / ("undo-" + str(boundary))
            shutil.copytree(seed, trial_root)
            layout = resolve_layout(trial_root)
            result = subprocess.run([sys.executable, "-B", "-c", code, str(trial_root), str(boundary)],
                                    capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 75, result.stderr.decode(errors="replace"))
            outcome = module.apply_pending_restore(layout,
                memory_path=layout.data_dir / "tradewind_memory.db", limits=self.limits)
            self.assertEqual(outcome.status, "rolled_back", boundary)
            self.assertEqual(business_digest(layout), before, boundary)
        boundaries = [2 * index + side for index in selected for side in (0, 1)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(trial, boundaries))
        print(f"Rollback matrix: {len(boundaries)} undo-publication/original-rename force-exit trials")

    def test_strict_bounded_control_reader_rejects_unknown_duplicate_and_oversize(self):
        module = self.module()
        prepared = self.prepare()
        journal = self.layout.rescue_dir / prepared.operation_id / "recovery.json"
        good = journal.read_bytes()
        for raw in (b'{"state":"prepared","state":"committed"}', good + b" " * 65536,
                    good.replace(b'"format_version":1', b'"format_version":2')):
            with self.subTest(raw=raw[:80]):
                journal.write_bytes(raw)
                with self.assertRaises(BackupError):
                    module._read_recovery_state(self.layout)
                self.assertEqual(self.apply().status, "blocked")
        journal.write_bytes(good)


if __name__ == "__main__":
    unittest.main()
