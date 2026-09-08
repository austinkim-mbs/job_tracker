from django.core.management.base import BaseCommand

from slug_filters import is_garbage_slug
from tracker.models import Company


class Command(BaseCommand):
    help = "Delete Company rows whose slug looks like a mis-captured artifact rather than a real ATS board slug."

    def add_arguments(self, parser):
        parser.add_argument("--platform", default=None)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        qs = Company.objects.all()
        if options["platform"]:
            qs = qs.filter(ats_platform=options["platform"])

        bad = [c for c in qs if is_garbage_slug(c.slug)]
        self.stdout.write(f"Found {len(bad)} garbage-looking companies out of {qs.count()}")
        for c in bad[:20]:
            self.stdout.write(f"  [{c.ats_platform}] {c.slug[:80]!r}")
        if len(bad) > 20:
            self.stdout.write(f"  ... and {len(bad) - 20} more")

        if options["dry_run"]:
            self.stdout.write("Dry run, nothing deleted.")
            return

        ids = [c.id for c in bad]
        deleted, _ = Company.objects.filter(id__in=ids).delete()
        self.stdout.write(f"Deleted {deleted} row(s).")
