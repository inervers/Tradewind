from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import server


class LocalOriginPolicyTests(unittest.TestCase):
    def test_epoch_gate_errors_retain_cors_for_trusted_ui(self) -> None:
        from backup_helpers import make_business_fixture, open_fixture_server
        with tempfile.TemporaryDirectory(prefix="task6-cors-") as directory:
            layout = make_business_fixture(Path(directory))
            with open_fixture_server(layout) as client:
                response = client.post("/api/products", json={}, headers={"Origin": "http://localhost:5173", "X-Tradewind-Data-Epoch": "stale"})
                self.assertEqual((response.status_code, response.json()["code"]), (409, "stale_epoch"))
                self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:5173")
                preflight = client.options("/api/products", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "X-Tradewind-Data-Epoch"})
                self.assertEqual(preflight.status_code, 200)

    def test_official_and_vite_origins_are_allowed(self) -> None:
        for origin in (
            "http://127.0.0.1:8101",
            "http://localhost:8101",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ):
            self.assertTrue(server.is_trusted_mutation_origin("POST", origin))

    def test_untrusted_browser_origin_is_rejected(self) -> None:
        self.assertFalse(
            server.is_trusted_mutation_origin("POST", "https://attacker.example")
        )

    def test_read_requests_and_local_scripts_remain_available(self) -> None:
        self.assertTrue(
            server.is_trusted_mutation_origin("GET", "https://attacker.example")
        )
        self.assertTrue(server.is_trusted_mutation_origin("POST", None))

    def test_middleware_rejects_untrusted_mutation_before_endpoint(self) -> None:
        response = TestClient(server.app).post(
            "/api/config/company",
            headers={"Origin": "https://attacker.example"},
            json={"company_name": "must-not-be-written"},
        )
        self.assertEqual(response.status_code, 403)
