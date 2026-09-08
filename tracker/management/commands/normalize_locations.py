import csv

from django.core.management.base import BaseCommand

from tracker.location_utils import classify_location
from tracker.models import Location


class Command(BaseCommand):
    help = (
        "One-time normalization pass over existing Location rows -- canonicalizes "
        "city names and fills in region, merging into an existing canonical row "
        "where one already exists (repointing postings, deleting the duplicate). "
        "Rows classify_location can't confidently place are left untouched and "
        "written to --confusing-file for manual review instead of being guessed."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
        parser.add_argument("--confusing-file", default="confusing_locations.csv")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        confusing = []
        merged = 0
        region_only_update = 0
        renamed = 0
        unchanged = 0

        # Snapshot ids up front -- merges delete rows as we go, so re-querying
        # mid-loop could skip or double-visit rows still in the queryset.
        location_ids = list(Location.objects.values_list("id", flat=True))
        self.stdout.write(f"{len(location_ids)} Location rows to review")

        for loc_id in location_ids:
            try:
                loc = Location.objects.get(id=loc_id)
            except Location.DoesNotExist:
                continue  # already merged away by an earlier iteration this run

            search_str = ", ".join(p for p in (loc.city, loc.state, loc.country) if p) or loc.city
            result = classify_location(search_str)

            if not result.is_confident:
                confusing.append({
                    "location_id": loc.id, "city": loc.city, "state": loc.state,
                    "country": loc.country, "postings_count": loc.postings.count(),
                })
                continue

            canonical_city = result.city or loc.city
            canonical_state = result.state or loc.state
            canonical_country = result.country or loc.country
            canonical_region = result.region

            if (canonical_city, canonical_state, canonical_country) == (loc.city, loc.state, loc.country):
                if loc.region != canonical_region:
                    region_only_update += 1
                    if not dry_run:
                        loc.region = canonical_region
                        loc.save(update_fields=["region"])
                else:
                    unchanged += 1
                continue

            target = Location.objects.filter(
                city=canonical_city, state=canonical_state, country=canonical_country,
            ).exclude(id=loc.id).first()

            if target:
                merged += 1
                if not dry_run:
                    if target.region != canonical_region:
                        target.region = canonical_region
                        target.save(update_fields=["region"])
                    for posting in loc.postings.all():
                        posting.locations.add(target)
                        posting.locations.remove(loc)
                    loc.delete()
            else:
                renamed += 1
                if not dry_run:
                    loc.city, loc.state, loc.country, loc.region = (
                        canonical_city, canonical_state, canonical_country, canonical_region,
                    )
                    loc.save(update_fields=["city", "state", "country", "region"])

        with open(options["confusing_file"], "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["location_id", "city", "state", "country", "postings_count"])
            writer.writeheader()
            writer.writerows(confusing)

        prefix = "[DRY RUN] would have " if dry_run else ""
        self.stdout.write(
            f"{prefix}renamed {renamed}, merged {merged} into existing rows, "
            f"updated region only on {region_only_update}, left {unchanged} unchanged"
        )
        self.stdout.write(f"{len(confusing)} rows need manual review -- written to {options['confusing_file']}")
