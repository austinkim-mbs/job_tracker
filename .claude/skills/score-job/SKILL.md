---
name: score-job
description: Score one job description (pasted text or a job posting URL) against the resume using the evidence-based fit rubric (interview_funnel/prompts/prep/fit_rubric_prompt.md); the older ATS + recruiter-fit rubric (ats_score_prompt.md) is still available on request. Use when the user pastes a JD or a job link and asks "should I apply", "score this", "ATS score this", or similar — this is the ad-hoc single-posting scorer, not the bulk CSV scorers.
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
- `resume_scorer.py` — Stage-2 calibrated scorer (claude-sonnet-4-6,
  `interview_funnel/prompts/prep/stage2_scorer_prompt.md`), run in bulk by
  `manage.py score_postings` and by `poll_applications.py`; saves scores
  onto `Posting`. Not meant for ad-hoc use.
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

Default: read **`interview_funnel/prompts/prep/fit_rubric_prompt.md`** in
full. Substitute the resume text for `{RESUME}`, the JD text for
`{JOB_POSTING}`, and anything the user has stated about their experience or
preferences in this conversation for `{EXTRA}` (or "none"). Follow it exactly
as written: six-category 0-100 documented qualification fit, hard
constraints reported separately, career-direction fit (High/Medium/Low),
the seven output sections, and the "do not equate" list. Show the rubric
only before the first posting scored in a conversation.

For the running comparison (section 7), read `../fit_scores.csv` (one
directory above the repo root) if it exists, and after scoring append one row
(`date,company,role,fit_score,confidence,main_gap,priority,url`), creating
the file with that header if needed. Scores from the older rubrics
(ats_score_prompt.md, score_bay_area_postings.py) are uncalibrated against
this one -- label them so, never rank them together.

If the user asks for the old "ATS score" / keyword-parser view, use
`interview_funnel/prompts/prep/ats_score_prompt.md` instead (substitute
`{RESUME}` / `{JOB_POSTING}` and follow it as written).

Do not paraphrase or shorten either rubric's rules. If a file has been
edited since this skill was written, the file wins.

## 4. Writes

Present the assessment in conversation. The only file write is the one
`fit_scores.csv` row from section 3 -- don't touch the bulk-scorer CSVs or
the DB.
