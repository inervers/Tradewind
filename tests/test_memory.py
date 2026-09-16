from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import memory


class MemoryWorkflowTests(unittest.TestCase):
    def test_save_email_once_reuses_workflow_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            with patch.object(memory, "MEMORY_DB", db):
                first = memory.save_email_once(
                    "wf-001", "Demo Clinic", "HK", "Laser", "Draft", 4, "en", "email"
                )
                second = memory.save_email_once(
                    "wf-001", "Demo Clinic", "HK", "Laser", "Draft", 4, "en", "email"
                )
            conn = sqlite3.connect(db)
            count = conn.execute(
                "SELECT COUNT(*) FROM emails WHERE workflow_id='wf-001'"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(first, second)
        self.assertEqual(count, 1)

    def test_old_email_table_is_migrated_without_losing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE emails (id INTEGER PRIMARY KEY AUTOINCREMENT, customer TEXT NOT NULL, "
                "country TEXT DEFAULT '', product TEXT DEFAULT '', email TEXT NOT NULL, score REAL DEFAULT 0, "
                "language TEXT DEFAULT 'zh-hant', format TEXT DEFAULT 'email', created_at REAL NOT NULL)"
            )
            conn.execute(
                "INSERT INTO emails (customer, email, created_at) VALUES ('Old', 'Old draft', 1)"
            )
            conn.commit()
            conn.close()
            with patch.object(memory, "MEMORY_DB", db):
                memory.save_email_once("wf-new", "New", "HK", "Laser", "New draft")
            conn = sqlite3.connect(db)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(emails)")}
            rows = conn.execute("SELECT customer FROM emails ORDER BY id").fetchall()
            conn.close()
        self.assertIn("workflow_id", columns)
        self.assertEqual(rows, [("Old",), ("New",)])


if __name__ == "__main__":
    unittest.main()
