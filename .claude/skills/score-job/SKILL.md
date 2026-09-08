---
name: score-job
description: Score one job description (pasted text or a job posting URL) against the resume using the repo's real ATS + recruiter-fit rubric (interview_funnel/prompts/prep/ats_score_prompt.md). Use when the user pastes a JD or a job link and asks "should I apply", "score this", "ATS score this", or similar — this is the ad-hoc single-posting scorer, not the bulk CSV scorers.
user-invocable: true
allowed-tools:
  - Read
  - Bash
  - WebFetch
  - AskUserQuestion
---

# /score-job — Ad-hoc JD Scoring

Arguments passed: `$ARGUMENTS`

This is the interactive, single-posting scorer — distinct from two other
scorers in this repo, don't confuse them:
- `resume_scorer.py` — simpler JSON-only score, called automatically by the
  Gmail-polling pipeline (`main.py`/`poll_applications.py`) via the
  Anthropic API, not meant for ad-hoc use.
- `score_bay_area_postings.py` / `score_biotech_postings.py` — deterministic
  regex/keyword scorers over the whole `Posting` table for bulk CSV export
  (see the `pull-new-jobs` skill).

This skill instead applies **`interview_funnel/prompts/prep/ats_score_prompt.md`**
directly, with you (Claude) doing the scoring in this conversation — no
Anthropic API call needed, you already are the model that prompt is written
for.

---

## 1. Get the job description text

- If `$ARGUMENTS` (or what the user pasted) is a URL: `WebFetch` it and
  extract the job description body.
  - Heads up: several ATS platforms this repo already knows about
    (Ashby, Gem, Rippling) render postings client-side — a fetch of the raw
    page can come back mostly empty. If the fetched text looks too short or
    boilerplate-only to be a real JD, say so and ask the user to paste the
    posting text directly instead of guessing from a thin fetch.
- Otherwise treat the input as the JD text itself. If nothing was passed at
  all, ask the user to paste the job description or a link.

## 2. Get the resume text

Reuse the resume path already configured in `.env` — don't ask the user to
re-supply it. Extract text directly (don't import `resume_scorer.py`; its
module-level `anthropic.Anthropic(...)` client construction requires
`ANTHROPIC_API_KEY` to be set, which this skill doesn't need):

```
python -c "
from config import RESUME_PATH
import pdfplumber
with pdfplumber.open(RESUME_PATH) as pdf:
    print('\n'.join(p.extract_text() or '' for p in pdf.pages))
"
```

If `RESUME_PATH` isn't set or the file doesn't exist, point the user at the
`init` skill instead of guessing a path.

## 3. Apply the rubric

Read `interview_funnel/prompts/prep/ats_score_prompt.md` in full. Substitute
the resume text for `{RESUME}` and the JD text for `{JOB_POSTING}`, then
follow that prompt's instructions exactly as written — same output format
(dual ATS/recruiter-fit score, stack-match table, requirements walk, real
gaps, bottom line, "if the gap were closed"), same calibration bands
(absolute 0-100, not relative to other postings scored this session), same
rules (don't inflate to be encouraging, distinguish AI-tool-usage from
AI-product-building, flag hidden seniority and legal/eligibility blockers
explicitly, credit biotech/life-sciences domain adjacency).

Do not paraphrase or shorten the rubric's rules — they encode specific,
previously-learned failure modes (e.g. the domain-adjacency bonus, the
calibration-drift warning). Follow the file as it exists now; if it's been
edited since this skill was written, the file wins.

## 4. Don't auto-write anywhere

Present the score in conversation. Only write it to a file (or append to
one of the tracked CSVs) if the user explicitly asks — this skill is a
one-off read, not part of the bulk pipeline's data flow.
