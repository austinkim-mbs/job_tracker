"""
Fetch phase of the split ingestion pipeline -- pure network I/O, zero DB
writes (not even is_invalid). Safe to run several of these concurrently
(different platforms, or sharded --company-ids-file slices of the same
platform) since there's no writer to contend over during this phase; pair
with ingest_postings.py, the single serial writer that reads the JSONL this
produces.

Run: python manage.py dump_postings --platform ashby --out dump_ashby.jsonl
"""

import json
import time
from datetime import datetime, timezone

import requests
from django.core.management.base import BaseCommand

from tracker.management.commands.fetch_postings import FETCHERS, PLATFORM_RPS, USER_AGENT
from job_crawler import RateLimiter
from tracker.models import Company


def _json_default(obj):
    if isinstance(obj, datetime):
        return {"__datetime__": obj.isoformat()}
    raise TypeError(f"not JSON serializable: {type(obj)}")


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--platform", required=True, choices=list(FETCHERS.keys()))
        parser.add_argument("--rps", type=float, default=None)
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--company-ids-file", default=None,
                             help="newline-separated Company ids -- restricts to just those (for sharding)")
        parser.add_argument("--out", default=None, help="output JSONL path (default: dump_<platform>_<ts>.jsonl)")

    def handle(self, *args, **options):
        platform = options["platform"]
        fetch = FETCHERS[platform]
        rps = options["rps"] or PLATFORM_RPS[platform]
        limiter = RateLimiter(rps)

        companies = Company.objects.filter(ats_platform=platform, is_invalid=False).order_by("slug")
        if options["company_ids_file"]:
            with open(options["company_ids_file"]) as f:
                ids = [int(line) for line in f if line.strip()]
            companies = companies.filter(id__in=ids)
        if options["limit"]:
            companies = companies[: options["limit"]]
        companies = list(companies.values("id", "slug", "ats_platform"))

        out_path = options["out"] or f"dump_{platform}_{int(time.time())}.jsonl"
        self.stdout.write(f"[{platform}] {len(companies)} companies, rate limit {rps} req/s -> {out_path}")

        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT

        start_time = time.monotonic()
        last_progress = start_time
        total_postings = 0

        with open(out_path, "w", encoding="utf-8") as out:
            for i, company in enumerate(companies, 1):
                limiter.wait()
                try:
                    postings = fetch(session, company["slug"])
                    status = "ok" if postings else ("empty" if postings == [] else "invalid")
                    record = {"company_id": company["id"], "status": status, "postings": postings or []}
                except requests.RequestException as e:
                    record = {"company_id": company["id"], "status": "error", "error": str(e), "postings": []}

                out.write(json.dumps(record, default=_json_default) + "\n")
                total_postings += len(record["postings"])

                now = time.monotonic()
                if now - last_progress >= 30:
                    last_progress = now
                    elapsed = now - start_time
                    rate = i / elapsed * 60 if elapsed else 0
                    eta = (len(companies) - i) / rate if rate else 0
                    self.stdout.write(
                        f"[{platform}] {i}/{len(companies)} ({elapsed/60:.1f}m elapsed, {rate:.0f}/min, "
                        f"~{eta:.0f}m left), {total_postings} postings dumped so far"
                    )
                    self.stdout.flush()

        self.stdout.write(f"[{platform}] done: {len(companies)} companies, {total_postings} postings -> {out_path}")
