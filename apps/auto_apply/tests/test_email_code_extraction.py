"""Tests for email verification extraction logic."""
from datetime import datetime, timezone
from django.test import SimpleTestCase

from apps.auto_apply.email_verification.extraction import (
    evaluate_email_candidate,
    extract_code_from_text,
    is_sender_allowed,
    strip_html_tags,
)


class SenderAllowlistTests(SimpleTestCase):
    def test_domain_matching(self):
        self.assertTrue(is_sender_allowed("no-reply@greenhouse.io", ["greenhouse.io"]))
        self.assertTrue(
            is_sender_allowed("Greenhouse <no-reply@us.greenhouse.io>", ["greenhouse.io"])
        )
        self.assertFalse(is_sender_allowed("no-reply@evil-greenhouse.io", ["greenhouse.io"]))
        self.assertFalse(is_sender_allowed("attacker@gmail.com", ["greenhouse.io"]))

    def test_full_email_matching(self):
        self.assertTrue(
            is_sender_allowed("no-reply@greenhouse.io", ["no-reply@greenhouse.io"])
        )
        self.assertFalse(
            is_sender_allowed("other@greenhouse.io", ["no-reply@greenhouse.io"])
        )


class HtmlStrippingTests(SimpleTestCase):
    def test_strip_tags(self):
        html = "<div><p>Your verification code is <b>654321</b>.</p></div>"
        text = strip_html_tags(html)
        self.assertIn("654321", text)
        self.assertNotIn("<p>", text)


class ExtractionLogicTests(SimpleTestCase):
    def test_realistic_text_extraction(self):
        subject = "Your Greenhouse verification code"
        body = "Thank you for applying. Your verification code is 123456."
        code = extract_code_from_text(subject, body)
        self.assertEqual(code, "123456")

    def test_no_verification_phrasing_returns_none(self):
        subject = "Order Confirmation #987654"
        body = "Your order total is $50.00. Tracking number 123456."
        code = extract_code_from_text(subject, body)
        self.assertIsNone(code)

    def test_contextual_regex_prioritized_over_unrelated_number(self):
        subject = "Greenhouse Security Code"
        body = "Account ID 999888. Your verification code is 654321."
        code = extract_code_from_text(subject, body)
        self.assertEqual(code, "654321")


class EvaluateEmailCandidateTests(SimpleTestCase):
    def test_disallowed_sender_ignored(self):
        raw_msg = (
            b"From: attacker@bad.com\r\n"
            b"Subject: Greenhouse verification code\r\n"
            b"Date: Wed, 05 Aug 2026 12:00:00 +0000\r\n"
            b"\r\n"
            b"Your verification code is 123456."
        )
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        code = evaluate_email_candidate(raw_msg, since, ["greenhouse.io"])
        self.assertIsNone(code)

    def test_pre_since_message_ignored(self):
        raw_msg = (
            b"From: no-reply@greenhouse.io\r\n"
            b"Subject: Greenhouse verification code\r\n"
            b"Date: Wed, 05 Aug 2026 10:00:00 +0000\r\n"
            b"\r\n"
            b"Your verification code is 123456."
        )
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        code = evaluate_email_candidate(raw_msg, since, ["greenhouse.io"])
        self.assertIsNone(code)

    def test_valid_candidate_extracted(self):
        raw_msg = (
            b"From: no-reply@greenhouse.io\r\n"
            b"Subject: Greenhouse verification code\r\n"
            b"Date: Wed, 05 Aug 2026 12:00:00 +0000\r\n"
            b"\r\n"
            b"Your verification code is 789012."
        )
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        code = evaluate_email_candidate(raw_msg, since, ["greenhouse.io"])
        self.assertEqual(code, "789012")
