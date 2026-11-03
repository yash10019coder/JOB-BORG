---
title: Question matching rules (reworded-question candidates)
type: rules
status: active
date: 2026-10-06
origin: docs/plans/2026-10-06-001-feat-reworded-question-candidates-plan.md
---

# Question matching rules

The contract for `apps/accounts/question_matching.py` (the pure matcher) and
`apps/accounts/services/question_candidates.py` (retrieval). Each rule ID is cited
by at least one test; `apps/accounts/tests/test_question_match_rule_coverage.py`
fails if an ID here has no citing test.

A match is only ever a **suggestion** shown next to a blank field. Nothing here
keys, fills or confirms an answer. A confidently wrong candidate is worse than none.

## Matcher

- **QM1** Comparison text is lowercased, with `(optional)`/`(required)`, a leading "please", punctuation and contractions normalised, and is cut to a fixed length. Never raises on odd input.
- **QM2** Similarity is Jaccard over word-padded character trigrams.
- **QM3** `typed_owned`: a question the typed layer owns (`blocks_llm`: citizenship, "are you located in X") never gets a candidate.
- **QM4** `polarity`: a differing set of negation words vetoes the pair, whatever the score.
- **QM5** `auth_kind`: authorization vs sponsorship, or any ambiguous authorization wording (H-1B, work permit, negated), vetoes the pair. The bare word "immigration" does not make a sponsorship question ambiguous here.
- **QM6** `countries`: differing resolved countries or country abbreviations veto, and a country named on only one side vetoes.
- **QM7** `tier`: differing risk tiers veto, unless the two share an intent, in which case the riskier tier governs.
- **QM8** `numbers`: differing digit strings veto ("at least 18" / "at least 21").
- **QM9** `strict_tier`: for legal and commercial pairs (and anything the classifier does not recognise, which it files as legal), the meaningful words must be equal, or the pair must share an intent and differ only by that topic's interchangeable wording.
- **QM10** Intents are an ordered table of topics; authorization and sponsorship come from `question_semantics`. Compound or unclear text has no intent.
- **QM11** Outside the strict tiers a pair matches on trigram score at or above the threshold, or on a shared intent.
- **QM12** `named_terms`: a capitalised name in one question that appears nowhere in the other (Python / Java, Canadian / British) vetoes the pair. Shouting, slash-joined labels and Title Case are not names.
- **QM13** `intent`: two different recognised intents veto the pair.

## Retrieval

- **QC1** Only the asking profile's own rows, even when the caller preloads a mixed set.
- **QC2** Expired rows, `legacy:*` rows and rows with no question text are skipped, and so is the exact normalised key.
- **QC3** Learned rows are used only while the profile has learning on.
- **QC4** A location-sensitive question uses only a row saved for the job's region or marked "applies everywhere".
- **QC5** Ranking is locked, then user, learned and imported, then score; the result is capped.
- **QC6** The stored value is mapped onto the live field's options, or is `None` (text only).
- **QC7** Retrieval never writes and makes no query beyond the preload.

## Review and measurement

- **QR1** A candidate never changes a field's value, review flag, confirmation flag or the draft's outcome.
- **QR2** Taking a candidate is a separate "use this answer" action, validated like a typed value; a legal or commercial one stays held for the per-field confirmation.
- **QR3** A value taken from a candidate is not the user's authorship: the learner ignores it and precision does not count it.
- **QR4** Candidates are dropped from a draft when it is sent.
- **QR5** Precision is reported per tier over typed or changed answers only, as counts, with no question text or values.
