from __future__ import annotations

import unittest

from app.outreach_channels import (
    build_whatsapp_link,
    ensure_cold_email_footer,
    normalize_whatsapp_number,
    split_generated_email,
    validate_email_draft,
)


class OutreachChannelTests(unittest.TestCase):
    def test_hong_kong_number_and_unicode_message_are_encoded(self) -> None:
        self.assertEqual(normalize_whatsapp_number("6123 4567", "香港"), "85261234567")
        link = build_whatsapp_link("+852 6123 4567", "香港", "你好 & hello")
        self.assertEqual(
            link,
            "https://wa.me/85261234567?text=%E4%BD%A0%E5%A5%BD%20%26%20hello",
        )

    def test_ambiguous_local_number_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_whatsapp_number("61234567", "")

    def test_subject_is_split_from_generated_email(self) -> None:
        subject, body = split_generated_email("Subject: Demo offer\n\nHello clinic")
        self.assertEqual(subject, "Demo offer")
        self.assertEqual(body, "Hello clinic")

    def test_chinese_subject_and_missing_subject_are_supported(self) -> None:
        self.assertEqual(
            split_generated_email("主题：合作建议\n您好"),
            ("合作建议", "您好"),
        )
        self.assertEqual(split_generated_email("Hello clinic"), ("", "Hello clinic"))

    def test_footer_adds_identity_and_stop_instruction_once(self) -> None:
        body = ensure_cold_email_footer("Hello", "Demo User", "DemoMed", "en")
        self.assertIn("Demo User", body)
        self.assertIn("DemoMed", body)
        self.assertIn("not to receive further messages", body)
        self.assertEqual(
            ensure_cold_email_footer(body, "Demo User", "DemoMed", "en"), body
        )

    def test_missing_identity_or_stop_instruction_blocks_send(self) -> None:
        issues = validate_email_draft("Offer", "Hello", "Demo User", "DemoMed")
        self.assertIn("missing_sender_identity", issues)
        self.assertIn("missing_opt_out", issues)

    def test_complete_footer_passes_validation(self) -> None:
        body = ensure_cold_email_footer("Hello", "Demo User", "DemoMed", "en")
        self.assertEqual(
            validate_email_draft("Offer", body, "Demo User", "DemoMed"), []
        )
