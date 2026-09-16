from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class FakeWorkflow:
    def __init__(self) -> None:
        self.starts: list[tuple[str, dict]] = []
        self.projections: dict[str, dict] = {}

    def start(self, workflow_id: str, inputs: dict, context) -> dict:
        self.starts.append((workflow_id, inputs))
        projection = {
            "status": "done",
            "total": 1,
            "done": 1,
            "current": inputs["customer"],
            "results": [],
            "result": {
                "workflow_id": workflow_id,
                "customer_id": inputs.get("customer_id", ""),
                "customer": inputs["customer"],
                "email": "Draft",
            },
            "error": "",
            "stream": "Draft",
        }
        self.projections[workflow_id] = projection
        return projection

    def get_task_projection(self, workflow_id: str) -> dict:
        return self.projections.get(workflow_id, {"status": "not_found"})


class ServerEmailWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.customers = Path(self.tmp.name) / "customers.json"
        self.customers.write_text(
            json.dumps([{"id": "c-001", "name": "Demo Clinic"}]),
            encoding="utf-8",
        )
        server.TASKS.clear()

    def tearDown(self) -> None:
        server.TASKS.clear()
        self.tmp.cleanup()

    def test_start_uses_task_id_as_workflow_id_and_survives_memory_clear(self) -> None:
        workflow = FakeWorkflow()
        legacy_result = {
            "customer": "Demo Clinic",
            "country": "HK",
            "product": "Laser",
            "email": "Legacy",
            "issues": [],
            "scores": None,
            "judge_status": "disabled",
            "tokens": 0,
            "time_s": 0,
            "language": "en",
            "format": "email",
            "templates_used": [],
        }
        with (
            patch.object(server, "CUSTOMERS_FILE", self.customers),
            patch.object(server, "_EMAIL_WORKFLOW", workflow, create=True),
            patch.object(server, "generate_email", return_value=legacy_result),
        ):
            client = TestClient(server.app)
            response = client.post(
                "/api/email/start",
                json={
                    "customer_id": "c-001",
                    "customer": "Demo Clinic",
                    "country": "HK",
                    "product": "Laser",
                    "language": "en",
                    "format": "email",
                },
            )
            self.assertEqual(response.status_code, 200)
            task_id = response.json()["task_id"]
            self.assertRegex(task_id, r"^[0-9a-f]{12}$")
            deadline = time.monotonic() + 2
            while not workflow.starts and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(workflow.starts[0][0], task_id)
            self.assertEqual(workflow.starts[0][1]["customer_id"], "c-001")

            server.TASKS.clear()
            restored = client.get(f"/api/email/tasks/{task_id}").json()

        self.assertEqual(restored["status"], "done")
        self.assertEqual(restored["stream"], "Draft")
        self.assertEqual(restored["result"]["workflow_id"], task_id)
        self.assertEqual(restored["result"]["customer_id"], "c-001")


if __name__ == "__main__":
    unittest.main()
