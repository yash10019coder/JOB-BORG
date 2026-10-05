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


# Fields where a grounded LLM result is preferred over the rules (rule L6). Rules
# keep phone, links and location: they are exact patterns the model gains nothing on.
LLM_PREFERRED = frozenset({"full_name", "headline", "current_employer"})
_FROM_ENTRIES = frozenset({"current_employer", "target_titles"})


def merge(rule_extraction, llm_extraction, text):
    """Combine the rule result with a grounded LLM result (rule L6).

    The LLM's entries replace the rules' entries when it found any (rules are weak
    at entries); fields derived from entries are then recomputed from them. For
    plain fields the LLM wins only for :data:`LLM_PREFERRED`; otherwise the rule
    proposal stands and the LLM only fills a gap.
    """
    fields = {proposal.field: proposal for proposal in rule_extraction.fields}
    entries = rule_extraction.entries
    if llm_extraction.entries:
        entries = llm_extraction.entries
        for name in _FROM_ENTRIES:
            fields.pop(name, None)
        for proposal in entry_rules.derived_fields(text, entries):
            fields.setdefault(proposal.field, proposal)
    for proposal in llm_extraction.fields:
        if proposal.field in LLM_PREFERRED or proposal.field not in fields:
            fields[proposal.field] = proposal
    return Extraction(fields=list(fields.values()), entries=list(entries))
