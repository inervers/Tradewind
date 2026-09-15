from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

import server


class LocalOriginPolicyTests(unittest.TestCase):
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
