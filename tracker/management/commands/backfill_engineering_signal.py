"""
One-time, DB-only backfill for Company.last_us_engineering_posting_at.

fetch_postings.py now stamps this field live as postings are fetched, but
that logic was added after the last full Ashby/Greenhouse re-pull already
ran -- so the field is empty for essentially every company despite the DB
already holding fresh posting data. Re-running the full network fetch again
just to populate this would cost ~50 minutes for no new data; this instead
derives it from what's already stored; prefers each posting's normalized
Location.region where available (cheap, already computed by
normalize_locations), falling back to a live classify_location() call on the
raw location string for postings that never got m2m Location links.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand

from tracker.location_utils import classify_location, is_engineering_title
from tracker.models import Company, Posting


class Command(BaseCommand):
    help = "Backfill Company.last_us_engineering_posting_at from existing Posting data (no network calls)."

    def handle(self, *args, **options):
        qs = (
            Posting.objects.filter(title__iregex=r"engineer(ing)?")
            .select_related("company")
            .prefetch_related("locations")
            .only("title", "location", "posted_at", "first_seen", "company_id")
        )
        total = qs.count()
        self.stdout.write(f"{total} engineering-titled postings to scan")

        best_per_company = defaultdict(lambda: None)
        checked = 0
        us_hits = 0

        for p in qs.iterator(chunk_size=2000):
            checked += 1
            if not is_engineering_title(p.title):
                continue

            regions = [loc.region for loc in p.locations.all()]
            if regions:
                is_us = "us" in regions
            else:
                is_us = classify_location(p.location).region == "us"

            if not is_us:
                continue

            us_hits += 1
            # posted_at is the ATS's own "published" date when available;
            # first_seen (when we discovered it) is the fallback, not "now"
            # -- this is a historical backfill, not a live signal, so we
            # shouldn't stamp the current wall-clock time for old postings.
            seen_at = p.posted_at or p.first_seen
            current_best = best_per_company[p.company_id]
            if current_best is None or seen_at > current_best:
                best_per_company[p.company_id] = seen_at

            if checked % 20000 == 0:
                self.stdout.write(f"  {checked}/{total} scanned, {us_hits} US-engineering hits so far")

        self.stdout.write(f"scanned {checked} postings, {us_hits} were US + engineering")
        self.stdout.write(f"updating {len(best_per_company)} companies...")

        updated = 0
        for company_id, seen_at in best_per_company.items():
            Company.objects.filter(id=company_id).update(last_us_engineering_posting_at=seen_at)
            updated += 1

        self.stdout.write(f"done: {updated} companies now have last_us_engineering_posting_at set")
