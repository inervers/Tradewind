from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class CustomerImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.customers_file = Path(self.temp_dir.name) / "customers.json"
        self.file_patch = patch.object(server, "CUSTOMERS_FILE", self.customers_file)
        self.file_patch.start()

    def tearDown(self) -> None:
        self.file_patch.stop()
        self.temp_dir.cleanup()

    def test_repeated_import_is_skipped_without_rewriting_file(self) -> None:
        csv_text = "name,country,website,email,phone\nGlow Clinic,香港,https://glow.hk,hi@glow.hk,+852 2345 6789"
        first = server.api_customer_import({"text": csv_text, "source": "maps"})
        self.assertEqual(first["added"], 1)

        with patch.object(server, "_write_json", wraps=server._write_json) as write_json:
            second = server.api_customer_import({"text": csv_text, "source": "maps"})

        self.assertEqual(second["added"], 0)
        self.assertEqual(second["duplicates"], 1)
        write_json.assert_not_called()

    def test_import_deduplicates_by_website_even_when_name_changes(self) -> None:
        first = "name,country,website\nGlow Medical,香港,https://www.glow.hk/services"
        second = "name,country,website\nGlow Medical Centre,香港,https://glow.hk/about"
        server.api_customer_import({"text": first})

        result = server.api_customer_import({"text": second})

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["duplicates"], 1)

    def test_batch_delete_writes_once_and_returns_removed_count(self) -> None:
        csv_text = "name,country\nAlpha,香港\nBeta,香港\nGamma,香港"
        imported = server.api_customer_import({"text": csv_text})
        ids = [item["id"] for item in imported["items"][:2]]

        with patch.object(server, "_write_json", wraps=server._write_json) as write_json:
            result = server.api_customer_batch_delete({"ids": ids})

        self.assertEqual(result["removed"], 2)
        self.assertEqual(result["total"], 1)
        write_json.assert_called_once()

    def test_import_merges_unique_instruments_and_gap_recommendations_once(self) -> None:
        csv_text = (
            "name,country,instruments,gap_recs\n"
            'Glow Clinic,香港,"激光|皮秒|激光","水光设备; 水光设备; 皮肤检测仪"'
        )

        result = server.api_customer_import({"text": csv_text, "source": "maps"})

        self.assertEqual(result["added"], 1)
        self.assertEqual(
            result["items"][0]["notes"],
            "店舖用儀器：激光、皮秒；缺品推薦：水光设备；皮肤检测仪",
        )


if __name__ == "__main__":
    unittest.main()
