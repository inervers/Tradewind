from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.outreach_store import (
    InvalidTransition,
    OutreachConflict,
    OutreachNotFound,
    OutreachStore,
    SuppressedContact,
)


class OutreachStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "memory.db"
        self.store = OutreachStore(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def create_email(self) -> dict:
        return self.store.create_job(
            customer_id="c-001",
            customer_name="Demo Clinic",
            channel="email",
            provider="gmail_smtp",
            sender_account="sender@example.test",
            recipient="buyer@example.test",
            subject="Demo",
            body="Body\n\nDemo Sender\nReply to stop future contact.",
        )

    def create_whatsapp(self) -> dict:
        return self.store.create_job(
            customer_id="c-002",
            customer_name="Demo Spa",
            channel="whatsapp",
            provider="whatsapp_link",
            sender_account="",
            recipient="85261234567",
            subject="",
            body="Hello",
        )

    def test_job_survives_new_store_instance(self) -> None:
        created = self.create_email()
        reopened = OutreachStore(self.db).get_job(created["id"])
        self.assertEqual(reopened["status"], "pending_review")
        self.assertEqual(reopened["recipient"], "buyer@example.test")

    def test_duplicate_active_job_is_rejected(self) -> None:
        self.create_email()
        with self.assertRaises(OutreachConflict):
            self.create_email()

    def test_invalid_transition_and_missing_job_are_rejected(self) -> None:
        job = self.create_email()
        with self.assertRaises(InvalidTransition):
            self.store.transition(job["id"], "replied")
        with self.assertRaises(OutreachNotFound):
            self.store.get_job(9999)

    def test_interrupted_send_recovers_as_unknown(self) -> None:
        job = self.create_email()
        claimed = self.store.claim_email_send(job["id"])
        self.assertEqual(claimed["status"], "sending")
        self.assertTrue(claimed["message_id"].startswith("<tradewind-"))

        recovered = OutreachStore(self.db)
        self.assertEqual(recovered.recover_interrupted(), 1)
        item = recovered.get_job(job["id"])
        self.assertEqual(item["status"], "unknown")
        self.assertEqual(item["error_code"], "recovered_after_restart")

    def test_suppression_blocks_new_job_and_masks_list_output(self) -> None:
        suppression = self.store.add_suppression(
            "email", " Buyer@Example.Test ", "requested_stop"
        )
        self.assertEqual(suppression["normalized_recipient"], "buyer@example.test")
        self.assertNotIn("buyer@example.test", suppression["masked_recipient"])
        with self.assertRaises(SuppressedContact):
            self.create_email()

    def test_editing_opened_job_returns_it_to_review(self) -> None:
        job = self.create_whatsapp()
        self.assertEqual(self.store.mark_opened(job["id"])["status"], "opened")
        updated = self.store.update_content(job["id"], "", "Revised")
        self.assertEqual(updated["status"], "pending_review")
        self.assertEqual(updated["body"], "Revised")

    def test_email_claim_is_atomic_and_message_id_survives_manual_retry(self) -> None:
        job = self.create_email()
        claimed = self.store.claim_email_send(job["id"])
        with self.assertRaises(OutreachConflict):
            self.store.claim_email_send(job["id"])

        failed = self.store.complete_email_send(
            job["id"], "failed", "network", "网络连接失败"
        )
        retried = self.store.claim_email_send(job["id"])
        self.assertEqual(retried["message_id"], claimed["message_id"])
        self.assertEqual(failed["status"], "failed")

    def test_channel_and_status_filters_return_matching_jobs(self) -> None:
        email = self.create_email()
        whatsapp = self.create_whatsapp()
        self.store.transition(whatsapp["id"], "skipped")

        pending = self.store.list_jobs(statuses={"pending_review"})
        self.assertEqual([item["id"] for item in pending], [email["id"]])
        whatsapp_items = self.store.list_jobs(channel="whatsapp")
        self.assertEqual([item["status"] for item in whatsapp_items], ["skipped"])

    def test_skipped_job_can_return_to_pending_review(self) -> None:
        job = self.create_email()
        skipped = self.store.transition(job["id"], "skipped")
        self.assertEqual(skipped["status"], "skipped")

        restored = self.store.transition(job["id"], "pending_review")

        self.assertEqual(restored["status"], "pending_review")
        self.assertEqual(restored["subject"], job["subject"])
        self.assertEqual(restored["body"], job["body"])

    def test_suppression_transitions_matching_active_jobs_and_can_be_removed(self) -> None:
        job = self.create_whatsapp()
        suppression = self.store.add_suppression(
            "whatsapp", "+852 6123-4567", "requested_stop"
        )
        self.assertEqual(self.store.get_job(job["id"])["status"], "suppressed")
        self.assertEqual(len(self.store.list_suppressions()), 1)
        self.store.delete_suppression(suppression["id"])
        self.assertEqual(self.store.list_suppressions(), [])

    def test_rebinding_email_sender_requires_review_and_resets_message_id(self) -> None:
        job = self.create_email()
        self.store.claim_email_send(job["id"])
        self.store.complete_email_send(job["id"], "failed", "auth", "认证失败")
        rebound = self.store.rebind_email_sender(
            job["id"], "qq_smtp", "new@qq.com"
        )
        self.assertEqual(rebound["status"], "pending_review")
        self.assertEqual(rebound["provider"], "qq_smtp")
        self.assertEqual(rebound["sender_account_snapshot"], "new@qq.com")
        self.assertEqual(rebound["message_id"], "")

    def test_same_workflow_returns_one_job_but_changed_payload_conflicts(self) -> None:
        args = {
            "workflow_id": "wf-001",
            "customer_id": "c-003",
            "customer_name": "Demo Center",
            "channel": "email",
            "provider": "gmail_smtp",
            "sender_account": "sender@example.test",
            "recipient": "BUYER@example.test",
            "subject": "Demo",
            "body": "Same body",
        }
        first = self.store.create_job(**args)
        second = self.store.create_job(**args)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["workflow_id"], "wf-001")
        self.assertEqual(self.store.get_job_by_workflow_id("wf-001")["id"], first["id"])
        self.assertIsNone(self.store.get_job_by_workflow_id(""))

        with self.assertRaises(OutreachConflict):
            self.store.create_job(**{**args, "body": "Changed body"})

    def test_old_outreach_table_is_migrated_without_losing_rows(self) -> None:
        legacy_db = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(legacy_db)
        conn.executescript(
            """
            CREATE TABLE outreach_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id TEXT NOT NULL,
                customer_name_snapshot TEXT NOT NULL,
                channel TEXT NOT NULL,
                provider TEXT NOT NULL,
                sender_account_snapshot TEXT DEFAULT '',
                recipient TEXT NOT NULL,
                subject TEXT DEFAULT '',
                body TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                message_id TEXT DEFAULT '',
                status TEXT NOT NULL,
                provider_reference TEXT DEFAULT '',
                error_code TEXT DEFAULT '',
                error_summary TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                opened_at REAL,
                submitted_at REAL,
                replied_at REAL
            );
            INSERT INTO outreach_jobs (
                customer_id, customer_name_snapshot, channel, provider, recipient,
                body, content_hash, status, created_at, updated_at
            ) VALUES ('old-1', 'Old Clinic', 'email', 'gmail_smtp',
                      'old@example.test', 'Old body', 'hash', 'accepted', 1, 1);
            """
        )
        conn.commit()
        conn.close()

        migrated = OutreachStore(legacy_db)
        row = migrated.get_job(1)
        conn = sqlite3.connect(legacy_db)
        columns = {item[1] for item in conn.execute("PRAGMA table_info(outreach_jobs)")}
        count = conn.execute("SELECT COUNT(*) FROM outreach_jobs").fetchone()[0]
        conn.close()

        self.assertIn("workflow_id", columns)
        self.assertEqual(row["customer_name_snapshot"], "Old Clinic")
        self.assertEqual(row["workflow_id"], "")
        self.assertEqual(count, 1)
