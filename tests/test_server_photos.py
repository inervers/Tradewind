from __future__ import annotations

import base64
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from app.config import vision_key_format_error


class VisionConfigValidationTests(unittest.TestCase):
    def test_volc_rejects_obviously_malformed_credentials(self) -> None:
        self.assertIn("不能包含", vision_key_format_error("volc", '"bad key"'))
        self.assertIn("Access Key ID", vision_key_format_error("volc", "AKLT-example"))
        self.assertIn("推理接入点", vision_key_format_error("volc", "ep-example"))
        self.assertEqual(vision_key_format_error("volc", "plain-api-key-token"), "")
        self.assertEqual(vision_key_format_error("glm", '"provider-specific-format"'), "")


class PhotoLibraryTests(unittest.TestCase):
    def test_library_groups_by_store_and_batch_delete_is_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shop = root / "美容店"
            shop.mkdir()
            (shop / "01.jpg").write_bytes(b"\xff\xd8\xffphoto")
            (shop / "notes.txt").write_text("keep", encoding="utf-8")
            with patch.object(server, "CRAWLER_PHOTOS_DIR", root):
                snapshot = server.api_photo_library()
                item = snapshot["stores"][0]
                result = server.api_photo_library_delete(server.PhotoDeleteRequest(items=[{
                    "store_id": item["store_id"], "photo_id": item["photos"][0]["photo_id"],
                }]))

            self.assertEqual(snapshot["total"], 1)
            self.assertEqual(result["removed"], 1)
            self.assertTrue((shop / "notes.txt").exists())

    def test_store_can_be_renamed_and_deleted_without_touching_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shop = root / "旧店名"
            shop.mkdir()
            (shop / "01.jpg").write_bytes(b"\xff\xd8\xffphoto")
            with patch.object(server, "CRAWLER_PHOTOS_DIR", root):
                store_id = server.api_photo_library()["stores"][0]["store_id"]
                renamed = server.api_photo_store_rename(server.PhotoStoreRenameRequest(
                    store_id=store_id, name="新店/分店",
                ))
                deleted = server.api_photo_store_delete(server.PhotoStoreDeleteRequest(
                    store_id=renamed["store_id"],
                ))

            self.assertTrue(renamed["ok"])
            self.assertEqual(renamed["name"], "新店_分店")
            self.assertTrue(deleted["ok"])
            self.assertEqual(deleted["removed"], 1)
            self.assertFalse((root / "新店_分店").exists())

    def test_store_delete_preserves_non_photo_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shop = root / "美容店"
            shop.mkdir()
            (shop / "01.png").write_bytes(b"\x89PNGphoto")
            (shop / "notes.txt").write_text("keep", encoding="utf-8")
            with patch.object(server, "CRAWLER_PHOTOS_DIR", root):
                store_id = server.api_photo_library()["stores"][0]["store_id"]
                result = server.api_photo_store_delete(server.PhotoStoreDeleteRequest(store_id=store_id))

            self.assertTrue(result["ok"])
            self.assertEqual(result["removed"], 1)
            self.assertIn("非照片文件", result["warning"])
            self.assertTrue((shop / "notes.txt").exists())


class PhotoTaskTests(unittest.TestCase):
    def tearDown(self) -> None:
        server.PHOTO_TASKS.clear()

    def _wait(self, task_id: str) -> dict:
        deadline = time.time() + 2
        while time.time() < deadline:
            task = server.PHOTO_TASKS[task_id]
            if task["status"] != "running" and not server.PHOTO_RUN_LOCK.locked():
                return task
            time.sleep(0.01)
        return server.PHOTO_TASKS[task_id]

    @patch("app.crawler.vision_analyzer.analyze_image_bytes_with_meta")
    def test_photo_scan_saves_unique_file_and_returns_device(self, analyze) -> None:
        analyze.return_value = ([{
            "device": "射频仪", "brand": "Test", "purpose": "紧肤", "confidence": 0.91,
        }], None, {"provider": "qwen", "model": "qwen3-vl-plus"})
        encoded = base64.b64encode(b"\xff\xd8\xffphoto").decode()
        request = server.PhotoScanRequest(images=[{"filename": "设备?.jpg", "data_base64": encoded}])

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(server, "ROOT", Path(tmp)), \
                patch.object(server, "PHOTO_SCAN_DIR", Path(tmp) / "photo_scan"), \
                patch.object(server, "get_vision_config", return_value={"api_key": "configured"}):
            task_id = server.api_photos_start(request)["task_id"]
            task = self._wait(task_id)
            saved_name = Path(task["results"][0]["saved_path"]).name

        self.assertEqual(task["status"], "done")
        self.assertTrue(task["results"][0]["has_device"])
        self.assertEqual(task["results"][0]["provider"], "qwen")
        self.assertNotIn("?", saved_name)

    def test_photo_scan_rejects_unconfigured_vision(self) -> None:
        request = server.PhotoScanRequest(images=[{"filename": "a.jpg", "data_base64": "eA=="}])
        with patch.object(server, "get_vision_config", return_value={"api_key": ""}):
            result = server.api_photos_start(request)
        self.assertEqual(result["task_id"], "")
        self.assertIn("API Key", result["error"])


if __name__ == "__main__":
    unittest.main()
