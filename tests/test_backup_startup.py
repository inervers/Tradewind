from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from backup_helpers import make_business_fixture, make_archive, business_digest
from app.backup.archive import capture_snapshot
from app.backup.maintenance import MaintenanceCoordinator
from app.backup.models import BackupLimits, RestorePreview
from app.backup.restore import prepare_restore, apply_pending_restore
from app.backup.validation import validate_archive
from app.backup.paths import resolve_layout


class StartupBackupTests(unittest.TestCase):
    def prepare(self, root):
        import uuid
        layout = make_business_fixture(root / "local")
        incoming = make_business_fixture(root / "incoming")
        customers = json.loads((incoming.data_dir / "customers.json").read_text(encoding="utf-8"))
        customers[0]["name"] = "restored-owned"
        (incoming.data_dir / "customers.json").write_text(json.dumps(customers), encoding="utf-8")
        archive = make_archive(incoming, root / "incoming.zip", photos=False)
        limits = BackupLimits()
        validated = validate_archive(archive, root / "validated", limits=limits, cancel=lambda: False, progress=lambda *a: None)
        coor = MaintenanceCoordinator(layout)
        with coor.exclusive("snapshot") as lease:
            current = capture_snapshot(layout, memory_path=layout.data_dir / "tradewind_memory.db", includes_photos=False,
                destination=root / "current", limits=limits, lease=lease, cancel=lambda: False, progress=lambda *a: None)
        epoch, revision = coor.version()
        preview = RestorePreview(str(uuid.uuid4()), validated.archive_sha256, epoch.epoch, revision, time.time() + 1800,
                                 validated.snapshot.summary, current.summary)
        with coor.exclusive("prepare") as lease:
            prepared = prepare_restore(layout, validated, preview, request_id="startup-owned", coordinator=coor,
                lease=lease, memory_path=layout.data_dir / "tradewind_memory.db", limits=limits,
                cancel=lambda: False, progress=lambda *a: None)
        return layout, prepared

    def child(self, layout, *, custom=False):
        script = '''
import importlib.abc, json, os, pathlib, sys
root = pathlib.Path(os.environ['TRADEWIND_DATA_DIR'])
class Trap(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'app.credential_store':
            print('BEFORE_BUSINESS', json.loads((root/'data/customers.json').read_text(encoding='utf-8'))[0]['name'])
            print('BEFORE_SEED', (root/'data/products.json').exists())
            raise RuntimeError('OWNED_IMPORT_BOUNDARY')
sys.meta_path.insert(0, Trap())
try:
    import server
except Exception as exc:
    print(type(exc).__name__, getattr(exc, 'code', 'boundary'))
'''
        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and k not in {"MEMORY_DB_PATH", "LOCAL_KB_PATH", "PYTHONPATH"}}
        env["TRADEWIND_DATA_DIR"] = str(layout.data_dir.parent)
        if custom:
            env["MEMORY_DB_PATH"] = str(layout.data_dir / "custom-owned.db")
        return subprocess.run([sys.executable, "-B", "-c", script], env=env, capture_output=True, text=True, timeout=30)

    def test_pending_restore_runs_before_seeding_db_import_and_orphan_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="task6-pending-") as directory:
            layout, prepared = self.prepare(Path(directory))
            result = self.child(layout)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("BEFORE_BUSINESS restored-owned", result.stdout)
            self.assertTrue(layout.epoch_file.is_file())
            self.assertTrue((layout.rescue_dir / prepared.rescue_id / "rescue.zip").is_file())

    def test_pending_incompatible_memory_fails_closed_before_business_import(self):
        with tempfile.TemporaryDirectory(prefix="task6-custom-pending-") as directory:
            layout, _ = self.prepare(Path(directory))
            before = business_digest(layout)
            result = self.child(layout, custom=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("BackupError restore_blocked", result.stdout)
            self.assertNotIn("BEFORE_BUSINESS", result.stdout)
            self.assertEqual(business_digest(layout), before)
            self.assertFalse((layout.data_dir / "custom-owned.db").exists())

    def test_rolled_back_startup_uses_old_data_once(self):
        with tempfile.TemporaryDirectory(prefix="task6-rolled-back-") as directory:
            layout, _ = self.prepare(Path(directory))
            with patch("app.backup.restore._move", side_effect=OSError("synthetic move failure")):
                outcome = apply_pending_restore(layout, memory_path=layout.data_dir / "tradewind_memory.db", limits=BackupLimits())
            self.assertEqual(outcome.status, "rolled_back")
            old = json.loads((layout.data_dir / "customers.json").read_text(encoding="utf-8"))
            old[0]["name"] = "old-owned-after-rollback"
            (layout.data_dir / "customers.json").write_text(json.dumps(old), encoding="utf-8")
            before = business_digest(layout)
            for _ in range(2):
                result = self.child(layout)
                self.assertIn("BEFORE_BUSINESS old-owned-after-rollback", result.stdout)
            self.assertEqual(business_digest(layout), before)
    def test_blocked_startup_does_not_import_business_or_seed_or_report_health_ok(self):
        with tempfile.TemporaryDirectory(prefix="task6-startup-") as directory:
            layout = resolve_layout(Path(directory))
            layout.data_dir.mkdir()
            import uuid
            folder = layout.rescue_dir / str(uuid.uuid4())
            folder.mkdir(parents=True)
            (folder / "recovery.json").write_bytes(b"invalid synthetic control")
            script = '''
import importlib.abc, sys
class Trap(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'app.credential_store','app.memory','app.customer_followup','app.email_agent','app.email_workflow'}:
            raise AssertionError('BUSINESS_IMPORT_BEFORE_RECOVERY')
sys.meta_path.insert(0, Trap())
try:
    import server
except Exception as exc:
    print(type(exc).__name__, getattr(exc, 'code', 'unknown'))
else:
    print('UNSAFE_HEALTH_READY')
'''
            env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and k not in {"MEMORY_DB_PATH", "LOCAL_KB_PATH", "PYTHONPATH"}}
            env["TRADEWIND_DATA_DIR"] = directory
            result = subprocess.run([sys.executable, "-B", "-c", script], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("BackupError restore_blocked", result.stdout)
            self.assertNotIn("BUSINESS_IMPORT", result.stdout)
            self.assertFalse((layout.data_dir / "tradewind_memory.db").exists())
            self.assertFalse((layout.data_dir / "products.json").exists())

    def test_invalid_epoch_blocks_before_business_import(self):
        with tempfile.TemporaryDirectory(prefix="task6-epoch-") as directory:
            layout = resolve_layout(Path(directory))
            layout.data_dir.mkdir()
            layout.epoch_file.write_bytes(b"invalid synthetic epoch")
            env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and k not in {"MEMORY_DB_PATH", "LOCAL_KB_PATH", "PYTHONPATH"}}
            env["TRADEWIND_DATA_DIR"] = directory
            script = '''
import importlib.abc, sys
class Trap(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'app.credential_store':
            raise AssertionError('BUSINESS_IMPORT_BEFORE_EPOCH_CHECK')
sys.meta_path.insert(0, Trap())
try:
    import server
except Exception as exc:
    print(type(exc).__name__, getattr(exc, 'code', 'unknown'))
'''
            result = subprocess.run([sys.executable, "-B", "-c", script], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("BackupError invalid_epoch_state", result.stdout)
            self.assertFalse((layout.data_dir / "tradewind_memory.db").exists())


if __name__ == "__main__":
    unittest.main()
