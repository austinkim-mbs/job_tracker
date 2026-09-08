"""
Ingest phase of the split pipeline -- the one serial DB writer, reading
JSONL dumps produced by dump_postings.py (one or more; run after however
many parallel/sharded dump_postings runs you did) and batch-writing via
Posting.objects.bulk_upsert() instead of one upsert() call per posting.

Also applies is_invalid and last_us_engineering_posting_at, which
dump_postings deliberately doesn't touch (it does zero DB writes).

The actual work lives in ingest_files() below so fetch_preferred_companies.py
can call it in-process (one writer, no subprocess) right after its own
parallel dump phase, instead of shelling out to this command a second time.

Run: python manage.py ingest_postings dump_ashby.jsonl dump_greenhouse.jsonl
"""

import json
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from comp_parser import parse_comp_range
from tracker.location_utils import classify_location, classify_location_dict, is_engineering_title
from tracker.models import Company, Posting

BATCH_SIZE = 1000


def _json_object_hook(d):
    if "__datetime__" in d:
        return datetime.fromisoformat(d["__datetime__"])
    return d


class _NullWriter:
    def write(self, msg):
        pass

    def flush(self):
        pass


def ingest_files(files: list[str], batch_size: int = BATCH_SIZE, stdout=None) -> dict:
    """Ingest one or more dump_postings.py JSONL files in a single serial,
    batched write pass. Returns a summary dict; also written to `stdout`
    (a Django-style writer with .write(), or nothing if omitted) as it goes.
    """
    stdout = stdout or _NullWriter()
    company_cache = {}

    def get_company(company_id):
        if company_id not in company_cache:
            company_cache[company_id] = Company.objects.get(id=company_id)
        return company_cache[company_id]

    total_postings = 0
    newly_invalid = 0
    empty_boards = 0
    errors = 0
    us_engineering_seen_at = {}  # company_id -> best timestamp
    # Shared across every bulk_upsert() call in this run (not per-batch)
    # so a common city like "San Francisco" gets resolved once for the
    # whole ingest instead of once per 1000-row batch.
    location_cache = {}

    batch = []

    def flush():
        nonlocal total_postings, batch
        if batch:
            Posting.objects.bulk_upsert(batch, location_cache=location_cache)
            total_postings += len(batch)
            batch = []

    for path in files:
        stdout.write(f"ingesting {path}...")
        line_count = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                line_count += 1
                record = json.loads(line, object_hook=_json_object_hook)

                try:
                    company = get_company(record["company_id"])
                except Company.DoesNotExist:
                    continue

                if record["status"] == "error":
                    errors += 1
                    continue
                if record["status"] == "invalid":
                    if not company.is_invalid:
                        company.is_invalid = True
                        company.save(update_fields=["is_invalid"])
                        newly_invalid += 1
                    continue
                if record["status"] == "empty":
                    empty_boards += 1
                    continue

                for p in record["postings"]:
                    if not p.get("url") or not p.get("title"):
                        continue
                    avg_comp, lowest_comp = parse_comp_range(p.get("description", ""))
                    raw_locations = p.get("locations")
                    enriched = (
                        [classify_location_dict(loc) for loc in raw_locations]
                        if raw_locations is not None else None
                    )
                    batch.append({
                        "company": company, "title": p["title"], "url": p["url"],
                        "location": p.get("location", ""), "description": p.get("description", ""),
                        "avg_comp": avg_comp, "lowest_comp": lowest_comp,
                        "posted_at": p.get("posted_at"), "workplace_type": p.get("workplace_type", ""),
                        "locations": enriched,
                    })

                    if is_engineering_title(p["title"]):
                        regions = (
                            [loc["region"] for loc in enriched] if enriched
                            else [classify_location(p.get("location", "")).region]
                        )
                        if "us" in regions:
                            seen_at = p.get("posted_at") or datetime.now(timezone.utc)
                            current = us_engineering_seen_at.get(company.id)
                            if current is None or seen_at > current:
                                us_engineering_seen_at[company.id] = seen_at

                    if len(batch) >= batch_size:
                        flush()

                if line_count % 5000 == 0:
                    stdout.write(f"  {line_count} companies processed, {total_postings + len(batch)} postings so far")

        stdout.write(f"  {path}: {line_count} companies")

    flush()

    stdout.write(f"updating last_us_engineering_posting_at for {len(us_engineering_seen_at)} companies...")
    for company_id, seen_at in us_engineering_seen_at.items():
        company = get_company(company_id)
        if company.last_us_engineering_posting_at is None or seen_at > company.last_us_engineering_posting_at:
            Company.objects.filter(id=company_id).update(last_us_engineering_posting_at=seen_at)

    summary = {
        "total_postings": total_postings, "empty_boards": empty_boards,
        "newly_invalid": newly_invalid, "errors": errors,
    }
    stdout.write(
        f"done: {total_postings} postings upserted, {empty_boards} empty boards, "
        f"{newly_invalid} newly invalid, {errors} request errors"
    )
    return summary


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("files", nargs="+", help="JSONL files produced by dump_postings.py")
        parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)

    def handle(self, *args, **options):
        ingest_files(options["files"], batch_size=options["batch_size"], stdout=self.stdout)
