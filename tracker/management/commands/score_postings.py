"""
Stage 2: run the calibrated Claude scorer (resume_scorer.py) over postings
that pass the free Stage-1 gate (score_bay_area_postings.py) and save
ats_score / recruiter_fit_score / desirability_score / role_type / fit_notes
onto each Posting, so the SWE market dashboard's ATS and Fit columns fill in.

Each posting is one API call (claude-sonnet-4-6), so this defaults to a
small --limit and to skipping postings that already have a score. Postings
scored by earlier manual /score-job sessions (blank fit_scorer_version) are
skipped too unless --rescore is passed.

Run: python manage.py score_postings --dry-run
     python manage.py score_postings --limit 25
     python manage.py score_postings --limit 200 --days 30
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from resume_scorer import SCORER_VERSION, ResumeScorer
from score_bay_area_postings import build_stage1_queryset, is_hardware_flavored_role, stage1_gate


class Command(BaseCommand):
    help = "Stage-2 Claude scoring of Stage-1-passing postings; saves scores onto Posting."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25, help="Max postings to score this run (default 25)")
        parser.add_argument("--days", type=int, default=90, help="Only postings posted in the last N days (default 90)")
        parser.add_argument("--rescore", action="store_true",
                            help="Also re-score postings already scored by an older/different scorer version")
        parser.add_argument("--dry-run", action="store_true", help="List what would be scored; no API calls")

    def handle(self, *args, **opts):
        qs = build_stage1_queryset(days=opts["days"])
        if opts["rescore"]:
            qs = qs.exclude(fit_scorer_version=SCORER_VERSION)
        else:
            qs = qs.filter(recruiter_fit_score__isnull=True)
        qs = qs.order_by("-posted_at")

        todo = []
        for p in qs.iterator():
            if is_hardware_flavored_role(p.title, p.description):
                continue
            passed, _ = stage1_gate(p.title, p.description)
            if passed:
                todo.append(p)
                if len(todo) >= opts["limit"]:
                    break

        self.stdout.write(f"{len(todo)} Stage-1-passing postings to score (limit {opts['limit']}).")
        if opts["dry_run"]:
            for p in todo:
                self.stdout.write(f"  {p.company.name} - {p.title} ({p.location})")
            return

        scorer = ResumeScorer()
        ok = failed = 0
        for p in todo:
            try:
                r = scorer.score_posting(p)
            except Exception as e:  # one bad posting shouldn't stop the batch
                failed += 1
                self.stdout.write(self.style.WARNING(f"  FAILED {p.company.name} - {p.title}: {e}"))
                continue
            ok += 1
            self.stdout.write(
                f"  [{r['tier']:<11}] fit={r['recruiter_fit_score']:>3} ats={r['ats_score']:>3} "
                f"want={r['desirability_score']:>3}  {p.company.name} - {p.title}"
            )
        self.stdout.write(self.style.SUCCESS(f"scored {ok}, failed {failed}"))
