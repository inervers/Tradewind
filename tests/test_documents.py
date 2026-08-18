from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from server import _doc_to_text


class DocumentExtractionTests(unittest.TestCase):
    def test_plain_text_does_not_depend_on_markitdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "template.txt"
            path.write_text("Dear Clinic,\nThis is a follow-up message.", encoding="utf-8")

            text, ocr_used = _doc_to_text(path, ".txt")

        self.assertIn("Dear Clinic", text)
        self.assertFalse(ocr_used)

    def test_docx_uses_embedded_document_xml(self) -> None:
        document_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:t>First paragraph</w:t></w:r></w:p>
          <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p></w:body>
        </w:document>"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "template.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", document_xml)

            text, ocr_used = _doc_to_text(path, ".docx")

        self.assertEqual(text, "First paragraph\nSecond paragraph")
        self.assertFalse(ocr_used)


if __name__ == "__main__":
    unittest.main()
