"""The whole rule-based extraction for one document: profile fields plus entries.

``run_rules`` takes the output of :func:`documents.normalize_text` and returns
every proposal, all grounded. Fields derived from entries (``current_employer``,
``target_titles``) are added only from *resolved* experience entries, so an
ambiguous entry can never feed a profile field.
"""
from dataclasses import dataclass, field

from apps.accounts.importing import entries as entry_rules
from apps.accounts.importing import rules
from apps.accounts.importing.grounding import is_grounded


@dataclass
class Extraction:
    fields: list = field(default_factory=list)  # [grounding.Proposal]
    entries: list = field(default_factory=list)  # [entries.EntryProposal]


def run_rules(normalized, today=None):
    extracted = rules.extract_fields(normalized)
    found_entries = entry_rules.extract_entries(normalized, today)
    taken = {proposal.field for proposal in extracted}
    for proposal in entry_rules.derived_fields(normalized.text, found_entries):
        if proposal.field not in taken and is_grounded(normalized.text, proposal):
            extracted.append(proposal)
            taken.add(proposal.field)
    return Extraction(fields=extracted, entries=found_entries)
