---
name: onboard-companies
description: Discover new companies via the Wayback Machine CDX index ("cmx") and VC-portfolio Consider boards, then pull their postings. Use when the user says "onboard new companies", "find new companies", "grow the company list", or wants to expand beyond the current watchlist.
user-invocable: true
allowed-tools:
  - Read
  - Bash
  - AskUserQuestion
---

# /onboard-companies — Discover & Onboard New Companies

Arguments passed: `$ARGUMENTS`

---

## Coverage disclaimer — say this up front, every run

**This pipeline only discovers and onboards companies on ATS platforms this
repo already knows how to talk to.** Tell the user this before running
anything:

- Discovered automatically via the Wayback CDX crawler (`job_crawler.py`):
  **Ashby, Lever, Greenhouse (both `boards.greenhouse.io` and
  `job-boards.greenhouse.io`), Gem, Breezy**.
- Also onboardable (one company at a time, or via a Consider VC-portfolio
  board) but **not** discovered by the CDX crawler itself:
  **SmartRecruiters, Workable, Rippling**.
- **Not supported at all** — companies on Workday, iCIMS, Jobvite, Oracle
  HCM, or a fully custom/bespoke careers page are invisible to this
  pipeline. They won't be discovered, and `onboard_company` will fail to
  find a board for them even if you point it at their site. There's no
  fetcher for these platforms in `fetch_postings.py`.

If the user cares about a specific company and it's not on one of the
supported platforms, say so plainly instead of letting it silently vanish
into the "unsupported, skipped" counts these commands report.

## 1. Wayback CDX discovery ("cmx")

```
python job_crawler.py
```

Incremental — stores the newest capture timestamp per target in
`crawler_state.json` and only fetches what's new since last run. Default
rate is 1 req/s against the Internet Archive's CDX server; ask before
raising `--rps` (be a polite citizen of a free public API). Scope to one
platform with `--target ashby` (choices: `ashby`, `lever`, `greenhouse`,
`greenhouse-jobboards`, `gem`, `breezy`) if the user only wants one.

This can take a while on a full run across all targets — set that
expectation before starting.

## 2. Import discovered slugs into the Company table

```
python manage.py import_companies
```

Reads `crawler_state.json`, filters obviously-garbage slugs
(`slug_filters.py`), and creates `Company` rows for anything new. Reports
new-vs-total per platform.

## 3. Optional: VC-portfolio boards (Consider)

For portfolio-wide boards like Lightspeed's (`jobs.lsvp.com`):

```
python manage.py discover_consider_board
```

Default targets Lightspeed; other Consider-hosted VC boards need
`--host <domain> --board-id <id>`. This only creates `Company` rows for
jobs whose `applyUrl` lands on a supported platform (same list as above) —
everything else is counted as "unsupported" and skipped, same caveat as
section 1.

## 4. Optional: onboard one specific company by URL

If the user names a specific company they want added right now rather than
waiting for the next full crawl:

```
python manage.py onboard_company https://jobs.ashbyhq.com/acme
python manage.py onboard_company https://www.acme.com/careers
```

Works with a direct ATS board link (fastest, most reliable) or a bare
company/careers URL (best-effort slug-guessing against every supported
platform — can fail even for a real, supported-platform company if its ATS
slug doesn't match its domain name). If it fails, ask the user for the
company's actual board URL instead of guessing further.

## 5. Pull postings for what was just onboarded

Company rows with no postings yet are invisible to scoring. Run the full
sweep:

```
python manage.py fetch_postings
```

**Warn the user this covers every known company, not just the new ones, and
can take on the order of an hour.** If they want just the newly-onboarded
companies refreshed quickly instead, get their `Company` ids (e.g. via
`python manage.py shell -c "..."` filtering on recent `id`s or a
platform-scoped query) and pass `--company-ids-file`.

## 6. Suggest next step

If new companies now qualify (have a current US/Remote engineering
posting), point the user at the `pull-new-jobs` skill's ranking step
(`rank_preferred_companies`) so they show up in the watchlist rather than
only the full sweep.
