"""Tests for submission time budget, system check, and reason code mapping totality."""
from django.test import SimpleTestCase, override_settings

from apps.auto_apply.checks import check_auto_apply_sending_timeout_ordering
from apps.auto_apply.email_verification.base import VerificationOutcome
from apps.auto_apply.greenhouse_form.exceptions import GreenhouseFormVerificationFailed
from apps.auto_apply.models import AutoApplyDraft
from apps.auto_apply.tasks import _reason_code_for, _submit_budget_seconds


class SubmitTimeBudgetTests(SimpleTestCase):
    @override_settings(AUTO_APPLY_SENDING_TIMEOUT_SECONDS=600)
    def test_budget_computation_honors_settings(self):
        self.assertEqual(_submit_budget_seconds(), 540.0)

    @override_settings(AUTO_APPLY_SENDING_TIMEOUT_SECONDS=300)
    def test_budget_computation_updates_on_override(self):
        self.assertEqual(_submit_budget_seconds(), 240.0)


class SystemCheckOrderingTests(SimpleTestCase):
    @override_settings(AUTO_APPLY_SENDING_TIMEOUT_SECONDS=600)
    def test_valid_ordering_produces_no_errors(self):
        errors = check_auto_apply_sending_timeout_ordering(None)
        self.assertEqual(len(errors), 0)

    @override_settings(AUTO_APPLY_SENDING_TIMEOUT_SECONDS=950)
    def test_inverted_ordering_produces_system_check_error(self):
        errors = check_auto_apply_sending_timeout_ordering(None)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, "auto_apply.E001")


class ReasonCodeTotalityTests(SimpleTestCase):
    def test_every_verification_outcome_maps_to_a_reason_code(self):
        expected_mappings = {
            VerificationOutcome.NO_INBOX_CREDENTIALS: AutoApplyDraft.ReasonCode.NO_INBOX_CREDENTIALS,
            VerificationOutcome.CODE_TIMEOUT: AutoApplyDraft.ReasonCode.VERIFICATION_CODE_TIMEOUT,
            VerificationOutcome.INBOX_AUTH_FAILED: AutoApplyDraft.ReasonCode.INBOX_AUTH_FAILED,
            VerificationOutcome.INBOX_UNAVAILABLE: AutoApplyDraft.ReasonCode.INBOX_UNAVAILABLE,
            VerificationOutcome.CODE_AMBIGUOUS: AutoApplyDraft.ReasonCode.VERIFICATION_CODE_AMBIGUOUS,
            VerificationOutcome.CODE_REJECTED: AutoApplyDraft.ReasonCode.VERIFICATION_CODE_REJECTED,
        }

        for outcome, expected_reason_code in expected_mappings.items():
            exc = GreenhouseFormVerificationFailed("error", outcome=outcome)
            reason_code = _reason_code_for(exc)
            self.assertEqual(reason_code, expected_reason_code)
