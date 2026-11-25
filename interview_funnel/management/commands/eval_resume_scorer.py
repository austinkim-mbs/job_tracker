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

Each entry may also carry a "band" (must_pass | borderline | hard_reject).
An item PASSES when the predicted recruiter-fit score is within --tolerance
(default 5) of human_score, or, for entries labeled only with a band, within
tolerance of that band's range (must_pass 75-90, borderline 55-74,
hard_reject 0-40).

Run: python manage.py eval_resume_scorer
     python manage.py eval_resume_scorer --repeat 3
     python manage.py eval_resume_scorer --output eval_report.json
"""

import json
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
from django.core.management.base import BaseCommand, CommandError

from resume_scorer import SCORER_VERSION, ResumeScorer
from tracker.models import Posting

BANDS = {"must_pass": (75, 90), "borderline": (55, 74), "hard_reject": (0, 40)}

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
        parser.add_argument("--tolerance", type=int, default=5, help="Allowed points of error per item (default 5)")
        parser.add_argument("--workers", type=int, default=10, help="Concurrent API calls (default 10)")

    def handle(self, *args, **options):
        golden_path = Path(options["golden_set"])
        if not golden_path.exists():
            raise CommandError(f"Golden set not found: {golden_path}")
        golden_set = json.loads(golden_path.read_text(encoding="utf-8"))

        scorer = ResumeScorer()
        repeat = options["repeat"]
        report = []

        # Phase 1: resolve every entry to job text (DB work stays on this thread).
        jobs = []
        for entry in golden_set:
            # An entry may carry its own JD text ("description") -- e.g. labels
            # taken from a chat export where the posting was pasted, not crawled.
            # Otherwise the posting is looked up from the DB: by primary key when
            # the entry has "posting_id" (instant), else by company/title/location
            # (a scan of the multi-GB posting table).
            if entry.get("description"):
                job = {"description": entry["description"], "title": "", "location": entry.get("location", ""),
                       "company": "" if entry["company"].startswith("(") else entry["company"]}
            else:
                if entry.get("posting_id"):
                    qs = Posting.objects.filter(id=entry["posting_id"])
                else:
                    qs = Posting.objects.filter(company__name__icontains=entry["company"], title=entry["title"])
                    if entry.get("location"):
                        qs = qs.filter(location=entry["location"])
                posting = qs.select_related("company").first()
                if not posting:
                    self.stdout.write(self.style.WARNING(f"Skipping (not found in DB): {entry['company']} - {entry['title']}"))
                    continue
                job = {"description": posting.description, "title": posting.title, "location": posting.location,
                       "company": posting.company.name or posting.company.slug}
            jobs.append((entry, job))

        # Phase 2: all API calls concurrently -- each is ~30-60s of waiting on
        # the model, so running them one after another is what made evals slow.
        # The SDK retries 429s, so a too-high --workers only slows down, not fails.
        def run_one(job):
            return scorer.score(job["description"], title=job["title"],
                                location=job["location"], company=job["company"])

        results = {i: [] for i in range(len(jobs))}
        with ThreadPoolExecutor(max_workers=options["workers"]) as pool:
            futures = {pool.submit(run_one, job): i for i, (_, job) in enumerate(jobs) for _ in range(repeat)}
            for fut in as_completed(futures):
                try:
                    results[futures[fut]].append(fut.result())
                except anthropic.AuthenticationError:
                    pool.shutdown(cancel_futures=True)
                    raise CommandError(
                        "ANTHROPIC_API_KEY is invalid (401 from Anthropic) -- update .env and retry. "
                        "The eval harness itself is fine; it just can't call the model right now."
                    )
                except Exception as e:  # one failed call shouldn't sink the run
                    entry = jobs[futures[fut]][0]
                    self.stdout.write(self.style.WARNING(f"Call failed for {entry['company']} - {entry['title']}: {e}"))

        # Phase 3: report in golden-set order.
        for i, (entry, job) in enumerate(jobs):
            if not results[i]:
                continue
            scores = [r["score"] for r in results[i]]
            last_result = results[i][-1]

            mean_score = statistics.mean(scores)
            tol = options["tolerance"]
            band = entry.get("band")
            if entry.get("human_score") is not None:
                lo = hi = entry["human_score"]
            else:
                lo, hi = BANDS[band]
            within = (lo - tol) <= mean_score <= (hi + tol)
            stdev_score = statistics.stdev(scores) if len(scores) > 1 else 0.0
            # Transferable skills count as matched for the human-label comparison --
            # the hand labels predate the matched/transferable split.
            predicted_skills = last_result.get("matched_skills", []) + last_result.get("transferable_skills", [])
            skill_overlap = jaccard(predicted_skills, entry.get("human_matched_skills", []))
            seniority_match = (last_result.get("seniority_fit") == entry["human_seniority_fit"]
                               if entry.get("human_seniority_fit") else None)

            item_report = {
                "company": entry["company"],
                "title": entry["title"],
                "band": band,
                "human_score": entry.get("human_score"),
                "target_range": [lo, hi],
                "within_tolerance": within,
                "predicted_tier": last_result.get("tier"),
                "predicted_ats_score": last_result.get("ats_score"),
                "predicted_desirability": last_result.get("desirability_score"),
                "predicted_role_type": last_result.get("role_type"),
                "predicted_score_mean": round(mean_score, 1),
                "predicted_score_stdev": round(stdev_score, 2),
                "abs_error": round(max(lo - mean_score, mean_score - hi, 0), 1),
                "matched_skills_jaccard_vs_human": round(skill_overlap, 2),
                "predicted_seniority_fit": last_result.get("seniority_fit"),
                "human_seniority_fit": entry.get("human_seniority_fit"),
                "seniority_fit_match": seniority_match,
            }
            report.append(item_report)
            self.stdout.write(
                f"{'PASS' if within else 'FAIL'}  {entry['company']} - {entry['title']}: "
                f"target={lo if lo == hi else f'{lo}-{hi}'} predicted={item_report['predicted_score_mean']} "
                f"(+/-{item_report['predicted_score_stdev']}) abs_error={item_report['abs_error']} "
                f"skill_jaccard={item_report['matched_skills_jaccard_vs_human']} "
                f"seniority_fit={'n/a' if seniority_match is None else 'match' if seniority_match else 'MISMATCH'}"
            )

        if not report:
            self.stdout.write(self.style.WARNING("No golden-set items matched a posting in the DB -- nothing to report."))
            return

        mae = statistics.mean(r["abs_error"] for r in report)
        n_pass = sum(r["within_tolerance"] for r in report)
        mean_jaccard = statistics.mean(r["matched_skills_jaccard_vs_human"] for r in report)
        self.stdout.write(self.style.SUCCESS(
            f"\n{SCORER_VERSION}  |  within +/-{options['tolerance']}: {n_pass}/{len(report)}  |  "
            f"MAE (outside target): {round(mae, 2)}  |  mean skill Jaccard: {round(mean_jaccard, 2)}"
        ))

        if options["output"]:
            out_path = Path(options["output"])
            out_path.write_text(
                json.dumps({"scorer": SCORER_VERSION, "items": report, "passed": n_pass, "mae": mae,
                            "mean_skill_jaccard": mean_jaccard}, indent=2),
                encoding="utf-8",
            )
            self.stdout.write(f"Wrote report to {out_path.resolve()}")
