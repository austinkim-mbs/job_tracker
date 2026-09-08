---
name: pull-new-jobs
description: Refresh postings for already-onboarded, preferred companies and re-score them against the resume. Use when the user says "pull new jobs", "what's new", "refresh my job list", or wants an updated scored list without discovering new companies.
user-invocable: true
allowed-tools:
  - Read
  - Bash
  - AskUserQuestion
---

# /pull-new-jobs — Refresh & Score the Watchlist

Scope: companies **already known** to the tracker (the `Company` table).
This does not discover new companies — that's the `onboard-companies`
skill. Run that first if the user wants the universe of companies to grow.

Arguments passed: `$ARGUMENTS`

---

## 1. Make sure companies are ranked

`fetch_preferred_companies` only refreshes companies with a
`preference_rank` set. Check whether any exist:

```
python manage.py shell -c "from tracker.models import Company; print(Company.objects.filter(preference_rank__isnull=False).count())"
```

If zero (or the user wants to pick up newly-onboarded companies that
qualified since the last rank pass), run:

```
python manage.py rank_preferred_companies
```

This only ranks companies that have at least one current engineering-titled
posting (tier 1 = CA/NY/Remote, tier 2 = elsewhere US/Europe, tier 3 =
everywhere else) and never overwrites a rank set by hand — add `--force` only
if the user explicitly wants a full recompute.

## 2. Refresh postings for the watchlist

```
python manage.py fetch_preferred_companies
```

Ask the user if they want to scope this run:
- `--tier 1` (or 2/3) to refresh just one priority tier
- `--platform ashby` (etc.) to refresh just one ATS

**This is a two-phase pipeline by design — don't "simplify" it into one
loop:**
1. **Fetch, in parallel:** one subprocess per ATS platform (`dump_postings`),
   all launched at once and awaited together. Each subprocess only hits its
   own platform's public API and writes to its own dump file — zero DB
   writes — so running every platform concurrently is safe; they can't
   contend with each other.
2. **Ingest, sequentially:** once every fetch subprocess has finished, a
   single `ingest_postings` pass reads all the dump files and writes to the
   DB one at a time, in one process.

The split exists because SQLite only tolerates one writer at a time — an
earlier version wrote directly to the DB from each per-platform process and
hit recurring "database is locked" crashes on the heaviest platforms
(ashby/greenhouse) whenever they ran concurrently. Never route platform
fetches through separate concurrent DB writes; always fetch-parallel /
ingest-serial through this command (or `dump_postings` +
`ingest_postings` directly, scoped the same way) instead.

Safe to re-run any time — the ingest is an upsert.

## 3. Score against the resume

Run the scorer that matches the user's job-path profile from `/init` —
default to:

```
python score_bay_area_postings.py
```

(`score_biotech_postings.py` exists for the biotech-scoped variant.) This is
a deterministic keyword/regex scorer, not an LLM call — fast, free, safe to
re-run. It writes a fresh CSV one directory above the repo root
(`../bay_area_fullstack_postings_scored.csv` relative to this file) and
overwrites the previous run's output.

If the user's `/init` answers meant this script was customized for a
different location/stack than "Bay Area fullstack," use whatever script (or
edited copy) resulted from that setup instead.

## 4. Report back

Read the CSV's header + score distribution (the script itself prints
`min=/max=/avg=` on completion — surface that), and show the top 10-15 rows
by `ats_fit_score`. Mention the output path so the user can open the full
CSV themselves.

Do not try to interpret "preferred" as requiring `ANTHROPIC_API_KEY` — this
whole path is Claude-free. The Claude-based ATS scoring
(`resume_scorer.py`) only runs inside `main.py`'s Gmail-polling loop, for
applications that have actually replied — different pipeline, not part of
this skill.
