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

    def test_eight_character_alphanumeric_code_extracted(self):
        """Regression test for a real, live Greenhouse interstitial (Alpaca,
        job 6113944004) whose on-page copy reads "enter the 8-character
        code" -- the prior 6-digit-only regex could never match this. The
        exact code shown here is synthetic (no real verification email was
        ever captured for this posting); the length/charset assumption
        (alphanumeric, contains at least one digit) is a judgment call
        pending a real captured email, not a confirmed fact."""
        subject = "Your Greenhouse verification code"
        body = (
            "A verification code was sent to you. To submit your "
            "application, enter the 8-character code to confirm you're a "
            "human. Security code: XJ4K9P2Q"
        )
        code = extract_code_from_text(subject, body)
        self.assertEqual(code, "XJ4K9P2Q")

    def test_bare_verify_earlier_in_text_does_not_win_over_the_real_code(self):
        """Regression test for a code-review finding (mechanically verified):
        re.search takes the leftmost trigger-phrase match. A bare "verify"
        occurring earlier in the text than the real "verification code"
        phrase could previously make an unrelated adjacent alphanumeric
        token (e.g. a reference number) win over the real code."""
        subject = "Your Greenhouse verification code"
        body = (
            "Please verify: your reference number REQ998877 was recorded. "
            "Your verification code is 654321."
        )
        code = extract_code_from_text(subject, body)
        self.assertEqual(code, "654321")

    def test_plain_words_near_phrasing_are_not_mistaken_for_a_code(self):
        """The real Alpaca copy itself contains "confirm" and "human" right
        next to the code -- neither has a digit, so the digit-required
        pattern must not grab them as a false code when the real code is
        genuinely absent from the text."""
        subject = "Your Greenhouse verification code"
        body = (
            "A verification code was sent to you. To submit your "
            "application, enter the code to confirm you're a human."
        )
        code = extract_code_from_text(subject, body)
        self.assertIsNone(code)


class EvaluateEmailCandidateTests(SimpleTestCase):
    def test_matching_is_sender_independent(self):
        # Deliberate design choice (see extraction.py's evaluate_email_
        # candidate docstring): a real verification email can legitimately
        # arrive from any address an employer's own Greenhouse-hosted flow
        # happens to use -- verified against real production failures where
        # a strict "greenhouse.io" sender allowlist caused every single
        # verification lookup to time out because the real sender didn't
        # match. Content (contextual phrasing + a code-shaped token) is what
        # identifies a genuine verification email, not the sender address;
        # `sender_allowlist` is accepted for signature compatibility only.
        raw_msg = (
            b"From: notifications@some-other-domain.example\r\n"
            b"Subject: Greenhouse verification code\r\n"
            b"Date: Wed, 05 Aug 2026 12:00:00 +0000\r\n"
            b"\r\n"
            b"Your verification code is 123456."
        )
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        code = evaluate_email_candidate(raw_msg, since, ["greenhouse.io"], since)
        self.assertEqual(code, "123456")

    def test_matches_even_with_an_empty_sender_allowlist(self):
        # sender_allowlist no longer participates in matching at all.
        raw_msg = (
            b"From: notifications@some-other-domain.example\r\n"
            b"Subject: Greenhouse verification code\r\n"
            b"Date: Wed, 05 Aug 2026 12:00:00 +0000\r\n"
            b"\r\n"
            b"Your verification code is 123456."
        )
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        code = evaluate_email_candidate(raw_msg, since, [], since)
        self.assertEqual(code, "123456")

    def test_missing_contextual_phrasing_still_ignored_regardless_of_sender(self):
        # Sender-independence doesn't mean "match anything" -- an email with
        # no verification-code phrasing at all must still be ignored, from
        # any sender.
        raw_msg = (
            b"From: no-reply@greenhouse.io\r\n"
            b"Subject: Your weekly newsletter\r\n"
            b"Date: Wed, 05 Aug 2026 12:00:00 +0000\r\n"
            b"\r\n"
            b"Here are this week's top jobs: 123456 new postings!"
        )
        since = datetime(2026, 8, 5, 11, 0, 0, tzinfo=timezone.utc)
        code = evaluate_email_candidate(raw_msg, since, ["greenhouse.io"], since)
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
        code = evaluate_email_candidate(raw_msg, since, ["greenhouse.io"], since.replace(hour=10))
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
        code = evaluate_email_candidate(raw_msg, since, ["greenhouse.io"], since)
        self.assertEqual(code, "789012")

    def test_server_date_controls_recency_with_two_minute_skew(self):
        from datetime import timedelta

        since = datetime(2026, 8, 5, 11, tzinfo=timezone.utc)
        for header in (b"", b"Date: invalid\r\n", b"Date: Wed, 05 Aug 2037 12:00:00 +0000\r\n"):
            msg = b"From: no-reply@greenhouse.io\r\n" + header + b"\r\nYour verification code is 789012."
            for seconds, expected in ((-121, None), (-120, "789012"), (0, "789012")):
                with self.subTest(header=header, seconds=seconds):
                    self.assertEqual(evaluate_email_candidate(
                        msg, since, ["greenhouse.io"], since + timedelta(seconds=seconds)
                    ), expected)
            self.assertIsNone(evaluate_email_candidate(msg, since, ["greenhouse.io"]))
            self.assertIsNone(evaluate_email_candidate(msg, since, ["greenhouse.io"], since.replace(tzinfo=None)))
