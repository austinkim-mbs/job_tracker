You are conducting due diligence on a company for a software engineer evaluating whether to join. Summarize recurring themes from reputable employee discussions and recent news.

Preferred sources:
 1. teamblind
 2. glassdoor
 3. hackernews
 4. Reddit
 5. Recent news

Highlight the following
- WLB
- Leadership sentiment (CEO/CTO/VP Engineering/Executive Team)
- Most commonly recurring positive themes.
- Most commonly recurring negative themes.
- Recent company events that could influence engineering or hiring.
    Source priority:

    Engineering-specific sentiment:
    - Blind: highest
    - Hacker News: high
    - Company blog: high
    - Reddit engineering communities: medium

    General company culture:
    - Glassdoor
    - Reddit

    Company facts:
    - News sources
    - SEC filings
    - Company announcements

If engineering role based sentiments differ from non-engineering role sentiments, weigh those slightly more. Also, weigh recent news (12-18months) much more than older news. 
The current year is 2026.

Add links when providing evidence as part of output.

If inadequate research is found, put unknown.
If mixed sentiment is found, put mixed.
Do not hallucinate.

Add a confidence rating from 0-1.0 for each top level section.

Output Structure:
{
    "overall_sentiment": "...",
    "engineering_sentiment": 1-10,
    "interview_rigor": 1-10,
    "work_life_balance": {
        "rating": "...",
        "confidence": 0.0,
        "evidence": []
    },
    "hiring_signals": {
        "engineering_growth":"unknown",
        "layoff_risk":"low",
        "hiring_momentum":"medium"
    }
    "leadership": {
        "known_events": [],
        "sentiment": {},
        "reason": "example: No engineering leadership discussions found."
    }
    "positive_themes": [],
    "negative_themes": [],
    "recent_news": [],
    "risks": [],
}

## After completing research

If this prompt is being run from inside this Django project (i.e. `manage.py` and the `tracker`/`interview_funnel` apps are reachable, not just a plain chat session), persist the result:

1. Write the final JSON object to a file (e.g. in the scratchpad).
2. Run:
   ```
   python manage.py update_company_gen_info "<Company Name>" --json-file <path-to-file>
   ```
   Use the company's name as it appears in the `tracker_company` table (check with the Django shell or admin if unsure -- the lookup is an exact, case-insensitive match). If it's ambiguous or missing, pass `--id <pk>` instead, or ask the user which row to use.
3. The command is a no-op if `gen_info` is already set on that company -- it will say so rather than overwrite. Don't pass `--force` unless the user explicitly asks to refresh a company's existing writeup.
4. Skip this step entirely if there's no Django project in scope (e.g. researching a company with no corresponding `Company` row, or running outside this repo).