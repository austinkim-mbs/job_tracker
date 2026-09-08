This repo houses an application meant to faciliate/aid the interview process.

There are two distinct django apps that are meant to work together in conjunction

- A tracker app that finds and tracks information about job postings.
- A interview funnel app that will use this information as well as help you prep for the interview process.

Currently this is used in conjunction with claude but is inteded to be ported over to ollama.

## Claude Code skills

This repo ships project-local Claude Code skills (`.claude/skills/`) that wrap
the pipeline below. Run these from Claude Code inside this directory:

- **`/init`** — first-time (or re-) setup: points `RESUME_PATH` at a real
  file, optionally walks through Gmail API credentials, and asks a series of
  questions to shape which jobs score highly (target locations, titles,
  stack, seniority, domain). Flags that the scoring scripts are hand-tuned
  regex, not a config file — expect to keep adjusting them.
- **`/onboard-companies`** — discovers new companies via the Wayback Machine
  CDX index and VC-portfolio Consider boards, then pulls their postings.
  Only covers ATS platforms this repo already knows how to talk to (Ashby,
  Lever, Greenhouse, Gem, Breezy auto-discovered; SmartRecruiters, Workable,
  Rippling onboardable but not auto-discovered) — Workday, iCIMS, Jobvite,
  and bespoke career sites aren't supported.
- **`/pull-new-jobs`** — refreshes postings for companies already on the
  preferred-company watchlist and re-scores them against the resume. Doesn't
  discover new companies; run `/onboard-companies` first for that.
- **`/score-job`** — scores one job description (pasted text or a posting
  URL) against the resume, using the same ATS + recruiter-fit rubric
  (`interview_funnel/prompts/prep/ats_score_prompt.md`) applied directly by
  Claude in the conversation. For a single ad-hoc posting, not the bulk CSV
  scorers.


Some notable concerns
- This is written mainly for windows, so some of the jobs are unfortunately not "production ready"
- I used sqllite for ease, but obviously it can end up being quite sizeable.
- Added gmail integration at some point to track applications, but claude is pretty bad at sentiment analysis. It still works decently well, but needs a human in the loop every now and then.
- The ats scorer is very ok - I find using something like perplexity is significantly better at scoring and generating resumes.
