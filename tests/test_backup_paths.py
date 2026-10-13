from __future__ import annotations

import importlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class BackupPathTests(unittest.TestCase):
    def test_deletion_receipt_namespace_is_exact_and_never_an_archive_member(self):
        id = "11111111-1111-4111-8111-111111111111"
        for suffix in ("json", "tmp"):
            name = f".backup-deletions/{id}.{suffix}"
            self.assertEqual(self.paths.owned_path(self.layout, name), self.layout.data_dir / name)
            with self.assertRaises(self.models.BackupError):
                self.paths.validate_member_path("data/" + name)
        for name in (".backup-deletions/foreign.json", ".backup-deletions/config.json",
                     f".backup-deletions/{id}/x", f".backup-deletions/{id}.zip",
                     ".backup-deletions/11111111-1111-1111-8111-111111111111.json"):
            with self.subTest(name=name), self.assertRaises(self.models.BackupError):
                self.paths.owned_path(self.layout, name)
        self.assertFalse(self.layout.data_dir.exists())

    def setUp(self) -> None:
        # Import here so absent production modules produce an observed RED.
        self.models = importlib.import_module("app.backup.models")
        self.paths = importlib.import_module("app.backup.paths")
        self.temp = tempfile.TemporaryDirectory(prefix="tradewind-backup-paths-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.layout = self.paths.resolve_layout(self.root)

    def test_limits_match_spec(self) -> None:
        limits = self.models.BackupLimits()
        self.assertEqual(limits.max_archive_bytes, 2 * 1024**3)
        self.assertEqual(limits.max_unpacked_bytes, 8 * 1024**3)
        self.assertEqual(limits.max_files, 20_000)
        self.assertEqual(limits.max_photo_bytes, 20 * 1024**2)
        self.assertEqual(limits.max_json_bytes, 32 * 1024**2)
        self.assertEqual(limits.max_db_bytes, 512 * 1024**2)
        self.assertEqual(limits.max_manifest_bytes, 8 * 1024**2)
        self.assertEqual(limits.space_margin_bytes, 256 * 1024**2)

    def test_layout_is_fixed_without_creating_runtime_files(self) -> None:
        self.assertEqual(self.layout.data_dir, self.root / "data")
        self.assertEqual(self.layout.work_dir, self.root / "data/.backup-work")
        self.assertEqual(self.layout.rescue_dir, self.root / "data/backups")
        self.assertEqual(self.layout.epoch_file, self.root / "data/.data-epoch.json")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_chinese_photo_path_allowed(self) -> None:
        name = "data/crawler_photos/合成甲店（九龙）/01.jpg"
        self.assertEqual(self.paths.validate_member_path(name), name)
        target = self.paths.owned_path(self.layout, name.removeprefix("data/"))
        target.parent.mkdir(parents=True)
        target.write_bytes(b"synthetic photo")
        self.assertEqual(target.relative_to(self.layout.data_dir).as_posix(),
                         "crawler_photos/合成甲店（九龙）/01.jpg")
        self.assertEqual(target.read_bytes(), b"synthetic photo")

    def test_unsafe_windows_paths_rejected(self) -> None:
        # Removing a Windows spelling check must expose a corresponding case.
        names = [
            "../x", "/data/customers.json", "C:/data/customers.json",
            "C:data/customers.json", "//server/share/x", r"\\server\share\x",
            r"data\customers.json", "data/customers.json:stream",
            "data/crawler_photos/CON.jpg", "data/crawler_photos/con .jpg",
            "data/crawler_photos/AUX", "data/crawler_photos/NUL.png",
            "data/crawler_photos/COM1.gif", "data/crawler_photos/LPT9.webp",
            "data/crawler_photos/COM¹.jpg", "data/crawler_photos/LPT².jpg",
            "data/crawler_photos/店./01.jpg", "data/crawler_photos/店 /01.jpg",
            "data/crawler_photos/x.jpg.", "data/crawler_photos/x.jpg ",
            "data/crawler_photos/../customers.json", "data/./customers.json",
            "data//customers.json", "data/customers.json/", "",
            "data/crawler_photos/x\x00.jpg", "data/crawler_photos/x\x1f.jpg",
            "data/crawler_photos/x\x7f.jpg", "data/crawler_photos/x?.jpg",
            'data/crawler_photos/x".jpg', "data/crawler_photos/x*.jpg",
            "data/crawler_photos/x<.jpg", "data/crawler_photos/x>.jpg",
            "data/crawler_photos/x|.jpg", "data/crawler_photos/\ud800.jpg",
        ]
        for name in names:
            with self.subTest(name=repr(name)):
                with self.assertRaises(self.models.BackupError):
                    self.paths.validate_member_path(name)
                with self.assertRaises(self.models.BackupError):
                    self.paths.owned_path(self.layout, name.removeprefix("data/"))

    def test_archive_whitelist_excludes_internal_and_private_paths(self) -> None:
        for name in ("manifest.json", "data/customers.json", "data/products.json",
                     "data/emails.json", "data/crawler_seen.json",
                     "data/tradewind_memory.db", "data/crawler_photos/空店",
                     "data/photo_scan/01.png"):
            with self.subTest(name=name):
                self.assertEqual(self.paths.validate_member_path(name), name)
        for name in ("data/config.json", "data/.env", "data/tradewind_workflows.db",
                     "data/tradewind_memory.db-wal", "data/errors.log",
                     "data/.backup-work/id/upload.zip", "data/backups/id/rescue.zip",
                     "data/.data-epoch.json", "customers.json", "manifest.json/x"):
            with self.subTest(name=name):
                with self.assertRaises(self.models.BackupError):
                    self.paths.validate_member_path(name)

    def test_owned_paths_allow_only_business_and_control_namespaces(self) -> None:
        names = ("customers.json", "products.json", "emails.json", "crawler_seen.json",
                 "tradewind_memory.db", "crawler_photos", "photo_scan/01.png",
                 ".backup-work", ".backup-work/id/snapshot/data/customers.json",
                 "backups", "backups/id/original/tradewind_workflows.db-wal",
                 ".data-epoch.json")
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(self.paths.owned_path(self.layout, name),
                                 self.root / "data" / name)
        for name in ("config.json", ".env", "manifest.json", "customers.json/x",
                     "tradewind_workflows.db", "tradewind_memory.db-wal",
                     "data/customers.json", ".backup-work-other/x", "backups-other/x",
                     ".data-epoch.json/x", "backups/../../config.json"):
            with self.subTest(name=name):
                with self.assertRaises(self.models.BackupError):
                    self.paths.owned_path(self.layout, name)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_duplicate_key_compares_nfc_and_casefold_without_renaming(self) -> None:
        first = "data/crawler_photos/Café/PHOTO.JPG"
        alias = "data/crawler_photos/Cafe\u0301/photo.jpg"
        self.assertEqual(self.paths.validate_member_path(alias), alias)
        self.assertEqual(self.paths.member_path_key(first),
                         "data/crawler_photos/café/photo.jpg")
        self.assertEqual(self.paths.member_path_key(first), self.paths.member_path_key(alias))
        self.assertNotEqual(self.paths.member_path_key(first),
                            self.paths.member_path_key("data/crawler_photos/Cafe/other.jpg"))

    def test_custom_memory_path_rejected(self) -> None:
        expected = self.root / "data/tradewind_memory.db"
        self.paths.check_memory_path(self.layout, expected)
        for path in (self.root / "other/tradewind_memory.db",
                     self.root / "data/other.db"):
            with self.subTest(path=path):
                with self.assertRaises(self.models.BackupError) as caught:
                    self.paths.check_memory_path(self.layout, path)
                self.assertEqual(caught.exception.code, "custom_memory_path")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_junction_in_owned_ancestor_rejected(self) -> None:
        # A real NTFS junction is safe to create inside this owned temp root.
        outside = self.root / "outside"
        outside.mkdir()
        self.layout.data_dir.mkdir()
        junction = self.layout.data_dir / "crawler_photos"
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(junction.rmdir)
        else:
            junction.symlink_to(outside, target_is_directory=True)
            self.addCleanup(junction.unlink)
        with self.assertRaises(self.models.BackupError):
            self.paths.owned_path(self.layout, "crawler_photos/店/01.jpg")
        self.assertEqual(list(outside.iterdir()), [])

    def test_layout_rejects_junction_at_data_root(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        junction = self.root / "data"
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(junction.rmdir)
        else:
            junction.symlink_to(outside, target_is_directory=True)
            self.addCleanup(junction.unlink)
        with self.assertRaises(self.models.BackupError):
            self.paths.resolve_layout(self.root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_reparse_attribute_on_target_and_control_ancestors_rejected(self) -> None:
        self.layout.work_dir.mkdir(parents=True)
        self.layout.rescue_dir.mkdir()
        target = self.layout.data_dir / "customers.json"
        target.write_bytes(b"[]")
        for flagged, relative in ((target, "customers.json"),
                                  (self.layout.work_dir, ".backup-work/id/upload.zip"),
                                  (self.layout.rescue_dir, "backups/id/rescue.zip"),
                                  (self.root, "photo_scan/01.jpg")):
            actual_lstat = Path.lstat

            def flagged_lstat(path, *args, **kwargs):
                value = actual_lstat(path, *args, **kwargs)
                if path == flagged:
                    return SimpleNamespace(st_mode=value.st_mode,
                                           st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                return value

            with self.subTest(relative=relative):
                with patch.object(Path, "lstat", flagged_lstat):
                    with self.assertRaises(self.models.BackupError):
                        self.paths.owned_path(self.layout, relative)
        self.assertEqual(target.read_bytes(), b"[]")

    def test_reparse_ancestor_introduced_during_resolution_is_rechecked(self) -> None:
        self.layout.data_dir.mkdir()
        actual_resolve = Path.resolve
        actual_lstat = Path.lstat
        changed = False

        def resolving(path, *args, **kwargs):
            nonlocal changed
            result = actual_resolve(path, *args, **kwargs)
            changed = True
            return result

        def changed_lstat(path, *args, **kwargs):
            value = actual_lstat(path, *args, **kwargs)
            if changed and path == self.layout.data_dir:
                return SimpleNamespace(st_mode=value.st_mode,
                                       st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return value

        with patch.object(Path, "resolve", resolving), patch.object(Path, "lstat", changed_lstat):
            with self.assertRaises(self.models.BackupError):
                self.paths.owned_path(self.layout, "customers.json")

    def test_non_directory_ancestor_rejected(self) -> None:
        self.layout.data_dir.mkdir()
        (self.layout.data_dir / "crawler_photos").write_bytes(b"not a directory")
        with self.assertRaises(self.models.BackupError):
            self.paths.owned_path(self.layout, "crawler_photos/01.jpg")

    def test_windows_component_length_boundary(self) -> None:
        # Windows counts UTF-16 code units, not Python Unicode code points.
        self.assertEqual(self.paths.validate_member_path("data/crawler_photos/" + "a" * 255),
                         "data/crawler_photos/" + "a" * 255)
        for part in ("a" * 256, "\U0001f600" * 128):
            with self.subTest(part_length=len(part)):
                with self.assertRaises(self.models.BackupError):
                    self.paths.validate_member_path("data/crawler_photos/" + part)

    def test_owned_path_length_includes_root_and_missing_descendants(self) -> None:
        prefix = str(self.layout.data_dir / "crawler_photos")
        # MAX_PATH has room for 259 units plus its terminating NUL.
        leaf_length = 259 - len(prefix.encode("utf-16-le")) // 2 - 1
        self.assertGreater(leaf_length, 0)
        self.assertLess(leaf_length, 255)
        allowed = self.paths.owned_path(self.layout, "crawler_photos/" + "a" * leaf_length)
        self.assertEqual(len(str(allowed).encode("utf-16-le")) // 2, 259)
        with self.assertRaises(self.models.BackupError):
            self.paths.owned_path(self.layout, "crawler_photos/" + "a" * (leaf_length + 1))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_space_margin_required(self) -> None:
        limits = self.models.BackupLimits()
        required = 17 + 256 * 1024**2
        for free, accepted in ((required - 1, False), (required, True), (required + 1, True)):
            with self.subTest(free=free):
                with patch.object(shutil, "disk_usage", return_value=(0, 0, free)):
                    if accepted:
                        self.paths.require_space(self.root, 17, limits)
                    else:
                        with self.assertRaises(self.models.BackupError) as caught:
                            self.paths.require_space(self.root, 17, limits)
                        self.assertEqual(caught.exception.code, "insufficient_space")

    def test_injected_space_margin_controls_behavior(self) -> None:
        limits = self.models.BackupLimits(space_margin_bytes=3)
        with patch.object(shutil, "disk_usage", return_value=(20, 9, 11)):
            self.paths.require_space(self.root, 8, limits)
            with self.assertRaises(self.models.BackupError):
                self.paths.require_space(self.root, 9, limits)

    def test_real_space_check_finds_existing_parent_without_creating_paths(self) -> None:
        free = shutil.disk_usage(self.root).free
        self.paths.require_space(self.layout.work_dir / "id/upload.zip", 0,
                                 self.models.BackupLimits(space_margin_bytes=0))
        with self.assertRaises(self.models.BackupError):
            self.paths.require_space(self.root, free + 1024**3,
                                     self.models.BackupLimits(space_margin_bytes=0))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_space_budget_rejected(self) -> None:
        for value in (-1, 1.5, True):
            with self.subTest(value=value):
                with self.assertRaises(self.models.BackupError):
                    self.paths.require_space(self.root, value, self.models.BackupLimits())

    def test_os_errors_do_not_expose_paths_or_exception_text(self) -> None:
        with patch.object(Path, "lstat", side_effect=PermissionError("synthetic-private-marker")):
            with self.assertRaises(self.models.BackupError) as caught:
                self.paths.owned_path(self.layout, "customers.json")
        self.assertNotIn("synthetic-private-marker", str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))
        with patch.object(shutil, "disk_usage", side_effect=OSError("synthetic-private-marker")):
            with self.assertRaises(self.models.BackupError) as caught:
                self.paths.require_space(self.root, 0, self.models.BackupLimits())
        self.assertNotIn("synthetic-private-marker", str(caught.exception))

    def test_importing_backup_does_not_load_business_modules(self) -> None:
        # A fresh interpreter catches imports even when the full suite loaded server.
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import sys; import app.backup.models; import app.backup.paths; "
             "assert not ({'app.config', 'app.memory', 'app.llm', 'app.email_agent', "
             "'app.credential_store', 'app.outreach_smtp'} & sys.modules.keys())"],
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
