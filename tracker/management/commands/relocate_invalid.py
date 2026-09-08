"""
For Company rows marked is_invalid=True (their original ATS board 404s) whose
slug still looks like a real company name -- not a crawler artifact caught by
is_garbage_slug -- the company may simply have moved to a different ATS
under the same slug. Probes each one's slug against every OTHER platform in
FETCHERS and, on a hit, onboards it there exactly like onboard_company.py
does for a single URL: get_or_create the Company on the new platform and
upsert its current postings.

Does not touch the original dead row -- that board really is gone, staying
is_invalid=True there is correct. This just checks whether the same slug is
alive somewhere else.

Run: python manage.py relocate_invalid --platform ashby
     python manage.py relocate_invalid --platform ashby --limit 50   # quick test
     python manage.py relocate_invalid --platform greenhouse --target ashby   # check one target platform only
"""

import requests
from django.core.management.base import BaseCommand, CommandError

from comp_parser import parse_comp_range
from job_crawler import RateLimiter
from tracker.management.commands.fetch_postings import FETCHERS, PLATFORM_RPS, USER_AGENT, upsert_with_retry
from tracker.models import Company


class Command(BaseCommand):
    help = "Probe invalid Company rows' slugs against every other ATS platform to find where they actually moved."

    def add_arguments(self, parser):
        parser.add_argument("--platform", required=True, choices=list(FETCHERS.keys()), help="source platform whose invalid companies to re-probe")
        parser.add_argument("--target", choices=list(FETCHERS.keys()), default=None, help="only probe this one target platform (default: every other platform)")
        parser.add_argument("--limit", type=int, default=None, help="only check the first N invalid companies")
        parser.add_argument("--rps", type=float, default=None, help="override every target platform's rate limit")

    def handle(self, *args, **options):
        source_platform = options["platform"]
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT

        if options["target"]:
            if options["target"] == source_platform:
                raise CommandError("--target can't be the same as --platform")
            targets = {options["target"]: FETCHERS[options["target"]]}
        else:
            targets = {p: fetch for p, fetch in FETCHERS.items() if p != source_platform}
        limiters = {p: RateLimiter(options["rps"] or PLATFORM_RPS[p]) for p in targets}

        invalid = Company.objects.filter(ats_platform=source_platform, is_invalid=True).order_by("slug")
        if options["limit"]:
            invalid = invalid[: options["limit"]]
        total = invalid.count() if options["limit"] is None else len(invalid)
        self.stdout.write(f"Probing {total} invalid {source_platform} slugs against {', '.join(targets)}...")

        known = {(c.ats_platform, c.slug) for c in Company.objects.all().only("ats_platform", "slug")}

        checked = relocated = postings_total = 0
        for company in invalid:
            checked += 1
            for platform, fetch in targets.items():
                if (platform, company.slug) in known:
                    continue
                limiters[platform].wait()
                try:
                    postings = fetch(session, company.slug)
                except requests.RequestException:
                    continue
                if postings is None:
                    continue

                new_company, _ = Company.objects.get_or_create(
                    ats_platform=platform, slug=company.slug, defaults={"name": company.name},
                )
                known.add((platform, company.slug))
                relocated += 1
                self.stdout.write(f"  {company.slug!r}: {source_platform} (dead) -> {platform} ({len(postings)} posting(s))")

                for p in postings:
                    if not p["url"] or not p["title"]:
                        continue
                    avg_comp, lowest_comp = parse_comp_range(p["description"])
                    upsert_with_retry(
                        company=new_company, title=p["title"], url=p["url"], location=p["location"],
                        description=p["description"], avg_comp=avg_comp, lowest_comp=lowest_comp,
                        posted_at=p.get("posted_at"), workplace_type=p.get("workplace_type", ""),
                        locations=p.get("locations"),
                    )
                    postings_total += 1
                break  # found it, no need to check the remaining platforms for this slug

            if checked % 200 == 0:
                self.stdout.write(f"{checked}/{total} checked, {relocated} relocated so far, {postings_total} postings upserted")

        self.stdout.write(f"Done: {checked} checked, {relocated} relocated to a new platform, {postings_total} postings upserted")
