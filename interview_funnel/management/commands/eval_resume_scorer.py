"""
Eval harness for resume_scorer.ResumeScorer against a small hand-labeled
golden set (interview_funnel/eval/golden_set.json). Each golden-set entry
pairs a real posting -- looked up live from the DB, so the JD text stays
current -- with a human judgment: score, seniority fit, matched/missing
skills (see the manual scoring session that seeded Office Hours and
Hilberts entries).

Reports, per item and in aggregate:
  - predicted vs. human score (mean absolute error)
  - matched-skills overlap (Jaccard) between predicted and human lists
  - seniority_fit agreement
  - optional consistency check across repeated calls (--repeat N), since
    LLM scoring is non-deterministic and that's itself worth measuring

The golden set currently only has 2 entries. Add more over time as
postings get hand-scored -- that's the actual bottleneck on making this
eval meaningful, not the harness itself.

Run: python manage.py eval_resume_scorer
     python manage.py eval_resume_scorer --repeat 3
     python manage.py eval_resume_scorer --output eval_report.json
"""

import json
import statistics
from pathlib import Path

import anthropic
from django.core.management.base import BaseCommand, CommandError

from resume_scorer import ResumeScorer
from tracker.models import Posting

DEFAULT_GOLDEN_SET = Path(__file__).resolve().parent.parent.parent / "eval" / "golden_set.json"


def jaccard(a, b):
    a, b = {s.lower() for s in a}, {s.lower() for s in b}
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


class Command(BaseCommand):
    help = "Evaluate resume_scorer.ResumeScorer against a hand-labeled golden set of postings."

    def add_arguments(self, parser):
        parser.add_argument("--golden-set", type=str, default=str(DEFAULT_GOLDEN_SET))
        parser.add_argument("--repeat", type=int, default=1, help="Calls per item, to check score consistency (default: 1)")
        parser.add_argument("--output", type=str, default=None, help="Optional path to write a JSON report")

    def handle(self, *args, **options):
        golden_path = Path(options["golden_set"])
        if not golden_path.exists():
            raise CommandError(f"Golden set not found: {golden_path}")
        golden_set = json.loads(golden_path.read_text(encoding="utf-8"))

        scorer = ResumeScorer()
        repeat = options["repeat"]
        report = []

        for entry in golden_set:
            qs = Posting.objects.filter(company__name__icontains=entry["company"], title=entry["title"])
            if entry.get("location"):
                qs = qs.filter(location=entry["location"])
            posting = qs.select_related("company").first()
            if not posting:
                self.stdout.write(self.style.WARNING(f"Skipping (not found in DB): {entry['company']} - {entry['title']}"))
                continue

            scores = []
            last_result = None
            try:
                for _ in range(repeat):
                    result = scorer.score(posting.description)
                    scores.append(result["score"])
                    last_result = result
            except anthropic.AuthenticationError:
                raise CommandError(
                    "ANTHROPIC_API_KEY is invalid (401 from Anthropic) -- update .env and retry. "
                    "The eval harness itself is fine; it just can't call the model right now."
                )

            mean_score = statistics.mean(scores)
            stdev_score = statistics.stdev(scores) if len(scores) > 1 else 0.0
            skill_overlap = jaccard(last_result.get("matched_skills", []), entry["human_matched_skills"])
            seniority_match = last_result.get("seniority_fit") == entry["human_seniority_fit"]

            item_report = {
                "company": entry["company"],
                "title": entry["title"],
                "human_score": entry["human_score"],
                "predicted_score_mean": round(mean_score, 1),
                "predicted_score_stdev": round(stdev_score, 2),
                "abs_error": round(abs(mean_score - entry["human_score"]), 1),
                "matched_skills_jaccard_vs_human": round(skill_overlap, 2),
                "predicted_seniority_fit": last_result.get("seniority_fit"),
                "human_seniority_fit": entry["human_seniority_fit"],
                "seniority_fit_match": seniority_match,
            }
            report.append(item_report)
            self.stdout.write(
                f"{entry['company']} - {entry['title']}: "
                f"human={entry['human_score']} predicted={item_report['predicted_score_mean']} "
                f"(+/-{item_report['predicted_score_stdev']}) abs_error={item_report['abs_error']} "
                f"skill_jaccard={item_report['matched_skills_jaccard_vs_human']} "
                f"seniority_fit={'match' if seniority_match else 'MISMATCH'}"
            )

        if not report:
            self.stdout.write(self.style.WARNING("No golden-set items matched a posting in the DB -- nothing to report."))
            return

        mae = statistics.mean(r["abs_error"] for r in report)
        mean_jaccard = statistics.mean(r["matched_skills_jaccard_vs_human"] for r in report)
        self.stdout.write(self.style.SUCCESS(
            f"\nMAE: {round(mae, 2)}  |  mean skill Jaccard: {round(mean_jaccard, 2)}  |  n={len(report)}"
        ))

        if options["output"]:
            out_path = Path(options["output"])
            out_path.write_text(
                json.dumps({"items": report, "mae": mae, "mean_skill_jaccard": mean_jaccard}, indent=2),
                encoding="utf-8",
            )
            self.stdout.write(f"Wrote report to {out_path.resolve()}")
