"""
Re-derive the Location m2m for existing Greenhouse/Lever postings using the
improved split_locations() parser, from data already in the database --
no network calls. Posting.location (the raw ATS string) was always stored
in full, even before split_locations() existed, so a posting that lists e.g.
"London & San Francisco" can be correctly split into two Location rows now
without re-fetching anything.

Caches Location get_or_create results in-process (many postings share the
exact same raw location string, e.g. "Remote" or "San Francisco, CA") to
avoid redundant queries across ~126k rows.

Run: python manage.py backfill_locations
     python manage.py backfill_locations --platform lever
"""

from django.core.management.base import BaseCommand

from tracker.management.commands.fetch_postings import split_locations
from tracker.models import Location, Posting


class Command(BaseCommand):
    help = "Re-derive Location links for existing Greenhouse/Lever postings from their already-stored raw location string."

    def add_arguments(self, parser):
        parser.add_argument("--platform", choices=["greenhouse", "lever"], default=None, help="default: both")

    def handle(self, *args, **options):
        platforms = [options["platform"]] if options["platform"] else ["greenhouse", "lever"]
        location_cache: dict[tuple[str, str, str], Location] = {}

        def resolve(city: str, state: str, country: str) -> Location:
            key = (city, state, country)
            if key not in location_cache:
                location_cache[key] = Location.objects.get_or_create(city=city, state=state, country=country)[0]
            return location_cache[key]

        qs = Posting.objects.filter(company__ats_platform__in=platforms).only("id", "location")
        total = qs.count()
        self.stdout.write(f"Backfilling {total} postings across {', '.join(platforms)}...")

        processed = changed = 0
        for posting in qs.iterator(chunk_size=2000):
            processed += 1
            parts = split_locations(posting.location)
            new_locations = [resolve(p["city"], p["state"], p["country"]) for p in parts]
            current_ids = set(posting.locations.values_list("id", flat=True))
            new_ids = {loc.id for loc in new_locations}
            if current_ids != new_ids:
                posting.locations.set(new_locations)
                changed += 1

            if processed % 10000 == 0:
                self.stdout.write(f"  {processed}/{total} processed, {changed} updated so far")

        self.stdout.write(f"Done: {processed} postings processed, {changed} had their locations updated")
