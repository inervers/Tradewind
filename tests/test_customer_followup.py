from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from app.customer_followup import FollowupConflict, FollowupNotFound, FollowupStore


class FollowupStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "memory.db"
        self.store = FollowupStore(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def record(self, **overrides) -> dict:
        args = dict(contacted_at=1700000000.0, channel="phone", outcome="in_progress",
                    content="  Asked for specifications  ", request_id="record-1")
        args.update(overrides)
        return self.store.add_record("c-demo", "Demo Clinic", **args)

    def plan(self, **overrides) -> dict:
        args = dict(due_date="2026-10-03", action="  Send specifications  ",
                    request_id="plan-1", expected_plan_id=None)
        args.update(overrides)
        return self.store.set_plan("c-demo", "Demo Clinic", **args)

    def test_record_persists_without_touching_other_tables(self) -> None:
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE preserved (value TEXT)")
            conn.execute("INSERT INTO preserved VALUES ('keep')")
        item = self.record()
        reopened = FollowupStore(self.db)
        page = reopened.timeline("c-demo")
        self.assertEqual(page["items"][0]["content"], "Asked for specifications")
        self.assertEqual(page["items"][0]["record_id"], item["id"])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT value FROM preserved").fetchone()[0], "keep")

    def test_record_retry_returns_same_id(self) -> None:
        first = self.record()
        self.assertEqual(self.record()["id"], first["id"])
        self.assertEqual(len(self.store.timeline("c-demo")["items"]), 1)

    def test_record_retry_cannot_change_content_or_customer(self) -> None:
        self.record()
        with self.assertRaises(FollowupConflict):
            self.record(content="Different")
        with self.assertRaises(FollowupConflict):
            self.store.add_record("c-other", "Other", contacted_at=1700000000,
                                  channel="phone", outcome="in_progress",
                                  content="Asked for specifications", request_id="record-1")

    def test_record_validation(self) -> None:
        invalid = [{"content": " "}, {"content": "x" * 4001}, {"channel": "smtp"},
                   {"outcome": "accepted"}, {"request_id": ""},
                   {"contacted_at": float("nan")}, {"contacted_at": -1}]
        for values in invalid:
            with self.subTest(values=list(values)):
                with self.assertRaises(ValueError):
                    self.record(**values)

    def test_record_max_length_is_accepted(self) -> None:
        self.assertEqual(len(self.record(content="x" * 4000)["content"]), 4000)

    def test_record_delete_checks_customer_ownership(self) -> None:
        item = self.record()
        with self.assertRaises(FollowupNotFound):
            self.store.delete_record("c-other", item["id"])
        self.assertEqual(len(self.store.timeline("c-demo")["items"]), 1)
        self.store.delete_record("c-demo", item["id"])
        self.assertEqual(self.store.timeline("c-demo")["items"], [])

    def test_deleted_record_retry_cannot_recreate_it_after_restart(self) -> None:
        item = self.record()
        self.store.delete_record("c-demo", item["id"])
        self.store = FollowupStore(self.db)
        with self.assertRaises(FollowupConflict):
            self.record()
        self.assertEqual(self.store.timeline("c-demo")["items"], [])

    def test_deleted_request_cannot_be_reused_for_other_content_or_customer(self) -> None:
        item = self.record()
        self.store.delete_record("c-demo", item["id"])
        with self.assertRaises(FollowupConflict):
            self.record(content="Different")
        with self.assertRaises(FollowupConflict):
            self.store.add_record("c-other", "Other", contacted_at=1700000000,
                                  channel="phone", outcome="in_progress",
                                  content="Asked for specifications", request_id="record-1")

    def test_customer_delete_cleans_deleted_request_marker(self) -> None:
        item = self.record()
        self.store.delete_record("c-demo", item["id"])
        self.store.delete_customers(["c-demo"])
        self.assertTrue(self.record()["id"])

    def test_orphan_cleanup_preserves_live_customer_deleted_request_marker(self) -> None:
        item = self.record()
        self.store.delete_record("c-demo", item["id"])
        self.store.prune_orphans(["c-demo"])
        with self.assertRaises(FollowupConflict):
            self.record()
        self.store.prune_orphans([])
        self.assertTrue(self.record()["id"])

    def test_plan_persists_and_retry_does_not_duplicate(self) -> None:
        first = self.plan()
        self.assertEqual(self.plan()["id"], first["id"])
        self.assertEqual(FollowupStore(self.db).active_plan("c-demo")["action"], "Send specifications")
        self.assertEqual(len(self.store.timeline("c-demo")["items"]), 1)

    def test_second_unconditional_plan_is_rejected(self) -> None:
        first = self.plan()
        with self.assertRaises(FollowupConflict):
            self.plan(request_id="plan-2")
        self.assertEqual(self.store.active_plan("c-demo")["id"], first["id"])

    def test_replacement_keeps_prior_plan_and_single_active(self) -> None:
        first = self.plan()
        second = self.plan(due_date="2026-10-06", action="Call again",
                           request_id="plan-2", expected_plan_id=first["id"])
        self.assertEqual(second["previous_plan_id"], first["id"])
        self.assertEqual(self.store.active_plan("c-demo")["id"], second["id"])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT status FROM customer_followup_plans WHERE id=?",
                                          (first["id"],)).fetchone()[0], "superseded")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM customer_followup_plans WHERE status='active'").fetchone()[0], 1)

    def test_stale_replacement_and_completion_cannot_change_new_plan(self) -> None:
        first = self.plan()
        second = self.plan(request_id="plan-2", expected_plan_id=first["id"])
        with self.assertRaises(FollowupConflict):
            self.plan(request_id="plan-3", expected_plan_id=first["id"])
        with self.assertRaises(FollowupConflict):
            self.store.close_plan("c-demo", first["id"], "completed")
        self.assertEqual(self.store.active_plan("c-demo")["id"], second["id"])

    def test_completion_is_idempotent_and_new_plan_survives_retry(self) -> None:
        first = self.plan()
        self.store.close_plan("c-demo", first["id"], "completed")
        second = self.plan(request_id="plan-2")
        self.assertEqual(self.store.close_plan("c-demo", first["id"], "completed")["status"], "completed")
        self.assertEqual(self.store.active_plan("c-demo")["id"], second["id"])

    def test_cancel_removes_active_not_history(self) -> None:
        first = self.plan()
        self.store.close_plan("c-demo", first["id"], "cancelled")
        self.assertIsNone(self.store.active_plan("c-demo"))
        self.assertEqual({i["kind"] for i in self.store.timeline("c-demo")["items"]}, {"plan_created", "plan_cancelled"})

    def test_close_checks_customer_and_status(self) -> None:
        item = self.plan()
        with self.assertRaises(FollowupNotFound):
            self.store.close_plan("c-other", item["id"], "completed")
        with self.assertRaises(ValueError):
            self.store.close_plan("c-demo", item["id"], "sent")

    def test_plan_request_conflict(self) -> None:
        self.plan()
        with self.assertRaises(FollowupConflict):
            self.plan(action="Changed")

    def test_plan_date_and_length_validation(self) -> None:
        for values in [{"due_date": "2026-02-30"}, {"due_date": "20261003"},
                       {"action": " "}, {"action": "a" * 501}, {"request_id": ""}]:
            with self.subTest(values=list(values)):
                with self.assertRaises(ValueError):
                    self.plan(**values)
        self.assertEqual(len(self.plan(action="a" * 500)["action"]), 500)

    def test_concurrent_creation_has_one_winner(self) -> None:
        def create(number: int) -> str:
            try:
                self.plan(request_id=f"concurrent-{number}")
                return "created"
            except FollowupConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(create, [1, 2]))
        self.assertCountEqual(outcomes, ["created", "conflict"])

    def test_summary_classifies_dates_and_ignores_notes_for_contact_time(self) -> None:
        self.record(outcome="note")
        self.plan(due_date="2026-10-02")
        for cid, due in [("c-today", "2026-10-03"), ("c-future", "2026-10-04")]:
            self.store.set_plan(cid, cid, due_date=due, action="Call",
                                request_id=cid, expected_plan_id=None)
        result = self.store.summaries(["c-demo", "c-today", "c-future", "c-none"], "2026-10-03")
        self.assertEqual([result[c]["due_status"] for c in ["c-demo", "c-today", "c-future", "c-none"]],
                         ["overdue", "today", "upcoming", "unplanned"])
        self.assertIsNone(result["c-demo"]["last_contact_at"])
        self.record(outcome="replied", request_id="record-2")
        self.assertEqual(self.store.summaries(["c-demo"], "2026-10-03")["c-demo"]["last_contact_at"], 1700000000.0)

    def test_timeline_pagination_is_customer_scoped_and_stable(self) -> None:
        for number in range(7):
            self.record(request_id=f"page-{number}", content=str(number))
        first = self.store.timeline("c-demo", limit=3)
        second = self.store.timeline("c-demo", limit=3, offset=first["next_offset"])
        self.assertEqual(len(first["items"]), 3)
        self.assertTrue(first["has_more"])
        self.assertFalse(set(i["id"] for i in first["items"]) & set(i["id"] for i in second["items"]))
        self.assertEqual(self.store.timeline("c-other")["items"], [])

    def test_delete_and_orphan_cleanup_preserve_existing_external_history(self) -> None:
        self.record()
        self.plan()
        self.store.set_plan("c-keep", "Keep", due_date="2026-10-03", action="Keep",
                            request_id="keep", expected_plan_id=None)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE outreach_fixture (value TEXT)")
            conn.execute("INSERT INTO outreach_fixture VALUES ('external')")
        self.store.prune_orphans(["c-keep"])
        self.assertEqual(self.store.timeline("c-demo")["items"], [])
        self.assertIsNotNone(self.store.active_plan("c-keep"))
        self.store.delete_customers(["c-keep"])
        self.assertIsNone(self.store.active_plan("c-keep"))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT value FROM outreach_fixture").fetchone()[0], "external")


if __name__ == "__main__":
    unittest.main()
