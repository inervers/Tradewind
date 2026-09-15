from __future__ import annotations

import smtplib
import unittest

from app.outreach_smtp import SmtpSender


class FakeSMTP:
    def __init__(self, refused=None, login_error=None, send_error=None) -> None:
        self.refused = refused or {}
        self.login_error = login_error
        self.send_error = send_error
        self.starttls_called = False
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, context=None):
        self.starttls_called = context is not None
        return 220, b"ready"

    def login(self, _address, _secret):
        if self.login_error:
            raise self.login_error

    def send_message(self, message):
        self.message = message
        if self.send_error:
            raise self.send_error
        return self.refused


class SmtpSenderTests(unittest.TestCase):
    def job(self) -> dict:
        return {
            "recipient": "buyer@example.test",
            "subject": "Demo",
            "body": "Body",
            "message_id": "<tradewind-1@example.test>",
        }

    def test_success_is_accepted_not_delivered_and_builds_plain_message(self) -> None:
        fake = FakeSMTP()
        result = SmtpSender(factory=lambda *_args, **_kwargs: fake).send(
            self.job(),
            {
                "provider": "gmail_smtp",
                "address": "sender@example.test",
                "transport": "ssl",
            },
            "app-secret",
        )
        self.assertEqual((result.status, result.code), ("accepted", "server_accepted"))
        self.assertNotIn("delivered", result.summary.casefold())
        self.assertEqual(fake.message["From"], "sender@example.test")
        self.assertEqual(fake.message["To"], "buyer@example.test")
        self.assertEqual(fake.message["Message-ID"], "<tradewind-1@example.test>")
        self.assertEqual(fake.message.get_content_type(), "text/plain")

    def test_authentication_failure_is_definite_failure_without_raw_text(self) -> None:
        error = smtplib.SMTPAuthenticationError(535, b"private server detail")
        sender = SmtpSender(
            factory=lambda *_args, **_kwargs: FakeSMTP(login_error=error)
        )
        result = sender.send(
            self.job(),
            {"provider": "qq_smtp", "address": "a@qq.com", "transport": "ssl"},
            "secret",
        )
        self.assertEqual((result.status, result.code), ("failed", "auth"))
        self.assertNotIn("private server detail", result.summary)

    def test_disconnect_during_submission_is_unknown(self) -> None:
        error = smtplib.SMTPServerDisconnected("lost after DATA")
        sender = SmtpSender(
            factory=lambda *_args, **_kwargs: FakeSMTP(send_error=error)
        )
        result = sender.send(
            self.job(),
            {
                "provider": "gmail_smtp",
                "address": "sender@example.test",
                "transport": "ssl",
            },
            "secret",
        )
        self.assertEqual(
            (result.status, result.code), ("unknown", "submission_uncertain")
        )

    def test_starttls_transport_upgrades_connection(self) -> None:
        fake = FakeSMTP()
        SmtpSender(factory=lambda *_args, **_kwargs: fake).send(
            self.job(),
            {
                "provider": "gmail_smtp",
                "address": "sender@example.test",
                "transport": "starttls",
            },
            "secret",
        )
        self.assertTrue(fake.starttls_called)

    def test_recipient_rejection_is_failed(self) -> None:
        refused = {"buyer@example.test": (550, b"rejected")}
        result = SmtpSender(
            factory=lambda *_args, **_kwargs: FakeSMTP(refused=refused)
        ).send(
            self.job(),
            {"provider": "qq_smtp", "address": "a@qq.com", "transport": "ssl"},
            "secret",
        )
        self.assertEqual(
            (result.status, result.code), ("failed", "recipient_rejected")
        )

    def test_definite_data_rejection_is_failed(self) -> None:
        error = smtplib.SMTPDataError(550, b"private rejection")
        result = SmtpSender(
            factory=lambda *_args, **_kwargs: FakeSMTP(send_error=error)
        ).send(
            self.job(),
            {
                "provider": "gmail_smtp",
                "address": "sender@example.test",
                "transport": "ssl",
            },
            "secret",
        )
        self.assertEqual((result.status, result.code), ("failed", "message_rejected"))
        self.assertNotIn("private rejection", result.summary)

    def test_connection_failure_before_submission_is_failed_network(self) -> None:
        def broken_factory(*_args, **_kwargs):
            raise OSError("offline")

        result = SmtpSender(factory=broken_factory).send(
            self.job(),
            {
                "provider": "gmail_smtp",
                "address": "sender@example.test",
                "transport": "ssl",
            },
            "secret",
        )
        self.assertEqual((result.status, result.code), ("failed", "network"))

    def test_connection_test_authenticates_without_sending_a_message(self) -> None:
        fake = FakeSMTP()
        result = SmtpSender(factory=lambda *_args, **_kwargs: fake).test_connection(
            {"provider": "qq_smtp", "address": "a@qq.com", "transport": "ssl"},
            "secret",
        )
        self.assertEqual((result.status, result.code), ("accepted", "connection_ok"))
        self.assertIsNone(fake.message)
