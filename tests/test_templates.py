from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class TemplatePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.templates_file = Path(self.temp_dir.name) / "emails.json"
        self.file_patch = patch.object(server, "TEMPLATES_FILE", self.templates_file)
        self.file_patch.start()

    def tearDown(self) -> None:
        self.file_patch.stop()
        self.temp_dir.cleanup()

    def test_same_content_is_not_promoted_twice(self) -> None:
        first = server.api_template_add(server.ProductRequest(
            title="复盘 A",
            content="Hi Glow Clinic,\n\nReply for the catalog.",
            tags=["邮件", "模板", "人工复盘"],
        ))
        second = server.api_template_add(server.ProductRequest(
            title="复盘 B",
            content="  hi glow clinic, reply   for the catalog.  ",
            tags=["邮件", "模板", "人工复盘"],
        ))

        self.assertTrue(first["ok"])
        self.assertFalse(first.get("duplicate", False))
        self.assertTrue(second["duplicate"])
        self.assertEqual(server.api_templates(), [first["item"]])

    def test_blank_template_is_rejected_without_writing(self) -> None:
        result = server.api_template_add(server.ProductRequest(title=" ", content=" "))

        self.assertFalse(result["ok"])
        self.assertFalse(self.templates_file.exists())


if __name__ == "__main__":
    unittest.main()
