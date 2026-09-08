"""
Same SWE/Fullstack/MTS title filter, exclude filter, and ATS-style scoring as
score_bay_area_postings.py, but swapped to a biotech/life-sciences domain
filter instead of a Bay Area location filter (biotech roles aren't
concentrated in one metro the way this project's other search is), and a
120-day rolling posting-age window instead of 90.

Run: python score_biotech_postings.py
"""

import csv
import os
from datetime import datetime, timedelta, timezone

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dj_config.settings")
django.setup()

from django.db.models import Q  # noqa: E402

from tracker.models import Posting  # noqa: E402
from score_bay_area_postings import DOMAIN_BONUS_RE, score_posting  # noqa: E402

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "biotech_fullstack_postings_scored.csv")
ROLLING_WINDOW_DAYS = 120


def main():
    biotech_q = Q(title__iregex=DOMAIN_BONUS_RE.pattern) | Q(description__iregex=DOMAIN_BONUS_RE.pattern)
    swe_filter = (
        Q(title__iregex=r"software engineer")
        | Q(title__iregex=r"full[- ]?stack")
        | Q(title__iregex=r"member of technical staff")
    )
    exclude_filter = Q(title__iregex=r"senior|\bsr\.?\b|(?<!technical )staff")
    cutoff = datetime.now(timezone.utc) - timedelta(days=ROLLING_WINDOW_DAYS)

    qs = (
        Posting.objects.filter(swe_filter, biotech_q, posted_at__gte=cutoff)
        .exclude(exclude_filter)
        .select_related("company")
    )
    print(f"scoring {qs.count()} postings...")

    rows = []
    for p in qs:
        score, notes = score_posting(p.title, p.description)
        rows.append({
            "company": p.company.name or p.company.slug,
            "title": p.title,
            "location": p.location,
            "avg_comp": p.avg_comp or "",
            "lowest_comp": p.lowest_comp or "",
            "workplace_type": p.workplace_type,
            "posted_at": p.posted_at.date().isoformat() if p.posted_at else "",
            "ats_fit_score": score,
            "ats_notes": notes,
            "url": p.url,
        })

    rows.sort(key=lambda r: r["ats_fit_score"], reverse=True)

    fieldnames = ["company", "title", "location", "avg_comp", "lowest_comp",
                  "workplace_type", "posted_at", "ats_fit_score", "ats_notes", "url"]
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} rows to {os.path.abspath(OUT_PATH)}")
    if rows:
        print(f"score distribution: min={rows[-1]['ats_fit_score']}, max={rows[0]['ats_fit_score']}, "
              f"avg={sum(r['ats_fit_score'] for r in rows) / len(rows):.1f}")


if __name__ == "__main__":
    main()
