from django.test import SimpleTestCase

from apps.accounts.tiering import TIERING_VERSION
from apps.auto_apply.services.confirmation import build_submit_snapshot, unconfirmed_fields


def _entry(**overrides):
    entry = {"value": "Yes", "needs_review": True, "needs_confirmation": True}
    entry.update(overrides)
    return entry


class UnconfirmedFieldsTests(SimpleTestCase):
    def test_only_flagged_entries_with_a_value_block(self):
        answers = {
            "Authorized?": _entry(),
            "Confirmed": _entry(needs_confirmation=False),
            "Plain": {"value": "x", "needs_review": False},
            "Blank optional": _entry(value=""),
            "Blank list": _entry(value=[" "]),
            "Multi": _entry(value=["a", "b"]),
        }
        self.assertEqual(unconfirmed_fields(answers), ["Authorized?", "Multi"])

    def test_tolerates_empty_and_malformed_answers(self):
        self.assertEqual(unconfirmed_fields(None), [])
        self.assertEqual(unconfirmed_fields({}), [])
        self.assertEqual(unconfirmed_fields({"bad": "not a dict"}), [])


class BuildSubmitSnapshotTests(SimpleTestCase):
    def test_records_value_tier_and_provenance_per_answer(self):
        provenance = {"origin": "answer_bank", "source": "learned"}
        snapshot = build_submit_snapshot(
            {
                "Authorized?": _entry(
                    needs_confirmation=False, user_confirmed=True, tier="t0_legal",
                    provenance={"origin": "draft_review", "source": "user"},
                    confirmed_from=provenance, category="work_authorization",
                    reason="answer_bank",
                ),
            }
        )
        self.assertEqual(snapshot["tiering_version"], TIERING_VERSION)
        recorded = snapshot["answers"]["Authorized?"]
        self.assertEqual(recorded["value"], "Yes")
        self.assertEqual(recorded["tier"], "t0_legal")
        self.assertTrue(recorded["user_confirmed"])
        self.assertEqual(recorded["confirmed_from"], provenance)
        self.assertEqual(recorded["provenance"]["source"], "user")

    def test_file_values_are_not_recorded(self):
        snapshot = build_submit_snapshot(
            {"Resume": {"value": "/media/resumes/1/secret.pdf", "field_type": "file"}}
        )
        self.assertEqual(snapshot["answers"]["Resume"]["value"], "[file]")

    def test_skips_malformed_entries(self):
        self.assertEqual(build_submit_snapshot({"bad": 1})["answers"], {})
