---
date: 2026-10-05
topic: github-import-enrichment
---

# GitHub Import Enrichment

## Summary

Make the GitHub import far richer than a username check. It proposes a curated list of the user's own projects (description, dates, the skills found in each) and a list of skills, each shown with proof such as "Python, 14 repos, active since 2019". Everything goes through the existing review page, and the import stays public-data-only.

---

## Problem Frame

Today the GitHub import proposes three things: the profile link, up to five language tags, and a portfolio URL. The tags come from a 14-item job-matching vocabulary, so in practice a user sees one or two tags (Python, JavaScript, Golang, Kubernetes) and nothing else. A developer's GitHub is often the best record of what they have built and which tools they actually use, and the import throws almost all of it away.

Skills are the sharpest gap. The profile has no list of the user's skills as a whole: skills exist only on individual job and project entries that came from a resume. A user with a thin resume but an active GitHub gets no skills at all, and application questions about tools and frameworks cannot be answered from evidence.

---

## Actors

- A1. Job seeker: owns the profile, runs the import, reviews and accepts or rejects each proposal.
- A2. Application answering (downstream): reads accepted skills and projects when filling application questions.

---

## Key Flows

- F1. Import from GitHub
  - **Trigger:** The user enters a GitHub username on the Import tab.
  - **Actors:** A1
  - **Steps:** The user submits the username. The import reads the account's public repos and picks the strongest ones. The review page shows proposed projects and proposed skills with proof lines, all unticked. The user ticks, edits or rejects each one and applies.
  - **Outcome:** Accepted projects are saved as project entries, accepted skills are saved on the profile with their proof, and nothing the user set or locked is overwritten.
  - **Covered by:** R1, R2, R3, R5, R6, R9, R10

---

## Requirements

**Projects**
- R1. Propose the user's own repositories as project entries: name, description, date range (first to last activity), and the skills found in it. Forks, archived repos and repos owned by others are never proposed.
- R2. Show a curated shortlist of about ten projects, ranked with pinned repos first, then by stars and recent activity. The user can expand to see more, up to a hard cap.
- R3. Every proposed project starts unticked. The user decides what enters the profile.

**Skills with evidence**
- R4. Each proposed skill comes from what the repos show: language mix, repository topics, and dependency files in the shortlisted repos (so frameworks and libraries such as React or Django appear, not only languages). A skill that no repo shows is never proposed.
- R5. Each proposed skill carries a visible proof line: how many repos, and the first and last activity dates. The proof stays with the skill after it is accepted.
- R6. Accepted skills are stored as a profile-level skill list, separate from the skills on individual entries. The list is not limited to the 14 job-matching tags.
- R7. Skills that are also job-matching tags continue to feed the user's tags as they do today, subject to the same review and the same keep-what-the-user-set rule.

**Years and separation**
- R8. GitHub-derived time is shown as "seen on GitHub since YYYY" and is never added to job-based years for a skill. Answers that use years of experience keep using job-based years only.

**Review, safety and limits**
- R9. All proposals go through the existing per-item review. A value the user edited is saved as user-set. A skill or project the user already has, set, learned or locked is shown as kept and not overwritten.
- R10. The import uses public data only, with no login and no stored credential. Private repos and private contributions are invisible, and the review page says that the evidence reflects public activity only.
- R11. A re-import never deletes what the user previously accepted.

---

## Acceptance Examples

- AE1. **Covers R1, R3.** Given an account with 40 own repos, 15 forks and 5 archived repos, when the user imports, the review shows at most about ten of the own, non-archived repos as projects, none of the forks, and every one unticked.
- AE2. **Covers R4, R5.** Given a user whose repos use Python in 14 of them since 2019 and Django in 3, when the import runs, the skills list proposes Python with "14 repos, since 2019" and Django with "3 repos", and proposes no skill that appears in no repo.
- AE3. **Covers R8.** Given Python on GitHub since 2019 and Python on job entries only from 2022, when the user has accepted both, the profile shows job years from 2022 and a separate "seen on GitHub since 2019", and application answers use only the job years.
- AE4. **Covers R9, R11.** Given the user already edited a project's description by hand, when they import again, that project is shown as kept and is not replaced, and nothing previously accepted is removed.

---

## Success Criteria

- A developer with an active GitHub and a thin resume ends the import with a useful skill list and a handful of real projects after a quick review, instead of one or two tags.
- The user can see why each skill is on their profile and can trust that nothing appeared without being asked about.
- Planning can start without inventing what counts as a project, where skills are stored, how evidence is shown, or how GitHub years relate to job years.

---

## Scope Boundaries

- Open-source contributions to other people's repositories (merged pull requests, issues) are deferred until real data from projects and skills has been seen.
- Bio-derived profile fields (headline from bio, employer from company, location) stay as they are today and are not expanded in this work.
- Forks, archived repos, private repos and organization repos the user does not own are excluded.
- Answering "years of X" questions from GitHub years is not part of this work (R8 keeps them separate).
- No GitHub login, token paste, or stored credential. Private activity is out of reach by design.

---

## Key Decisions

- Skills and projects first: they carry the most value from GitHub, and contributions can follow once the data quality is known.
- Public data only: no credential to store or revoke, at the cost of undercounting people who work mostly in private repos.
- Evidence over inference: a skill appears only if a repo shows it, with the proof visible, so the user can judge each one.
- Separate GitHub years from job years: hobby and side-project time must not inflate "years of professional experience".
- Curated and unticked: a prolific account can have hundreds of repos, so the review shows a short list the user opts into.

---

## Dependencies / Assumptions

- Assumption: a profile-level skill list is the right home for skills with evidence. Today skills live only on entries, so this is new profile data. The synthesis flagged it as a fork and it was left as proposed; reconfirm in planning if it proves costly.
- Assumption: public API rate limits are sufficient because extra calls are spent only on the shortlisted repos, and the server's own token raises the limit. This is untested against heavy use.
- Builds on the Phase 4 import (resume entries, per-item review, keep-what-the-user-set rule). Requirements in `docs/plans/2026-10-03-2300-consolidated-requirements.md` and `docs/plans/2026-10-05-001-feat-profile-import-plan.md` apply unless this document says otherwise.

---

## Outstanding Questions

### Resolve Before Planning

- None.

### Deferred to Planning

- [Affects R2][Technical] How "strongest" repos are ranked when pinned repos are not available through the public API.
- [Affects R4][Needs research] Which dependency files to read and how skill names in them map to the existing skills vocabulary.
- [Affects R6][Technical] How the profile-level skill list is stored, shown on the Import tab, and kept in step with skills on entries.
- [Affects R10][Needs research] Whether the number of calls for a typical account stays within public rate limits without a server token.
- [Affects R1][User decision] Whether bio-derived fields should be pulled in at the same time since the same data is already fetched (currently deferred).
