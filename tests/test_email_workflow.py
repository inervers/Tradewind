from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from app import memory
from app.email_workflow import EmailWorkflow, WorkflowContext


class FakeSteps:
    def __init__(self, retry_once: bool = True) -> None:
        self.retry_once = retry_once
        self.calls: list[str] = []
        self.persist_count = 0

    def retrieve(self, state: dict) -> dict:
        self.calls.append("retrieve")
        return {
            **state,
            "products": [{"title": "Laser", "content": "Diode-808"}],
            "emails": [],
            "templates_used": [],
            "company_profile": {},
        }

    def generate(self, state: dict, context: WorkflowContext) -> tuple[str, int]:
        attempt = int(state["attempt"])
        self.calls.append(f"generate:{attempt}")
        return f"Draft {attempt}", attempt

    def check(self, state: dict) -> list[str]:
        attempt = int(state["attempt"])
        self.calls.append(f"check:{attempt}")
        if self.retry_once and attempt == 1:
            return ["缺少明确行动号召"]
        return []

    def judge(self, state: dict, context: WorkflowContext) -> tuple[dict | None, str]:
        self.calls.append("judge")
        return None, "disabled"

    def persist(self, state: dict) -> int:
        self.persist_count += 1
        return 41


class HistorySteps(FakeSteps):
    def persist(self, state: dict) -> int:
        self.persist_count += 1
        result = state["result"]
        return memory.save_email_once(
            state["workflow_id"],
            result["customer"],
            result["country"],
            result["product"],
            result["email"],
            0,
            result["language"],
            result["format"],
        )


def inputs(customer_id: str = "") -> dict:
    return {
        "customer_id": customer_id,
        "customer": "Demo Clinic",
        "country": "HK",
        "product": "Laser",
        "extra": "",
        "judge": False,
        "language": "en",
        "format": "email",
        "template_record": None,
        "company_profile": {},
    }


class EmailWorkflowTests(unittest.TestCase):
    def test_rule_failure_retries_once_then_persists(self) -> None:
        steps = FakeSteps(retry_once=True)
        workflow = EmailWorkflow(InMemorySaver(), steps)

        projection = workflow.start(
            "wf-retry", inputs(), WorkflowContext(now=lambda: 10.0)
        )
        snapshot = workflow.graph.get_state(
            {"configurable": {"thread_id": "wf-retry"}}
        )

        self.assertEqual(
            steps.calls,
            ["retrieve", "generate:1", "check:1", "generate:2", "check:2", "judge"],
        )
        self.assertEqual(snapshot.values["attempt"], 2)
        self.assertEqual(steps.persist_count, 1)
        self.assertEqual(projection["status"], "done")

    def test_interrupt_survives_restart_and_approve_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "workflow.db"
            steps = FakeSteps(retry_once=False)
            conn = sqlite3.connect(db, check_same_thread=False)
            saver = SqliteSaver(conn)
            saver.setup()
            first = EmailWorkflow(saver, steps, connection=conn)
            projection = first.start("wf-restart", inputs("c-001"), WorkflowContext())

            self.assertEqual(projection["stream"], "Draft 1")
            self.assertEqual(steps.persist_count, 0)
            first.close()

            reopened_conn = sqlite3.connect(db, check_same_thread=False)
            reopened_saver = SqliteSaver(reopened_conn)
            reopened_saver.setup()
            reopened = EmailWorkflow(reopened_saver, steps, connection=reopened_conn)
            self.assertEqual(reopened.get_task_projection("wf-restart")["stream"], "Draft 1")

            approved = reopened.resume_approval("wf-restart", "approve", 7)
            self.assertEqual(approved["result"]["workflow_id"], "wf-restart")
            self.assertEqual(steps.persist_count, 1)
            reopened.close()

    def test_duplicate_approval_writes_one_real_history_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history_db = Path(tmp) / "history.db"
            steps = HistorySteps(retry_once=False)
            with patch.object(memory, "MEMORY_DB", history_db):
                workflow = EmailWorkflow(InMemorySaver(), steps)
                workflow.start("wf-once", inputs("c-001"), WorkflowContext())
                workflow.resume_approval("wf-once", "approve", 8)
                workflow.resume_approval("wf-once", "approve", 8)

            conn = sqlite3.connect(history_db)
            count = conn.execute(
                "SELECT COUNT(*) FROM emails WHERE workflow_id='wf-once'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(count, 1)
            self.assertEqual(steps.persist_count, 1)

    def test_rejection_ends_without_persisting(self) -> None:
        steps = FakeSteps(retry_once=False)
        workflow = EmailWorkflow(InMemorySaver(), steps)
        workflow.start("wf-reject", inputs("c-001"), WorkflowContext())

        projection = workflow.resume_approval("wf-reject", "reject")

        self.assertEqual(projection["result"]["workflow_id"], "wf-reject")
        self.assertEqual(steps.persist_count, 0)
        snapshot = workflow.graph.get_state(
            {"configurable": {"thread_id": "wf-reject"}}
        )
        self.assertEqual(snapshot.next, ())
        self.assertEqual(snapshot.values["approval_status"], "rejected")


if __name__ == "__main__":
    unittest.main()
