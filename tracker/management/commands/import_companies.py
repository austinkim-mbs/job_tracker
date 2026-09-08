import json
import re

from django.core.management.base import BaseCommand

from config import BASE_DIR
from slug_filters import is_garbage_slug
from tracker.models import Company

STATE_PATH = f"{BASE_DIR}/crawler_state.json"

# job_crawler.py tracks some targets under a different name than the ATS
# platform they actually belong to (separate crawler_state.json bookkeeping
# for a second discovery pattern against the same backend) -- map those back
# to the real platform so fetch_postings.py's FETCHERS dict recognizes them.
PLATFORM_ALIASES = {
    "greenhouse-jobboards": "greenhouse",
}


def derive_name(slug: str) -> str:
    """Best-effort display name from a raw ATS slug, e.g. "acme-corp" -> "Acme Corp"."""
    words = re.split(r"[-_]+", slug.strip())
    return " ".join(w.capitalize() for w in words if w)


class Command(BaseCommand):
    help = "Import companies discovered by job_crawler.py (crawler_state.json) into the Company table."

    def handle(self, *args, **options):
        with open(STATE_PATH) as f:
            state = json.load(f)

        for state_key, data in state.items():
            platform = PLATFORM_ALIASES.get(state_key, state_key)
            slugs = data.get("slugs", [])
            created = skipped_garbage = 0
            for slug in slugs:
                slug = slug.strip()
                # is_garbage_slug() already runs at crawl time in
                # job_crawler.py, but crawler_state.json is a running log --
                # it isn't retroactively purged when slug_filters.py gains a
                # new pattern (or when a garbage Company row gets deleted via
                # clean_companies.py). Re-checking here means re-running this
                # command can't resurrect rows clean_companies.py already
                # removed.
                if not slug or is_garbage_slug(slug):
                    skipped_garbage += 1
                    continue
                _, was_created = Company.objects.get_or_create(
                    ats_platform=platform,
                    slug=slug,
                    defaults={"name": derive_name(slug)},
                )
                if was_created:
                    created += 1

            label = f"{state_key} -> {platform}" if state_key != platform else platform
            self.stdout.write(
                f"[{label}] {created} new / {len(slugs)} total companies imported "
                f"({skipped_garbage} garbage-looking slugs skipped)"
            )
