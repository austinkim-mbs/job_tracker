"""
Discovery source for VC-portfolio job boards built on Consider
(boards.consider.com) -- e.g. jobs.lsvp.com (Lightspeed Venture Partners).
These aggregate postings across an entire portfolio under one board, not
one company per board like every other target in this pipeline, so this
can't be a normal FETCHERS entry.

Mirrors job_crawler.py + import_companies.py's split rather than
onboard_company.py's do-everything-at-once approach: this command only
paginates the board's public search-jobs API and creates a Company row for
every job whose applyUrl points at one of our already-supported ATS
platforms (Ashby/Greenhouse/Lever/etc -- same patterns onboard_company.py
uses) -- it does NOT fetch postings itself. Run fetch_postings.py (or a
dump_postings.py + ingest_postings.py pass scoped to the newly-created
company ids) afterward to actually pull their postings, same as any other
newly-imported company. A job whose applyUrl is on an ATS we don't support
(Oracle HCM, Workday, a bespoke company site, ...) is counted but skipped,
since we couldn't fetch that company's postings ourselves regardless of how
we found them.

The search API requires a CSRF token minted per-session: GET the board page
once for a session cookie and the token embedded in its
`window.serverInitialData` blob, then send that token back as the
`x-csrf-token` header on every search-jobs POST using the same session.
Pagination is an opaque `sequence` cursor echoed back in each response's
`meta` and passed into the next request -- there's no page number or offset.
Termination is belt-and-suspenders: stop once `meta.total` (present on the
first page) is reached, OR the cursor stops advancing, OR jobs come back
empty -- a live run scanned well past a first-page `total` of ~9k before
being killed, so `total` alone isn't trusted to always be present/accurate.

--host/--board-id default to Lightspeed but are just as arguments in case
other VC firms turn out to run the same Consider platform under their own
domain.

Run: python manage.py discover_consider_board
     python manage.py discover_consider_board --host jobs.othervc.com --board-id othervc
"""

import re

import requests
from django.core.management.base import BaseCommand, CommandError

from job_crawler import RateLimiter
from tracker.management.commands.fetch_postings import USER_AGENT
from tracker.management.commands.onboard_company import parse_direct_board_url
from tracker.models import Company

CSRF_RE = re.compile(r'"csrfToken":"([^"]+)"')
PAGE_SIZE = 100
MAX_PAGES = 500  # hard safety cap (== 50k jobs at PAGE_SIZE=100) in case total/sequence both misbehave
# A VC portfolio's distinct-company count is small and finite even when its total
# job/posting count (what `meta.total` seems to actually measure, unreliably at
# that) is not -- a live run against Lightspeed found every one of its ~233
# distinct companies within the first ~15 pages, then cycled through 480 more
# pages of pure repeats before hitting MAX_PAGES. Bail out once nothing new
# (neither a new company nor a newly-seen unsupported one) has turned up for
# this many consecutive pages, rather than always paying for the full crawl.
STALE_PAGE_LIMIT = 20


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--host", default="jobs.lsvp.com", help="Consider-hosted board domain")
        parser.add_argument("--board-id", default="lightspeed", help="Consider board id (usually the VC firm's slug)")
        parser.add_argument("--location", default="United States", help="location filter, as it appears in the board's own UI")
        parser.add_argument("--rps", type=float, default=1.0, help="max requests/sec against the board's own search API")

    def handle(self, *args, **options):
        host = options["host"]
        board_id = options["board_id"]
        location = options["location"]
        board_limiter = RateLimiter(options["rps"])

        consider = requests.Session()
        consider.headers["User-Agent"] = USER_AGENT

        landing_url = f"https://{host}/jobs?locations={location.replace(' ', '+')}"
        resp = consider.get(landing_url, timeout=20)
        match = CSRF_RE.search(resp.text)
        if not match:
            raise CommandError(f"Couldn't find a csrfToken on {landing_url} -- board layout may have changed")
        consider.headers["x-csrf-token"] = match.group(1)

        # Loaded once, up front -- every (platform, slug) we already know about,
        # so the tens of thousands of jobs belonging to already-onboarded
        # companies (the common case -- a VC's portfolio heavily overlaps what
        # a company this age already has) skip with a plain set lookup instead
        # of a DB round-trip each. New ones found during the run are added to
        # this same set so a company posting multiple jobs in the same run
        # (or across pages) only gets a single get_or_create call.
        known = set(Company.objects.values_list("ats_platform", "slug"))
        self.stdout.write(f"{len(known)} companies already known before this run")

        search_url = f"https://{host}/api-boards/search-jobs"
        unsupported_companies = set()
        total_jobs = 0
        newly_created = 0
        expected_total = None
        sequence = None
        page = 0
        stale_pages = 0

        while True:
            page += 1
            if page > MAX_PAGES:
                self.stderr.write(f"hit the {MAX_PAGES}-page safety cap -- stopping early (see docstring)")
                break

            board_limiter.wait()
            meta = {"size": PAGE_SIZE}
            if sequence:
                meta["sequence"] = sequence
            body = {
                "meta": meta,
                "board": {"id": board_id, "isParent": True},
                "query": {"locations": [location], "promoteFeatured": True},
            }
            resp = consider.post(search_url, json=body, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            jobs = data.get("jobs", [])
            if not jobs:
                break
            total_jobs += len(jobs)
            if expected_total is None:
                expected_total = data.get("meta", {}).get("total")

            page_found_something_new = False
            for job in jobs:
                apply_url = job.get("applyUrl") or job.get("url") or ""
                direct = parse_direct_board_url(apply_url)
                if not direct:
                    unknown_company = job.get("companyDomain") or job.get("companyName") or "unknown"
                    if unknown_company not in unsupported_companies:
                        unsupported_companies.add(unknown_company)
                        page_found_something_new = True
                    continue
                if direct in known:
                    continue  # already onboarded -- the normal sweep/preferred pull already covers it
                known.add(direct)
                page_found_something_new = True

                platform, slug = direct
                Company.objects.create(ats_platform=platform, slug=slug, name=job.get("companyName") or slug)
                newly_created += 1
                self.stdout.write(f"  new company: {job.get('companyName') or slug} [{platform}/{slug}]")

            stale_pages = 0 if page_found_something_new else stale_pages + 1

            new_sequence = data.get("meta", {}).get("sequence")
            if new_sequence == sequence:
                self.stderr.write("pagination cursor stopped advancing -- stopping (board may have changed shape)")
                break
            sequence = new_sequence

            self.stdout.write(
                f"scanned {total_jobs}{f'/{expected_total}' if expected_total else ''} jobs so far, "
                f"{newly_created} new companies, {len(unsupported_companies)} on an unsupported ATS"
            )
            self.stdout.flush()
            if expected_total and total_jobs >= expected_total:
                break
            if stale_pages >= STALE_PAGE_LIMIT:
                self.stdout.write(f"nothing new in {STALE_PAGE_LIMIT} consecutive pages -- portfolio looks exhausted, stopping")
                break

        self.stdout.write(
            f"done: {total_jobs} jobs scanned, {newly_created} new companies created "
            f"(run fetch_postings.py or a scoped dump_postings.py/ingest_postings.py pass to pull their postings), "
            f"{len(unsupported_companies)} companies seen on an unsupported ATS (skipped, can't fetch those ourselves)"
        )
