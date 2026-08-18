from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import memory


class HistoryBatchDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(memory, "MEMORY_DB", Path(self.temp_dir.name) / "memory.db")
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_batch_delete_removes_selected_history_only(self) -> None:
        for customer in ("Alpha", "Beta", "Gamma"):
            memory.save_email(customer, "香港", "仪器", f"Email for {customer}")
        items = memory.recent_emails(10)
        selected = [items[0]["id"], items[2]["id"]]

        removed = memory.delete_emails(selected)
        remaining = memory.recent_emails(10)

        self.assertEqual(removed, 2)
        self.assertEqual([item["customer"] for item in remaining], ["Beta"])

    def test_batch_delete_ignores_duplicate_ids(self) -> None:
        memory.save_email("Alpha", "香港", "仪器", "Email")
        item_id = memory.recent_emails(1)[0]["id"]

        removed = memory.delete_emails([item_id, item_id])

        self.assertEqual(removed, 1)


if __name__ == "__main__":
    unittest.main()
