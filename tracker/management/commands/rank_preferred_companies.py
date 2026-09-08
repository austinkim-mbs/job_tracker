"""
Bulk-assign Company.preference_rank by geography, scoped to companies with at
least one current engineering-titled posting:
  1 = California, New York, or Remote
  2 = elsewhere in the US or Europe
  3 = everywhere else
Companies with no qualifying engineering posting are left unranked (None).

Never overwrites a rank set by hand (preference_rank is meant to be sticky --
see Company.preference_rank docstring) unless --force is passed.
"""

from django.core.management.base import BaseCommand

from tracker.location_utils import classify_location, is_engineering_title
from tracker.models import Company, Posting

TIER1_STATES = {"CA", "NY"}


def tier_for(region: str, state: str) -> int:
    if region == "remote" or state in TIER1_STATES:
        return 1
    if region in ("us", "europe"):
        return 2
    return 3


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="recompute even companies with an existing rank")

    def handle(self, *args, **options):
        qs = (
            Posting.objects.filter(title__iregex=r"engineer(ing)?")
            .prefetch_related("locations")
            .only("title", "location", "company_id")
        )
        total = qs.count()
        self.stdout.write(f"{total} engineering-titled postings to scan")

        best_tier = {}
        checked = 0
        for p in qs.iterator(chunk_size=2000):
            checked += 1
            if not is_engineering_title(p.title):
                continue

            locs = list(p.locations.all())
            if locs:
                tier = min(tier_for(loc.region, loc.state) for loc in locs)
            else:
                result = classify_location(p.location)
                tier = tier_for(result.region, result.state)

            current = best_tier.get(p.company_id)
            if current is None or tier < current:
                best_tier[p.company_id] = tier

            if checked % 20000 == 0:
                self.stdout.write(f"  {checked}/{total} scanned")

        self.stdout.write(f"scanned {checked} postings, {len(best_tier)} companies qualify for a rank")

        force = options["force"]
        updated = 0
        skipped_existing = 0
        by_tier = {1: 0, 2: 0, 3: 0}
        for company_id, tier in best_tier.items():
            company = Company.objects.get(id=company_id)
            if company.preference_rank is not None and not force:
                skipped_existing += 1
                continue
            company.preference_rank = tier
            company.save(update_fields=["preference_rank"])
            updated += 1
            by_tier[tier] += 1

        self.stdout.write(
            f"done: {updated} companies ranked (tier 1: {by_tier[1]}, tier 2: {by_tier[2]}, tier 3: {by_tier[3]}), "
            f"{skipped_existing} left alone (already had a rank -- use --force to override)"
        )
