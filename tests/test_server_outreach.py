from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from app import config as app_config, memory
from app.outreach_smtp import SendOutcome
from app.outreach_store import OutreachStore


PROFILE = {
    "sender_name": "Demo User",
    "company_name": "DemoMed",
    "email": "sender@example.test",
    "whatsapp": "",
    "website": "",
}
EMAIL_CONFIG = {
    "provider": "gmail_smtp",
    "address": "sender@example.test",
    "transport": "ssl",
}
EMAIL_CONTENT = (
    "Subject: Demo\n\nHello\n\nDemo User\nDemoMed\n"
    "If you prefer not to receive further messages, please let me know."
)


class FakeCredentialStore:
    values: dict[tuple[str, str], str] = {}

    def set(self, provider: str, address: str, secret: str) -> None:
        self.values[(provider, address.casefold())] = secret

    def get(self, provider: str, address: str) -> str:
        return self.values[(provider, address.casefold())]

    def has(self, provider: str, address: str) -> bool:
        return (provider, address.casefold()) in self.values

    def delete(self, provider: str, address: str) -> None:
        self.values.pop((provider, address.casefold()), None)


class DormantThread:
    starts = 0

    def __init__(self, *, target, args=(), daemon=False) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self) -> None:
        type(self).starts += 1


class FakeApprovalWorkflow:
    def __init__(self) -> None:
        self.resumes: list[tuple[str, str, int]] = []

    def resume_approval(
        self, workflow_id: str, action: str, outreach_job_id: int = 0
    ) -> dict:
        self.resumes.append((workflow_id, action, outreach_job_id))
        memory.save_email_once(
            workflow_id,
            "Demo Clinic",
            "香港",
            "Laser",
            EMAIL_CONTENT,
            language="en",
        )
        return {"status": "done"}


class ServerOutreachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.customers = root / "customers.json"
        self.db = root / "memory.db"
        self.config = root / "config.json"
        self.customers.write_text(
            json.dumps(
                [
                    {
                        "id": "c-001",
                        "name": "Demo Clinic",
                        "country": "香港",
                        "email": "buyer@example.test",
                        "phone": "+852 6123 4567",
                        "status": "new",
                        "notes": "",
                        "city": "",
                        "website": "",
                    }
                ]
            ),
            encoding="utf-8",
        )
        FakeCredentialStore.values = {}
        DormantThread.starts = 0
        self.patches = [
            patch.object(server, "CUSTOMERS_FILE", self.customers),
            patch.object(server, "MEMORY_DB_FILE", self.db),
            patch.object(app_config, "CONFIG_FILE", self.config),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def create_email(self) -> dict:
        with (
            patch.object(server, "get_email_outreach_config", return_value=EMAIL_CONFIG),
            patch.object(server, "get_company_profile", return_value=PROFILE),
        ):
            result = server.api_outreach_create(
                server.OutreachCreateRequest(
                    customer_id="c-001", channel="email", content=EMAIL_CONTENT
                )
            )
        self.assertTrue(result["ok"])
        return result["item"]

    def test_create_email_job_uses_customer_and_sender_snapshots(self) -> None:
        item = self.create_email()
        self.assertEqual(item["recipient"], "buyer@example.test")
        self.assertEqual(item["sender_account_snapshot"], "sender@example.test")
        self.assertEqual(item["subject"], "Demo")

    def test_workflow_approval_is_resumed_idempotently(self) -> None:
        workflow = FakeApprovalWorkflow()
        request = server.OutreachCreateRequest(
            customer_id="c-001",
            channel="email",
            content=EMAIL_CONTENT,
            workflow_id="wf-001",
        )
        with (
            patch.object(server, "get_email_outreach_config", return_value=EMAIL_CONFIG),
            patch.object(server, "get_company_profile", return_value=PROFILE),
            patch.object(server, "_EMAIL_WORKFLOW", workflow),
            patch.object(memory, "MEMORY_DB", self.db),
        ):
            first = server.api_outreach_create(request)
            second = server.api_outreach_create(request)

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(first["item"]["id"], second["item"]["id"])
        self.assertEqual(
            workflow.resumes,
            [("wf-001", "approve", first["item"]["id"])] * 2,
        )
        conn = sqlite3.connect(self.db)
        outreach_count = conn.execute(
            "SELECT COUNT(*) FROM outreach_jobs WHERE workflow_id='wf-001'"
        ).fetchone()[0]
        history_count = conn.execute(
            "SELECT COUNT(*) FROM emails WHERE workflow_id='wf-001'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(outreach_count, 1)
        self.assertEqual(history_count, 1)

    def test_create_whatsapp_job_normalizes_customer_phone(self) -> None:
        result = server.api_outreach_create(
            server.OutreachCreateRequest(
                customer_id="c-001", channel="whatsapp", content="Hello clinic"
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["item"]["recipient"], "85261234567")

    def test_registered_jobs_route_reads_the_patched_store(self) -> None:
        self.create_email()
        response = TestClient(server.app).get("/api/outreach/jobs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["customer_id"], "c-001")

    def test_accepted_send_updates_customer_summary(self) -> None:
        created = self.create_email()
        OutreachStore(self.db).claim_email_send(created["id"])
        with (
            patch.object(server, "get_email_outreach_config", return_value=EMAIL_CONFIG),
            patch.object(server, "get_company_profile", return_value=PROFILE),
            patch.object(server.CredentialStore, "get", return_value="app-secret"),
            patch.object(
                server.SmtpSender,
                "send",
                return_value=SendOutcome(
                    "accepted", "server_accepted", "服务器已接受", "<id@example.test>"
                ),
            ),
        ):
            server._execute_claimed_outreach_email(created["id"])

        self.assertEqual(
            OutreachStore(self.db).get_job(created["id"])["status"], "accepted"
        )
        customers = json.loads(self.customers.read_text(encoding="utf-8"))
        self.assertEqual(customers[0]["status"], "sent")

    def test_sender_switch_fails_claimed_job_before_smtp(self) -> None:
        created = self.create_email()
        OutreachStore(self.db).claim_email_send(created["id"])
        changed = {**EMAIL_CONFIG, "address": "other@example.test"}
        with (
            patch.object(server, "get_email_outreach_config", return_value=changed),
            patch.object(
                server.SmtpSender,
                "send",
                side_effect=AssertionError("SMTP must not run after sender switch"),
            ),
        ):
            server._execute_claimed_outreach_email(created["id"])
        item = OutreachStore(self.db).get_job(created["id"])
        self.assertEqual((item["status"], item["error_code"]), ("failed", "sender_changed"))

    def test_duplicate_send_request_claims_once_and_starts_one_worker(self) -> None:
        created = self.create_email()
        FakeCredentialStore.values[("gmail_smtp", "sender@example.test")] = "secret"
        with (
            patch.object(server, "get_email_outreach_config", return_value=EMAIL_CONFIG),
            patch.object(server, "get_company_profile", return_value=PROFILE),
            patch.object(server, "CredentialStore", FakeCredentialStore),
            patch.object(server.threading, "Thread", DormantThread),
        ):
            first = server.api_outreach_send_email(created["id"])
            second = server.api_outreach_send_email(created["id"])
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(first["item"]["status"], "sending")
        self.assertEqual(DormantThread.starts, 1)

    def test_suppression_blocks_new_job_and_transitions_existing_job(self) -> None:
        created = self.create_email()
        suppressed = server.api_outreach_suppress(
            server.OutreachSuppressionRequest(
                channel="email", recipient="buyer@example.test", reason="requested_stop"
            )
        )
        self.assertTrue(suppressed["ok"])
        self.assertEqual(
            OutreachStore(self.db).get_job(created["id"])["status"], "suppressed"
        )
        duplicate = server.api_outreach_create(
            server.OutreachCreateRequest(
                customer_id="c-001", channel="email", content=EMAIL_CONTENT
            )
        )
        self.assertFalse(duplicate["ok"])
        self.assertEqual(duplicate["code"], "suppressed")
        self.assertIn("禁止联系", duplicate["error"])

    def test_suppression_list_exposes_only_masked_recipient(self) -> None:
        server.api_outreach_suppress(
            server.OutreachSuppressionRequest(
                channel="email", recipient="buyer@example.test", reason="requested_stop"
            )
        )

        item = server.api_outreach_suppressions()[0]

        self.assertEqual(item["masked_recipient"], "b***@example.test")
        self.assertNotIn("normalized_recipient", item)
        self.assertNotIn("buyer@example.test", json.dumps(item))

    def test_startup_recovery_marks_sending_unknown_without_smtp(self) -> None:
        created = self.create_email()
        OutreachStore(self.db).claim_email_send(created["id"])
        with patch.object(
            server.SmtpSender,
            "send",
            side_effect=AssertionError("startup recovery must not call SMTP"),
        ):
            count = server.recover_outreach_after_startup()
        item = OutreachStore(self.db).get_job(created["id"])
        self.assertEqual(count, 1)
        self.assertEqual((item["status"], item["error_code"]), ("unknown", "recovered_after_restart"))

    def test_email_settings_never_return_the_credential(self) -> None:
        with patch.object(server, "CredentialStore", FakeCredentialStore):
            result = server.api_outreach_email_settings_save(
                server.OutreachEmailSettingsRequest(
                    provider="gmail_smtp",
                    address="sender@gmail.com",
                    transport="ssl",
                    credential="app-secret",
                )
            )
            loaded = server.api_outreach_email_settings()
        self.assertTrue(result["ok"])
        self.assertTrue(loaded["has_credential"])
        self.assertNotIn("credential", loaded)
        self.assertNotIn("app-secret", json.dumps(loaded))

    def test_customer_add_preserves_phone_for_outreach(self) -> None:
        result = server.api_customer_add(
            server.CustomerRequest(name="Second Clinic", country="香港", phone="61234567")
        )
        self.assertEqual(result["item"]["phone"], "61234567")

    def test_public_transition_cannot_forge_internal_sending_state(self) -> None:
        created = self.create_email()
        result = server.api_outreach_transition(
            created["id"], server.OutreachTransitionRequest(status="sending")
        )
        self.assertFalse(result["ok"])
        self.assertEqual(
            OutreachStore(self.db).get_job(created["id"])["status"], "pending_review"
        )

    def test_rebind_sender_uses_current_account_and_returns_to_review(self) -> None:
        created = self.create_email()
        current = {
            "provider": "qq_smtp",
            "address": "new@qq.com",
            "transport": "ssl",
        }
        with patch.object(server, "get_email_outreach_config", return_value=current):
            result = server.api_outreach_rebind_sender(created["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["item"]["provider"], "qq_smtp")
        self.assertEqual(result["item"]["sender_account_snapshot"], "new@qq.com")
        self.assertEqual(result["item"]["status"], "pending_review")
