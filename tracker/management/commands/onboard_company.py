"""
Onboard a single company from a URL -- either a direct ATS board link
(jobs.ashbyhq.com/<slug>, jobs.lever.co/<slug>, boards.greenhouse.io/<slug> or
job-boards.greenhouse.io/<slug>, a Greenhouse embed URL,
jobs.smartrecruiters.com/<slug>, apply.workable.com/<slug>,
jobs.gem.com/<slug>, <slug>.breezy.hr, or ats.rippling.com/<slug>) or just the
company's own website/careers page.

A direct board link is parsed straight off the URL. Anything else is treated
as a company site: the domain is turned into a slug guess (same heuristic as
import_toolforge.py) and probed against every platform in FETCHERS until one
comes back real. Domain-guessing is best-effort -- if the company's ATS slug
doesn't match its domain, it will fail to find a board even though one
exists. Pass the board URL directly in that case.

On a match, upserts the Company and all of its current postings in one shot
(same upsert path as fetch_postings.py), so the normal pipeline (scoring
scripts, etc.) sees it for free.

Run: python manage.py onboard_company https://jobs.ashbyhq.com/acme
     python manage.py onboard_company https://www.acme.com/careers
     python manage.py onboard_company acme.com --name "Acme Corp"
"""

import re
import urllib.parse

import requests
from django.core.management.base import BaseCommand, CommandError

from comp_parser import parse_comp_range
from job_crawler import RateLimiter
from slug_filters import is_garbage_slug
from tracker.management.commands.fetch_postings import FETCHERS, PLATFORM_RPS, USER_AGENT, upsert_with_retry
from tracker.management.commands.import_companies import derive_name
from tracker.management.commands.import_toolforge import domain_to_slug
from tracker.models import Company

# (platform, regex) -- checked in order against the raw URL. First match
# wins. Searched (not matched-from-start) so a slug embedded in a query
# string (Greenhouse's embed widget) is still found.
DIRECT_BOARD_PATTERNS = [
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.IGNORECASE)),
    ("lever", re.compile(r"jobs\.lever\.co/([^/?#]+)", re.IGNORECASE)),
    ("greenhouse", re.compile(r"[?&]for=([^&#]+)", re.IGNORECASE)),  # embed widget URL
    ("greenhouse", re.compile(r"boards\.greenhouse\.io/([^/?#]+)", re.IGNORECASE)),
    # Greenhouse's default public job-board domain since their ~2023 rebrand
    # (see job_crawler.py's "greenhouse-jobboards" target) -- boards.greenhouse.io
    # above still exists for older links, but most current applyUrls use this one.
    ("greenhouse", re.compile(r"job-boards\.greenhouse\.io/([^/?#]+)", re.IGNORECASE)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)", re.IGNORECASE)),
    ("workable", re.compile(r"apply\.workable\.com/([^/?#]+)", re.IGNORECASE)),
    ("gem", re.compile(r"jobs\.gem\.com/([^/?#]+)", re.IGNORECASE)),
    # Breezy's slug is a subdomain, not a path segment -- captured from the host.
    ("breezy", re.compile(r"([^./]+)\.breezy\.hr", re.IGNORECASE)),
    ("rippling", re.compile(r"ats\.rippling\.com/([^/?#]+)", re.IGNORECASE)),
]


def parse_direct_board_url(url: str) -> tuple[str, str] | None:
    """If `url` is already a link to a known ATS board, extract (platform,
    slug) straight from it. Returns None if it doesn't look like one."""
    for platform, pattern in DIRECT_BOARD_PATTERNS:
        match = pattern.search(url)
        if match:
            return platform, urllib.parse.unquote(match.group(1)).strip().lower()
    return None


def normalize_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = f"https://{url}"
    return url


class Command(BaseCommand):
    help = "Onboard a single company from a URL: detect its ATS board, create the Company row, and pull its current postings."

    def add_arguments(self, parser):
        parser.add_argument("url", help="Company website, careers page, or direct ATS board URL")
        parser.add_argument("--name", default=None, help="Display name to use (default: derived from the slug)")
        parser.add_argument("--rps", type=float, default=None, help="override the per-platform rate limit while probing")

    def handle(self, *args, **options):
        url = normalize_url(options["url"])
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT

        direct = parse_direct_board_url(url)
        if direct:
            platform, slug = direct
            self.stdout.write(f"Recognized direct {platform} board link -- slug={slug!r}")
            postings = self._fetch(session, platform, slug)
            if postings is None:
                raise CommandError(f"{platform}/{slug} doesn't look like a real board (API rejected it)")
        else:
            domain = urllib.parse.urlsplit(url).netloc
            slug = domain_to_slug(domain)
            if not slug or is_garbage_slug(slug):
                raise CommandError(f"Couldn't derive a usable slug from {url!r} -- pass the ATS board URL directly instead")
            self.stdout.write(f"No direct ATS link recognized -- guessing slug={slug!r} from domain={domain!r}, probing...")
            platform = postings = None
            for candidate_platform, fetch in FETCHERS.items():
                rps = options["rps"] or PLATFORM_RPS[candidate_platform]
                RateLimiter(rps).wait()
                try:
                    result = fetch(session, slug)
                except requests.RequestException as e:
                    self.stdout.write(f"  [{candidate_platform}] request failed ({e}), skipping")
                    continue
                if result is not None:
                    platform, postings = candidate_platform, result
                    self.stdout.write(f"  [{candidate_platform}] match -- {len(result)} posting(s)")
                    break
                self.stdout.write(f"  [{candidate_platform}] no board at that slug")

            if platform is None:
                raise CommandError(
                    f"No board found for slug {slug!r} on any of {', '.join(FETCHERS)}. "
                    "If you know which ATS this company uses, pass its board URL directly instead."
                )

        name = options["name"] or derive_name(slug)
        company, created = Company.objects.get_or_create(
            ats_platform=platform, slug=slug, defaults={"name": name},
        )
        if not created and options["name"] and company.name != name:
            company.name = name
            company.save(update_fields=["name"])
        self.stdout.write(f"{'Created' if created else 'Already known'}: {company}")

        upserted = 0
        for p in postings:
            if not p["url"] or not p["title"]:
                continue
            avg_comp, lowest_comp = parse_comp_range(p["description"])
            upsert_with_retry(
                company=company, title=p["title"], url=p["url"], location=p["location"],
                description=p["description"], avg_comp=avg_comp, lowest_comp=lowest_comp,
                posted_at=p.get("posted_at"), workplace_type=p.get("workplace_type", ""),
                locations=p.get("locations"),
            )
            upserted += 1

        self.stdout.write(f"Done: {company.name} [{platform}/{slug}] -- {upserted} posting(s) upserted")

    def _fetch(self, session, platform, slug):
        RateLimiter(PLATFORM_RPS[platform]).wait()
        return FETCHERS[platform](session, slug)
