"""
Tighter-cadence refresh for the watchlist -- every Company with a
preference_rank set (any tier), across every ATS platform, refreshed in
rank order (tier 1 first) so an interrupted run still covers the highest-
priority companies. Meant to be run daily/frequently; the full sweep in
fetch_postings.py covers everyone else (including brand-new companies with
no rank yet) on a slower cadence -- see rank_preferred_companies.py for how
preference_rank gets assigned.

Uses the same split pipeline as the full sweep (dump_postings.py /
ingest_postings.py): one pure-fetch subprocess per ATS platform (zero DB
writes, so safe to run fully in parallel -- each hits a different external
API) followed by a single serial, batched ingest pass. SQLite only tolerates
one writer at a time, and an earlier version of this command wrote directly
to the DB from one process per platform -- that caused recurring "database
is locked" crashes on the heaviest platforms (ashby/greenhouse) whenever all
platforms ran concurrently. This never has more than one writer, so the
crash can't happen regardless of how many platforms run at once.

Run: python manage.py fetch_preferred_companies
     python manage.py fetch_preferred_companies --tier 1
     python manage.py fetch_preferred_companies --platform ashby
"""

import os
import subprocess
import sys
import tempfile

from django.core.management.base import BaseCommand

from tracker.management.commands.fetch_postings import FETCHERS
from tracker.management.commands.ingest_postings import ingest_files
from tracker.models import Company


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument(
            "--tier", type=int, choices=[1, 2, 3], default=None,
            help="only refresh this preference tier instead of the whole watchlist",
        )
        parser.add_argument(
            "--platform", choices=list(FETCHERS.keys()), default=None,
            help="only refresh this ATS platform instead of the whole watchlist",
        )
        parser.add_argument("--rps", type=float, default=None, help="override every platform's default rate limit")

    def handle(self, *args, **options):
        companies = Company.objects.filter(preference_rank__isnull=False, is_invalid=False)
        if options["tier"] is not None:
            companies = companies.filter(preference_rank=options["tier"])

        platforms = [options["platform"]] if options["platform"] else list(FETCHERS.keys())

        with tempfile.TemporaryDirectory(prefix="fetch_preferred_") as tmpdir:
            procs = []
            dump_paths = {}
            for platform in platforms:
                ids = list(
                    companies.filter(ats_platform=platform)
                    .order_by("preference_rank", "slug")
                    .values_list("id", flat=True)
                )
                if not ids:
                    continue

                ids_path = os.path.join(tmpdir, f"{platform}_ids.txt")
                with open(ids_path, "w") as f:
                    f.write("\n".join(str(i) for i in ids))

                dump_path = os.path.join(tmpdir, f"dump_{platform}.jsonl")
                cmd = [
                    sys.executable, "manage.py", "dump_postings",
                    "--platform", platform, "--company-ids-file", ids_path, "--out", dump_path,
                ]
                if options["rps"]:
                    cmd += ["--rps", str(options["rps"])]

                self.stdout.write(f"[{platform}] {len(ids)} watchlist companies -- dumping...")
                procs.append((platform, subprocess.Popen(cmd)))
                dump_paths[platform] = dump_path

            if not procs:
                self.stdout.write("no ranked companies to refresh -- run rank_preferred_companies first")
                return

            failed = [platform for platform, proc in procs if proc.wait() != 0]
            for platform in failed:
                self.stderr.write(f"[{platform}] dump subprocess failed -- excluding it from this ingest")
                del dump_paths[platform]

            if not dump_paths:
                self.stderr.write("every platform's dump failed -- nothing to ingest")
                return

            self.stdout.write(f"all dumps done ({len(dump_paths)} platform(s)) -- ingesting (single writer)...")
            ingest_files(list(dump_paths.values()), stdout=self.stdout)
