from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.credential_store import CredentialStore, CredentialUnavailable


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, value: str) -> None:
        self.values[(service, username)] = value

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


class BrokenKeyring:
    def set_password(self, *_args) -> None:
        raise RuntimeError("backend detail must stay private")

    def get_password(self, *_args) -> str | None:
        raise RuntimeError("backend detail must stay private")

    def delete_password(self, *_args) -> None:
        raise RuntimeError("backend detail must stay private")


class OutreachCredentialTests(unittest.TestCase):
    def test_secret_is_kept_out_of_json_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with patch.object(config, "CONFIG_FILE", path):
                config.set_email_outreach_config(
                    "gmail_smtp", "sender@example.test", "ssl"
                )
                loaded = config.get_email_outreach_config()
            saved = path.read_text(encoding="utf-8")
            self.assertEqual(loaded["provider"], "gmail_smtp")
            self.assertIn("sender@example.test", saved)
            self.assertNotIn("password", saved.casefold())
            self.assertNotIn("secret", saved.casefold())

    def test_credential_round_trip_uses_normalized_keyring_identity(self) -> None:
        backend = FakeKeyring()
        store = CredentialStore(backend)
        store.set("gmail_smtp", " Sender@Example.Test ", " app-secret ")
        self.assertTrue(store.has("gmail_smtp", "sender@example.test"))
        self.assertEqual(
            store.get("gmail_smtp", "SENDER@EXAMPLE.TEST"), "app-secret"
        )
        self.assertIn(
            ("Tradewind/outreach-email/gmail_smtp", "sender@example.test"),
            backend.values,
        )
        store.delete("gmail_smtp", "sender@example.test")
        self.assertFalse(store.has("gmail_smtp", "sender@example.test"))

    def test_backend_errors_fail_closed_without_raw_detail(self) -> None:
        store = CredentialStore(BrokenKeyring())
        with self.assertRaises(CredentialUnavailable) as raised:
            store.get("qq_smtp", "sender@qq.com")
        self.assertNotIn("backend detail", str(raised.exception))

    def test_invalid_provider_and_transport_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "CONFIG_FILE", Path(tmp) / "config.json"):
                with self.assertRaises(ValueError):
                    config.set_email_outreach_config("custom", "a@example.test", "ssl")
                with self.assertRaises(ValueError):
                    config.set_email_outreach_config("qq_smtp", "a@qq.com", "plain")
