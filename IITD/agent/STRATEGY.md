# Strategy note: Battle Arena agent

**What we buy.**
- Search pages are nearly free (1 credit per 100 summaries), so in recon we sweep up to `SEARCH_PAGES_PER_ROLE` pages for every role we're hiring for.
- We rank summaries locally by skill overlap with the requisition. Skills are normalised first (k8s→kubernetes, Postgres→postgresql, JS→javascript…). Experience and city also count.
- Full profiles are bought only in batches (1.2 credits each, against 2 each singly), and only for the top `PROFILE_DEPTH × headcount` per requisition.
- A requisition that still has fewer than `BACKUP_FACTOR` eligible people per slot gets a deeper tier, up to `PROFILE_CAP`.
- `/assess` (25 credits) is bought only for top picks with a doubt: no assessment, one near the bar, suspiciously perfect scores, red-flag recruiter notes, or impossible experience. It's capped by `ASSESS_BUDGET`.

**What we sign.**
- A candidate must clear the real bar from the full profile: assessment ≥ minimum, notice ≤ maximum, expected CTC ≤ budget, and not claimed. We parse "71/100", "7.1/10", "2 months", "18 LPA", "₹18,00,000" and similar formats.
- Verified assessments override self-reported ones. A failed reference check or a fabrication marks the candidate dead.
- Doubtful and unverified candidates are never signed.
- Duplicates are caught by identity keys (email, phone, normalised name + city + years), so we never pay for a `same_person` rejection.
- Value = requisition points × quality, where quality blends skill overlap, margin over the assessment bar and note sentiment.
- The moment the market opens, all requisitions are filled best-first in parallel under a team-wide throttle (8.5 req/s). Rejections fall through to backups.

**When we stop.** Every spend passes one rule: `expected points > credits × PENALTY_FACTOR`, plus a hard `MAX_SPEND` ceiling. In closing, offers cost 20, so the same rule becomes stricter automatically. Once slots are full, we only poll free endpoints.

**Robustness.**
- State (profiles, assessments, signings, spend) is saved to disk atomically after every step, so a crash or redeploy never pays twice.
- The top-level loop catches every error, so a crash never burns one of our two restarts.
- Rate limits are prevented by the client-side throttle rather than retried.
- Empty searches, 409 wrong-phase responses and credit exhaustion each have their own branch.

**What the agent learns while running (learning.py).**
- *Assessment model:* self-reported scores are inflated. Each paid `/assess` gives a (self, verified) pair. We fit verified ≈ a·self + b and judge unverified candidates on the prediction minus half a standard deviation. After about 40 calibration assessments, the agent stops trusting face values without paying 25 credits for every candidate.
- *Fraud by note:* the agent tracks which recruiter notes go with failed verification (identity not verified, or a reference check that isn't clean), and skips unverified candidates who carry such notes.
- *Note scorer:* each distinct recruiter note is scored once (−2..+2) by `/reason` in batches of 40, then cached. That costs a few credits in total instead of one call per candidate, with a keyword fallback.

**Tuned at kick-off:** `PENALTY_FACTOR`=0.05, so a credit costs 0.05 points. An offer (10 credits) costs 0.5 points and a profile 0.06, which makes information cheap relative to a signing. So we sweep 120 pages per role, buy 12 profiles per slot, allow up to 4,000 credits of assessments, and cap total spend at `MAX_SPEND`=25,000.

**Data-format check:** before finalising the parsers, we made 7 manual calls during recon (31 credits): `/ledger`, `/requisitions`, 1 search, 2 profiles, 1 assess and 1 `/reason`. They were to learn the field formats. No decisions were fed to the agent.

**Incident, and its fix (11:49–12:01).**
- *What happened:* while the market was open, the server briefly reported `phase: closed` during an organiser pause. Our agent read any `closed` seen after the arena opened as the end of the arena, and exited at 11:49.
- *Impact:* none on the score. All 116 signings were already held on the server, and no credits were spent or lost (credits used stayed at 13,334).
- *Fix (12:01):* the agent now exits only on a `closed` that comes after it has seen the `closing` phase. That flag (`seen_closing`) is saved in the state file, so it survives restarts. Any earlier `closed` is treated as a pause: the agent keeps its signings and waits.
- *Restart:* the agent was restarted at 12:01 from its saved state, with no re-buying and no new spend.

**With two more hours:** releasing a weak signing to upgrade when the gain exceeds 15 × PF; using `/reason` on ambiguous notes of finalists (10 per call); using `/market` price pressure to prioritise contested requisitions.

**AI assistance:** Claude Code (Anthropic) helped write the agent.
