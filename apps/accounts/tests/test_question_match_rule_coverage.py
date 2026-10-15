"""Keeps every question-matching rule tested.

``docs/plans/2026-10-06-002-question-matching-rules.md`` is the contract for the
reworded-question matcher. :class:`QuestionMatchRuleCoverageTests` fails when a rule ID
in that document is not cited (in a test name, docstring or comment) by a matching test.
"""
import re
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.test import SimpleTestCase

RULES_DOC = Path(settings.BASE_DIR) / "docs/plans/2026-10-06-002-question-matching-rules.md"
TESTS = Path(settings.BASE_DIR) / "apps"


@skipUnless(RULES_DOC.exists(), "the rules document is not shipped in this environment")
class QuestionMatchRuleCoverageTests(SimpleTestCase):
    def cited_text(self):
        files = [
            TESTS / "accounts/tests/test_question_matching.py",
            TESTS / "accounts/tests/test_question_candidates.py",
            TESTS / "accounts/tests/test_candidate_metrics.py",
            TESTS / "accounts/tests/test_question_match_eval.py",
            TESTS / "web/tests/test_candidate_views.py",
        ]
        return "\n".join(path.read_text() for path in files)

    def test_every_rule_in_the_contract_is_cited_by_a_test(self):
        ids = re.findall(r"^- \*\*([A-Z]+\d+)\*\*", RULES_DOC.read_text(), re.M)
        self.assertGreaterEqual(len(ids), 25)  # the document was actually parsed
        cited = self.cited_text()
        uncited = [i for i in ids if not re.search(rf"(?<![A-Za-z0-9]){i}(?![0-9])", cited)]
        self.assertEqual(uncited, [], "rules with no test naming them: " + ", ".join(uncited))

    def test_a_rule_without_a_citing_test_is_detected(self):
        cited = self.cited_text()
        self.assertIsNone(re.search(r"(?<![A-Za-z0-9])QM99(?![0-9])", cited))
