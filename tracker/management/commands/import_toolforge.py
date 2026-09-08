"""
Pull the company catalog from ToolForge (master-tools-hub.vercel.app) and
probe each company's guessed slug against the Ashby/Greenhouse/Lever public
job-board APIs. Unlike job_crawler.py (which discovers *confirmed* ATS URLs
via the Wayback CDX index), ToolForge only gives us a company name + domain
-- there's no guarantee any given company publishes through one of these
three platforms, or that our slug guess is right. So this is a probe, not a
discovery: most companies will 404 on all three and be silently skipped.

Any hit is upserted into Company + Posting exactly like fetch_postings.py
does, so the normal pipeline (scoring scripts, etc.) picks it up for free.

Run: python manage.py import_toolforge
     python manage.py import_toolforge --limit 200   # quick test
"""

import json
import re

import requests
from django.core.management.base import BaseCommand

from comp_parser import parse_comp_range
from job_crawler import RateLimiter
from slug_filters import is_garbage_slug
from tracker.management.commands.fetch_postings import FETCHERS, PLATFORM_RPS, USER_AGENT, upsert_with_retry
from tracker.models import Company

TOOLS_DATA_URL = "https://master-tools-hub.vercel.app/data/tools-data.js"
DATA_PREFIX = "window.__TOOLS_DATA__ = "

# Domains that need two labels stripped instead of one (e.g. "acme.co.uk" -> "acme").
MULTI_LABEL_TLDS = {"co.uk", "com.au", "co.in", "com.br", "co.jp", "co.nz"}


def domain_to_slug(domain: str) -> str | None:
    """Best-effort apex/company label from a domain. For "podcast.adobe.com"
    this must yield "adobe" (the registrable domain owner), not "podcast" (a
    product subdomain) -- so we take the label immediately before the TLD,
    not the leftmost one. Any subdomain prefix (www, app, a product name,
    etc.) is simply ignored."""
    domain = (domain or "").lower().strip()
    labels = domain.split(".")
    if len(labels) < 2:
        return None
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_LABEL_TLDS:
        labels = labels[:-2]
    else:
        labels = labels[:-1]
    if not labels:
        return None
    slug = re.sub(r"[^a-z0-9-]", "", labels[-1])
    return slug or None


def name_to_slug(name: str) -> str | None:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or None


def load_tools(session) -> list[dict]:
    resp = session.get(TOOLS_DATA_URL, timeout=60)
    resp.raise_for_status()
    text = resp.text.strip()
    if text.startswith(DATA_PREFIX):
        text = text[len(DATA_PREFIX):]
    text = text.rstrip(";").strip()
    return json.loads(text)["tools"]


class Command(BaseCommand):
    help = "Probe ToolForge's company catalog against Ashby/Greenhouse/Lever and upsert any real boards found."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="only probe the first N companies")
        parser.add_argument("--rps", type=float, default=None, help="override every platform's rate limit")
        parser.add_argument(
            "--platforms", default=None,
            help=f"comma-separated subset of platforms to probe (default: all of {', '.join(FETCHERS)})",
        )

    def handle(self, *args, **options):
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT

        platform_names = options["platforms"].split(",") if options["platforms"] else list(FETCHERS)
        fetchers = {p: FETCHERS[p] for p in platform_names}

        self.stdout.write("Fetching ToolForge company catalog...")
        tools = load_tools(session)
        if options["limit"]:
            tools = tools[: options["limit"]]
        self.stdout.write(f"{len(tools)} companies to probe against: {', '.join(fetchers)}")

        limiters = {p: RateLimiter(options["rps"] or PLATFORM_RPS[p]) for p in fetchers}
        known_slugs = {(c.ats_platform, c.slug) for c in Company.objects.all().only("ats_platform", "slug")}

        checked = found = postings_total = 0

        for tool in tools:
            checked += 1
            candidates = []
            for cand in (domain_to_slug(tool.get("domain", "")), name_to_slug(tool.get("name", ""))):
                if cand and not is_garbage_slug(cand) and cand not in candidates:
                    candidates.append(cand)

            for slug in candidates:
                hit = False
                for platform, fetch in fetchers.items():
                    if (platform, slug) in known_slugs:
                        hit = True
                        continue
                    limiters[platform].wait()
                    try:
                        postings = fetch(session, slug)
                    except requests.RequestException:
                        continue
                    if postings is None:
                        continue

                    hit = True
                    known_slugs.add((platform, slug))
                    company, _ = Company.objects.get_or_create(
                        ats_platform=platform, slug=slug, defaults={"name": tool.get("name", "")},
                    )
                    found += 1

                    for p in postings:
                        if not p["url"] or not p["title"]:
                            continue
                        avg_comp, lowest_comp = parse_comp_range(p["description"])
                        upsert_with_retry(
                            company=company, title=p["title"], url=p["url"], location=p["location"],
                            description=p["description"], avg_comp=avg_comp, lowest_comp=lowest_comp,
                            posted_at=p.get("posted_at"), workplace_type=p.get("workplace_type", ""),
                        )
                        postings_total += 1

                if hit:
                    break  # matched on this candidate; no need to also try the fallback name-derived slug

            if checked % 200 == 0:
                self.stdout.write(f"{checked}/{len(tools)} checked, {found} boards found, {postings_total} postings so far")

        self.stdout.write(f"Done: {checked} companies checked, {found} real ATS boards found, {postings_total} postings upserted")
