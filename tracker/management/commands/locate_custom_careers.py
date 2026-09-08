"""
Last-resort fallback for ToolForge companies that don't have a board on any
of the ATS platforms this project can already fetch from (Ashby, Greenhouse,
Lever, SmartRecruiters, Workable). For those, query the Wayback CDX index
scoped to the company's OWN domain and look for any captured URL that looks
like a careers/jobs page.

This does NOT scrape those pages -- every company's custom career page is
laid out differently, so there's no single reusable parser for "whatever
HTML is on domain.com/careers". This just locates the URL and records it for
manual follow-up (or a future targeted scraper, if a specific company turns
out to be worth the one-off effort).

Writes to --out incrementally (one line per domain, flushed immediately) and
skips domains already present in an existing --out file, so a killed/
restarted run doesn't lose progress or redo work.

Run: python manage.py locate_custom_careers
     python manage.py locate_custom_careers --limit 100 --out preview.csv
"""

import csv
import os
import sys
import time

import requests
from django.core.management.base import BaseCommand

from job_crawler import RateLimiter
from tracker.management.commands.import_toolforge import domain_to_slug, load_tools, name_to_slug
from tracker.models import Company

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
USER_AGENT = "job-tracker-crawler/0.1 (personal job search; contact: kdaustin94@gmail.com)"
# Matched server-side via CDX's own regex filter (not pulled-then-filtered
# locally) -- pulling first, then filtering client-side doesn't work because
# CDX returns rows in urlkey (roughly alphabetical) order by default, and for
# any site with a lot of captures the first few hundred rows are almost
# never anywhere near a "/careers" path.
CAREER_PATH_FILTER = r"original:.*://[^/]+/(careers?|jobs?|join-us|join-the-team|hiring|work-with-us)([/?].*)?$"
FIELDNAMES = ["name", "domain", "tool_url", "status", "careers_url"]


def find_careers_url(session, domain, limiter, timeout=10, max_retries=2):
    """Query CDX for a careers/jobs-shaped URL captured under this domain.
    Returns the URL if found, "" if the domain was checked but nothing
    matched, or None if the query itself failed (e.g. timed out against an
    unusually large site) -- the caller distinguishes 'looked, found
    nothing' from 'couldn't check'.

    timeout is intentionally short (not the usual 20-30s elsewhere in this
    project): a first pass at this command left it at 25s and a meaningful
    fraction of ~1400 domains hit that ceiling, turning what should have
    been a ~15 minute job into multiple hours. Missing a slow site's real
    careers page here is a cheap loss -- it just falls back to
    "cdx_error" for manual follow-up instead of blocking everything behind it.
    """
    params = {
        "url": domain,
        "matchType": "domain",
        "output": "json",
        "filter": ["statuscode:200", "mimetype:text/html", CAREER_PATH_FILTER],
        "collapse": "urlkey",
        "limit": "5",
    }
    backoff = 1.0
    for attempt in range(max_retries):
        limiter.wait()
        try:
            resp = session.get(CDX_ENDPOINT, params=params, timeout=timeout)
            if resp.status_code in (429, 503) and attempt < max_retries - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            rows = resp.json() if resp.text.strip() else []
            break
        except requests.exceptions.Timeout:
            return None  # a slow query will stay slow -- retrying just wastes time
        except requests.RequestException:
            if attempt < max_retries - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            return None
    else:
        return None

    if len(rows) < 2:
        return ""
    candidates = sorted((row[2] for row in rows[1:] if len(row) > 2), key=len)
    return candidates[0] if candidates else ""


class Command(BaseCommand):
    help = "For ToolForge companies with no known ATS board, look up their own domain in Wayback CDX for a careers/jobs page URL."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="only check the first N unmatched companies")
        parser.add_argument("--rps", type=float, default=1.5, help="CDX request rate (default: 1.5/s)")
        parser.add_argument("--timeout", type=float, default=10, help="per-request timeout in seconds (default: 10)")
        parser.add_argument("--out", default="unmatched_companies_careers.csv")

    def handle(self, *args, **options):
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT

        self.stdout.write("Fetching ToolForge company catalog...")
        sys.stdout.flush()
        tools = load_tools(session)

        known_slugs = {c.slug for c in Company.objects.all().only("slug")}

        unmatched = []
        for tool in tools:
            candidates = {domain_to_slug(tool.get("domain", "")), name_to_slug(tool.get("name", ""))}
            candidates.discard(None)
            if not candidates & known_slugs:
                unmatched.append(tool)

        self.stdout.write(f"{len(unmatched)} of {len(tools)} ToolForge companies have no known ATS board")
        if options["limit"]:
            unmatched = unmatched[: options["limit"]]

        # Several ToolForge "tools" are sub-products of the same company on
        # the same domain (e.g. "Ahrefs Backlink Checker" / "Ahrefs Webmaster
        # Tools" both on ahrefs.com) -- query each distinct domain once and
        # fan the result back out to every tool that shares it.
        domains = sorted({t.get("domain", "") for t in unmatched if t.get("domain")})

        out_path = options["out"]
        already_done = {}
        if os.path.exists(out_path):
            with open(out_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    already_done[row["domain"]] = row["careers_url"] if row["status"] == "custom_career_page_found" else ""
            self.stdout.write(f"Resuming: {len(already_done)} domains already checked in {out_path}")

        remaining = [d for d in domains if d not in already_done]
        self.stdout.write(
            f"{len(domains)} distinct domains ({len(unmatched)} companies); "
            f"{len(remaining)} left to check against Wayback CDX..."
        )
        sys.stdout.flush()

        write_header = not os.path.exists(out_path)
        out_file = open(out_path, "a", newline="", encoding="utf-8")
        writer = csv.DictWriter(out_file, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
            out_file.flush()

        tools_by_domain = {}
        for tool in unmatched:
            tools_by_domain.setdefault(tool.get("domain", ""), []).append(tool)

        limiter = RateLimiter(options["rps"])
        found = checked = 0
        start = time.monotonic()

        for domain in remaining:
            checked += 1
            url = find_careers_url(session, domain, limiter, timeout=options["timeout"])
            if url is None:
                status = "cdx_error"
            elif url == "":
                status = "no_career_page_found"
            else:
                status = "custom_career_page_found"
                found += 1

            for tool in tools_by_domain.get(domain, []):
                writer.writerow({
                    "name": tool.get("name", ""),
                    "domain": domain,
                    "tool_url": tool.get("url", ""),
                    "status": status,
                    "careers_url": url or "",
                })
            out_file.flush()

            if checked % 20 == 0:
                elapsed = time.monotonic() - start
                rate = checked / elapsed if elapsed else 0
                eta_min = (len(remaining) - checked) / rate / 60 if rate else float("inf")
                self.stdout.write(
                    f"{checked}/{len(remaining)} domains checked, {found} custom careers pages found so far "
                    f"({rate:.2f}/s, ~{eta_min:.0f} min remaining)"
                )
                sys.stdout.flush()

        out_file.close()
        self.stdout.write(
            f"Done: {len(domains)} domains total ({len(unmatched)} companies), "
            f"{found} custom careers pages located this run, wrote {out_path}"
        )
