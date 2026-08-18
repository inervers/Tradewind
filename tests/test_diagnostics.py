import io
import json
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import server


class DiagnosticExportTests(unittest.TestCase):
    def test_export_contains_counts_but_not_private_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            photos_dir = data_dir / "crawler_photos"
            store_dir = photos_dir / "Private Clinic"
            store_dir.mkdir(parents=True)

            customers = data_dir / "customers.json"
            products = data_dir / "products.json"
            templates = data_dir / "emails.json"
            memory = data_dir / "tradewind_memory.db"
            customers.write_text(
                json.dumps([{"name": "Private Clinic", "email": "owner@example.test"}]),
                encoding="utf-8",
            )
            products.write_text("[]", encoding="utf-8")
            templates.write_text("[]", encoding="utf-8")
            memory.write_bytes(b"private-history")
            (store_dir / "private-photo.jpg").write_bytes(b"\xff\xd8\xffprivate")
            (data_dir / "crawler_errors.log").write_text(
                "ConnectTimeout: customer Private Clinic https://private.example.test sk-private-secret\nHTTP 503",
                encoding="utf-8",
            )

            replacements = {
                "ROOT": Path(temp_dir),
                "DATA_DIR": data_dir,
                "CUSTOMERS_FILE": customers,
                "PRODUCTS_FILE": products,
                "TEMPLATES_FILE": templates,
                "MEMORY_DB_FILE": memory,
                "CRAWLER_PHOTOS_DIR": photos_dir,
            }
            with ExitStack() as stack:
                for name, value in replacements.items():
                    stack.enter_context(patch.object(server, name, value))
                stack.enter_context(patch.object(server, "get_active_provider", return_value="deepseek"))
                stack.enter_context(
                    patch.object(
                        server,
                        "get_provider_config",
                        side_effect=lambda _pid: {"api_key": "sk-private-secret", "model": "safe-model"},
                    )
                )
                stack.enter_context(
                    patch.object(
                        server,
                        "get_vision_config",
                        return_value={"provider": "qwen", "model": "qwen3-vl-plus", "api_key": "vision-private"},
                    )
                )
                stack.enter_context(
                    patch.object(
                        server,
                        "get_vision_provider_config",
                        side_effect=lambda _pid: {"api_key": "vision-private", "model": "safe-vision-model"},
                    )
                )
                response = server.export_diagnostics()

            with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
                report = json.loads(archive.read("diagnostic.json"))
                all_content = b"\n".join(archive.read(name) for name in archive.namelist()).decode("utf-8")

            self.assertEqual(report["data"]["customers"]["item_count"], 1)
            self.assertEqual(report["data"]["crawler_photos"]["photo_count"], 1)
            self.assertEqual(report["errors"]["crawler"]["error_types"], {"ConnectTimeout": 1})
            self.assertEqual(report["errors"]["crawler"]["http_statuses"], {"503": 1})
            self.assertTrue(report["models"]["providers"]["deepseek"]["configured"])
            for private_value in (
                "Private Clinic",
                "owner@example.test",
                "private.example.test",
                "sk-private-secret",
                "vision-private",
                "private-photo.jpg",
                temp_dir,
            ):
                self.assertNotIn(private_value, all_content)


if __name__ == "__main__":
    unittest.main()
