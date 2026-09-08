---
name: init
description: First-time (or re-)setup for this job tracker — points RESUME_PATH at a real file, optionally walks through Gmail API credentials, and asks a series of questions to shape which jobs get scored highly. Use when the user says "/init", "set up this repo", "onboard me", or is running this project for the first time.
user-invocable: true
allowed-tools:
  - Read
  - Edit
  - Write
  - Glob
  - Bash
  - AskUserQuestion
---

# /init — Job Tracker Setup Wizard

This is a project-local setup flow, distinct from the generic `/init` that
writes a CLAUDE.md. It does not touch git, and it does not run any network
calls itself except the Gmail OAuth step (which the user drives through a
browser).

Arguments passed: `$ARGUMENTS`

---

## 1. Resume path

1. `Glob` the repo root for `*.pdf`, `*.doc`, `*.docx`.
2. If exactly one match, confirm with the user ("Use `<file>` as your
   resume?"). If multiple or zero matches, ask the user for a path
   (`AskUserQuestion` with an "other" free-text answer works, or just ask
   directly) — it does not need to live inside the repo; anywhere on disk is
   fine, e.g. `~/Documents/resume.pdf`.
3. Update `RESUME_PATH` in `.env` (create `.env` from `.env.example` first if
   `.env` doesn't exist yet). Match the existing quoting style
   (`RESUME_PATH="C:/path/to/file.pdf"` on Windows paths with spaces).
4. Do **not** copy the resume file into the repo — `RESUME_PATH` just points
   at wherever it already lives. Resumes contain PII; keeping it outside the
   tracked tree is deliberate.

## 2. Gmail API (optional)

Ask the user whether they want Gmail polling (auto-detects application
status from emails) set up now, or skip it for later.

If yes:
1. Check `credentials/google_oauth.json`. If missing, tell the user exactly
   how to get it — you cannot do this step for them:
   - Google Cloud Console → APIs & Services → Credentials
   - Create an OAuth client ID, type "Desktop app"
   - Download the JSON, save it as `credentials/google_oauth.json` in this
     repo (already gitignored — `credentials/*.json`)
2. Ask for their target Google Sheet ID (the long id in the sheet's URL) and
   write it to `SHEET_ID` in `.env`.
3. Explain that the *first* run of `python main.py` (or anything that
   constructs `GmailPoller`) will open a browser window for them to sign in
   and consent — that's what mints `credentials/token.json`. You cannot
   trigger or complete that browser flow yourself; tell them to run it
   themselves after this setup finishes.

If skipped, leave `SHEET_ID` alone and note that `main.py` (Gmail polling +
Sheets sync) won't work until this is done later, but the crawler/scoring
scripts (job discovery, `score_bay_area_postings.py`, etc.) don't need it.

Also confirm `ANTHROPIC_API_KEY` is set in `.env` if they want Claude-based
email classification (`email_classifier.py`) or resume ATS scoring
(`resume_scorer.py`) — the deterministic scorer (`score_bay_area_postings.py`)
does not need it.

## 3. Job-search preferences ("job path")

Ask the user, in one pass (AskUserQuestion supports multiple questions at
once):
- Target locations (e.g. "Bay Area only", "Bay Area + Remote US", "NYC",
  "Remote anywhere")
- Target role titles (e.g. "Software Engineer / Full-stack", "ML Engineer",
  "Data Engineer")
- Core tech stack to score for (languages, frameworks — pull a starting
  guess from their resume text if you can read it)
- Seniority band (new grad / mid-level IC / senior / staff+)
- Any domain preference (e.g. biotech, fintech, no preference)

**Tell the user up front this step is heuristic, not magic.** The scoring
scripts in this repo (`score_bay_area_postings.py`,
`score_biotech_postings.py`) are one-off, hand-tuned keyword/regex scorers —
the candidate profile, location list, and title patterns are hardcoded
constants near the top of the file, not read from a config. There's no
single settings knob that reshapes scoring for a new person.

After collecting answers, offer to edit `score_bay_area_postings.py` to
match:
- `BAY_AREA_CITIES` → their location list (rename/generalize if their
  target isn't the Bay Area at all — be upfront that the whole file is
  Bay-Area-flavored and a genuinely different location target may be better
  served by copying it to a new script than repurposing this one)
- `SWE_TITLE_PATTERNS` / `DISQUALIFY_RE` → their target titles
- `CATEGORY_KEYWORDS` → their tech stack
- the module docstring's candidate-profile summary and `DOMAIN_BONUS_RE`

Make the edits, then say plainly: **expect to keep tuning this by hand** as
false positives/negatives show up in scored output — it's regex, not ML.

## 4. Summary

End with a short status list: what's configured, what's still manual
(Google OAuth client JSON, completing the browser consent flow,
`ANTHROPIC_API_KEY`), and point to the `pull-new-jobs` and
`onboard-companies` skills as next steps.
