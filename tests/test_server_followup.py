from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from app.customer_followup import FollowupStore
from app.outreach_store import OutreachStore


class FollowupApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / "customers.json"
        self.db = self.root / "memory.db"
        self.customers = [dict(id="c-001", name="Demo Clinic", country="香港", city="",
                               website="https://demo.example", email="buyer@example.test",
                               phone="+852 61234567", notes="Equipment description", status="new", source="maps"),
                          dict(id="c-002", name="Other Clinic", country="香港", city="", website="",
                               email="other@example.test", phone="", notes="", status="new", source="webs")]
        self.file.write_text(json.dumps(self.customers), encoding="utf-8")
        self.patches = [patch.object(server, "CUSTOMERS_FILE", self.file),
                        patch.object(server, "MEMORY_DB_FILE", self.db)]
        for item in self.patches:
            item.start()
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def record(self, customer="c-001", **overrides):
        body = dict(contacted_at="2026-09-01T10:30:00+08:00", channel="phone",
                    outcome="in_progress", content="Private followup marker", request_id="api-record")
        body.update(overrides)
        return self.client.post(f"/api/customers/{customer}/followup/records", json=body)

    def plan(self, customer="c-001", **overrides):
        body = dict(due_date=date.today().isoformat(), action="Private pending marker",
                    request_id="api-plan", expected_plan_id=None)
        body.update(overrides)
        return self.client.post(f"/api/customers/{customer}/followup/plans", json=body)

    def test_record_and_plan_persist_without_changing_customer_notes_or_status(self) -> None:
        response = self.record()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.plan().status_code, 200)
        response = self.client.get("/api/customers/c-001/followup/timeline")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({i["kind"] for i in response.json()["items"]}, {"record", "plan_created"})
        persisted = json.loads(self.file.read_text(encoding="utf-8"))[0]
        self.assertEqual(persisted["notes"], "Equipment description")
        self.assertEqual(persisted["status"], "new")
        self.assertEqual(FollowupStore(self.db).active_plan("c-001")["action"], "Private pending marker")

    def test_retry_and_stale_plan_return_conflict_not_duplicate(self) -> None:
        first = self.plan()
        self.assertEqual(first.status_code, 200)
        first_id = first.json()["item"]["id"]
        self.assertEqual(self.plan().json()["item"]["id"], first_id)
        self.assertEqual(self.plan(action="Changed").status_code, 409)
        second = self.plan(request_id="api-plan-2", expected_plan_id=first_id)
        self.assertEqual(second.status_code, 200)
        response = self.client.post(f"/api/customers/c-001/followup/plans/{first_id}/complete")
        self.assertEqual(response.status_code, 409)

    def test_unknown_customer_or_foreign_record_is_not_found(self) -> None:
        self.assertEqual(self.record("missing").status_code, 404)
        record = self.record().json()["item"]
        self.assertEqual(self.client.delete(f"/api/customers/c-002/followup/records/{record['id']}").status_code, 404)
        plan = self.plan().json()["item"]
        self.assertEqual(self.client.post(f"/api/customers/c-002/followup/plans/{plan['id']}/cancel").status_code, 404)

    def test_delayed_record_retry_after_deletion_is_conflict_not_resurrection(self) -> None:
        record = self.record().json()["item"]
        self.assertEqual(self.client.delete(
            f"/api/customers/c-001/followup/records/{record['id']}").status_code, 200)
        self.assertEqual(self.record().status_code, 409)
        self.assertEqual(FollowupStore(self.db).timeline("c-001")["items"], [])

    def test_invalid_fields_and_naive_time_are_rejected(self) -> None:
        for values in [{"content": " "}, {"content": "x" * 4001}, {"channel": "smtp"},
                       {"contacted_at": "2026-09-01T10:00:00"}]:
            with self.subTest(values=list(values)):
                self.assertEqual(self.record(**values).status_code, 422)
        self.assertEqual(self.plan(due_date="2026-02-30").status_code, 422)
        self.assertEqual(self.plan(expected_plan_id=True).status_code, 422)
        self.assertEqual(self.client.get("/api/customers/c-001/followup/timeline?limit=0").status_code, 422)

    def test_origin_protection_applies_to_followup_writes(self) -> None:
        response = self.client.post("/api/customers/c-001/followup/records", json={},
                                    headers={"Origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)

    def test_summary_uses_local_date_and_only_existing_customers(self) -> None:
        self.plan(due_date=(date.today() - timedelta(days=1)).isoformat())
        self.plan("c-002", request_id="other-plan")
        self.record()
        result = self.client.get("/api/customers/followup-summary")
        self.assertEqual(result.status_code, 200)
        body = result.json()
        self.assertEqual(body["today"], date.today().isoformat())
        by_id = {item["customer_id"]: item for item in body["items"]}
        self.assertEqual(by_id["c-001"]["due_status"], "overdue")
        self.assertEqual(by_id["c-002"]["due_status"], "today")
        self.assertIsNotNone(by_id["c-001"]["last_contact_at"])

    def test_deleted_customer_id_is_never_reused_on_add_or_import(self) -> None:
        self.client.delete("/api/customers/c-002")
        new = self.client.post("/api/customers", json={"name": "New Clinic"}).json()["item"]
        self.assertNotEqual(new["id"], "c-002")
        self.client.delete(f"/api/customers/{new['id']}")
        imported = self.client.post("/api/customers/import", json={"text": "name,country\nImported,香港"}).json()["items"][0]
        self.assertNotIn(imported["id"], {"c-002", new["id"]})
        self.assertEqual(self.client.get("/api/customers").json()[0]["id"], "c-001")

    def test_delete_cleans_followup_but_keeps_outreach_and_suppression(self) -> None:
        self.record()
        self.plan()
        store = OutreachStore(self.db)
        job = store.create_job(customer_id="c-001", customer_name="Demo Clinic", channel="email",
                               provider="qq_smtp", sender_account="sender@example.test", recipient="buyer@example.test",
                               subject="Demo", body="Demo body")
        store.add_suppression("email", "buyer@example.test", "Do not contact")
        response = self.client.delete("/api/customers/c-001")
        self.assertTrue(response.json()["ok"])
        self.assertEqual(FollowupStore(self.db).timeline("c-001")["items"], [])
        self.assertEqual(store.get_job(job["id"])["status"], "suppressed")
        self.assertEqual(len(store.list_suppressions()), 1)

    def test_cleanup_failure_is_visible_and_repeat_delete_finishes_it(self) -> None:
        self.record()
        with patch.object(FollowupStore, "delete_customers", side_effect=sqlite3.OperationalError("private path")):
            response = self.client.delete("/api/customers/c-001")
        result = response.json()
        self.assertFalse(result["ok"])
        self.assertTrue(result["customer_deleted"])
        self.assertNotIn("private path", result["error"])
        self.assertEqual(len(FollowupStore(self.db).timeline("c-001")["items"]), 1)
        self.assertTrue(self.client.delete("/api/customers/c-001").json()["ok"])
        self.assertEqual(FollowupStore(self.db).timeline("c-001")["items"], [])

    def test_batch_delete_cleans_only_selected_customers(self) -> None:
        self.plan()
        self.plan("c-002", request_id="plan-other")
        response = self.client.post("/api/customers/batch-delete", json={"ids": ["c-001"]})
        self.assertEqual(response.json()["removed"], 1)
        self.assertIsNone(FollowupStore(self.db).active_plan("c-001"))
        self.assertIsNotNone(FollowupStore(self.db).active_plan("c-002"))

    def test_corrupt_json_never_overwrites_or_cleans_saved_followup(self) -> None:
        self.record()
        self.file.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.client.post("/api/customers", json={"name": "Must not overwrite"}).status_code, 500)
        self.assertEqual(self.file.read_text(encoding="utf-8"), "{broken")
        report = server.cleanup_orphan_followups()
        self.assertFalse(report["ok"])
        self.assertEqual(len(FollowupStore(self.db).timeline("c-001")["items"]), 1)

    def test_duplicate_customer_ids_prevent_orphan_cleanup(self) -> None:
        self.record()
        self.file.write_text(json.dumps([self.customers[0], self.customers[0]]), encoding="utf-8")
        self.assertFalse(server.cleanup_orphan_followups()["ok"])
        self.assertEqual(len(FollowupStore(self.db).timeline("c-001")["items"]), 1)

    def test_missing_customer_file_does_not_trigger_automatic_cleanup(self) -> None:
        self.record()
        self.file.unlink()
        self.assertFalse(server.cleanup_orphan_followups()["ok"])
        self.assertEqual(len(FollowupStore(self.db).timeline("c-001")["items"]), 1)

    def test_known_outreach_events_do_not_mislabel_failed_submission(self) -> None:
        store = OutreachStore(self.db)
        job = store.create_job(customer_id="c-001", customer_name="Demo", channel="email",
                               provider="qq_smtp", sender_account="sender@example.test", recipient="buyer@example.test",
                               subject="Demo", body="Demo body")
        store.claim_email_send(job["id"])
        store.complete_email_send(job["id"], "failed", "rejected", "Rejected")
        response = self.client.get("/api/customers/c-001/followup/timeline")
        self.assertEqual(response.status_code, 200)
        events = response.json()["items"]
        self.assertIn("outreach_failed", {event["kind"] for event in events})
        self.assertNotIn("outreach_accepted", {event["kind"] for event in events})
        self.assertNotIn("Demo body", json.dumps(events))

    def test_history_is_filtered_before_global_limit_and_pagination(self) -> None:
        store = OutreachStore(self.db)
        store.create_job(customer_id="c-001", customer_name="Demo", channel="whatsapp",
                         provider="whatsapp_link", sender_account="", recipient="85261234567", subject="", body="Hello")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for n in range(1005):
                conn.execute("""INSERT INTO outreach_jobs (customer_id,customer_name_snapshot,channel,provider,
                    recipient,body,content_hash,status,created_at,updated_at) VALUES (?,?, 'email','qq_smtp',
                    'x@example.test','Hello','hash','accepted',1800000000,1800000000)""", (f"other-{n}", "Other"))
        self.record()
        first = self.client.get("/api/customers/c-001/followup/timeline?limit=1").json()
        second = self.client.get(f"/api/customers/c-001/followup/timeline?limit=1&offset={first['next_offset']}").json()
        self.assertTrue(first["has_more"])
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        all_items = self.client.get("/api/customers/c-001/followup/timeline").json()["items"]
        self.assertIn("outreach_created", {item["kind"] for item in all_items})
        self.assertEqual(len(all_items), 2)


if __name__ == "__main__":
    unittest.main()
