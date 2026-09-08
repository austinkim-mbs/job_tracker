"""
Generate a static, self-contained HTML insights page for recent SWE-track
postings: ATS fit score distribution/trend, tech/role/industry breakdowns,
and comp stats. No server needed -- open the output file directly in a
browser, or host it anywhere as a flat file.

Title/exclude filtering and the ATS fit scorer are the same ones used by
score_bay_area_postings.py (deterministic keyword scoring against
Resume v12.pdf), just applied across all postings rather than Bay Area/90-day
ones, and ordered by recency instead of score.

Run: python manage.py generate_insights
     python manage.py generate_insights --limit 200 --output reports/insights.html
     python manage.py generate_insights --top-companies 10
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

from django.core.management.base import BaseCommand
from django.db.models import Q

from score_bay_area_postings import score_posting
from tracker.models import Posting

TEMPLATE_PATH = Path(__file__).resolve().parent / "insights_template.html"

TITLE_FILTER = (
    Q(title__iregex=r"software engineer")
    | Q(title__iregex=r"full[- ]?stack")
    | Q(title__iregex=r"member of technical staff")
)
# "staff" alone means over-leveled (Staff Engineer); "Member of Technical
# Staff" is a normal IC title and shouldn't be excluded just because "staff"
# is a substring of it -- same carve-out as score_bay_area_postings.py.
EXCLUDE_FILTER = (
    Q(title__iregex=r"tech(nical)?\s+lead")
    | Q(title__iregex=r"(?<!technical )staff")
)

TECH_PATTERNS = [
    ("Python", r"\bpython\b"), ("AWS", r"\baws\b|\bamazon web services\b"),
    ("TypeScript", r"\btypescript\b"), ("Go", r"\bgo(lang)?\b"),
    ("React", r"\breact(\.js)?\b"), ("Kubernetes", r"\bkubernetes\b|\bk8s\b"),
    ("PostgreSQL", r"\bpostgres(ql)?\b"), ("LLM / GenAI", r"\bllm\b|\bgenerative ai\b|\bgen ai\b|large language model"),
    ("Java", r"\bjava\b(?!script)"), ("Docker", r"\bdocker\b"),
    ("C++", r"c\+\+"), ("Machine Learning", r"\bmachine learning\b"),
    ("SQL", r"\bsql\b"), ("JavaScript", r"\bjavascript\b"),
    ("C#/.NET", r"c#|\.net\b"), ("Node.js", r"\bnode\.?js\b"),
    ("Kafka", r"\bkafka\b"), ("Rust", r"\brust\b"), ("Terraform", r"\bterraform\b"),
]

ROLE_PATTERNS = [
    ("ML / AI Engineer", r"machine learning|\bml\b|\bai\b|artificial intelligence|applied scientist"),
    ("Data Engineer", r"\bdata\b.*(engineer)|data platform|data infrastructure"),
    ("Mobile Engineer", r"\bios\b|\bandroid\b|\bmobile\b"),
    ("DevOps / Infra / Platform", r"devops|site reliability|\bsre\b|infrastructure|platform engineer|cloud engineer"),
    ("QA / Test", r"\bqa\b|quality assurance|\btest\b|sdet"),
    ("Security Engineer", r"security engineer|application security|\bcyber"),
    ("Frontend Engineer", r"front.?end"),
    ("Backend Engineer", r"back.?end"),
    ("Full Stack Engineer", r"full.?stack"),
    ("Member of Technical Staff", r"member of technical staff"),
    ("Engineering Manager / Lead", r"engineering manager|\bmanager\b|\blead\b"),
    ("Intern / New Grad", r"intern|new grad|entry.?level"),
    ("Senior Software Engineer", r"senior|\bsr\.?\b"),
    ("Software Engineer (general)", r"software engineer|software developer|swe\b"),
]

INDUSTRY_PATTERNS = [
    ("AI / ML / Research", r"foundation model|frontier model|\bllm\b|large language model|generative ai|\bgenai\b|ai research lab|artificial general intelligence|\bagi\b|ai.?native"),
    ("Robotics / Hardware / Autonomy", r"robotics|autonomous vehicle|self.?driving|\bdrone\b|hardware engineering|semiconductor|aerospace|defense technology|defense contractor"),
    ("Fintech / Payments / Crypto", r"\bfintech\b|payments? (platform|processing|company|infrastructure)|digital payments|trading platform|investment banking|\bcrypto\b|blockchain|hedge fund|asset management|neobank|\blending\b"),
    ("Healthcare / Biotech", r"\bhealthcare\b|health.?tech|\bbiotech\b|\bpharma\b|clinical trial|life sciences|therapeutics|genomics|diagnostics|patient care|electronic health record"),
    ("Cybersecurity", r"cybersecurity|\binfosec\b|threat detection|security platform|security operations"),
    ("E-commerce / Retail", r"e.?commerce|marketplace|online retail|shopping cart|checkout experience"),
    ("Gaming / Entertainment", r"\bgaming\b|video game|game studio|entertainment industry|streaming media"),
    ("Government / Defense (GovTech)", r"govtech|gov\.?tech|government contractor|public sector|department of defense|\bdod\b|national security (agency|clearance)"),
    ("Education / EdTech", r"\bedtech\b|education platform|online learning|\blms\b"),
    ("Real Estate / PropTech", r"real estate|proptech|property management"),
    ("Developer Tools / Infra SaaS", r"developer tools|devtools|api platform|infrastructure platform|observability platform|developer experience"),
    ("Enterprise SaaS", r"\bsaas\b|enterprise software|b2b software"),
]


def classify(text, patterns, default="Other"):
    for label, pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return label
    return default


class Command(BaseCommand):
    help = "Generate a static HTML insights page for the most recent SWE-track postings."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500, help="Most recent postings to pull (default: 500)")
        parser.add_argument("--output", type=str, default="insights.html", help="Output file path (default: insights.html)")
        parser.add_argument("--top-companies", type=int, default=30, help="Rows in the top-companies table (default: 30)")

    def handle(self, *args, **options):
        limit = options["limit"]
        top_companies_n = options["top_companies"]
        output_path = Path(options["output"])

        postings = list(
            Posting.objects.filter(TITLE_FILTER)
            .exclude(EXCLUDE_FILTER)
            .select_related("company")
            .order_by("-posted_at")[:limit]
        )
        if not postings:
            self.stdout.write(self.style.WARNING("No matching postings found."))
            return

        n = len(postings)
        scored = []
        for p in postings:
            score, _ = score_posting(p.title, p.description)
            scored.append((score, p))

        scores = [sc for sc, _ in scored]

        by_day = defaultdict(list)
        for sc, p in scored:
            if p.posted_at:
                by_day[p.posted_at.date().isoformat()].append(sc)
        trend = [{"day": day, "mean_score": round(mean(v), 1), "count": len(v)} for day, v in sorted(by_day.items())]

        tech_counts = Counter()
        role_counts = Counter()
        industry_counts = Counter()
        for _, p in scored:
            text = f"{p.title}\n{p.description}"
            for name, pattern in TECH_PATTERNS:
                if re.search(pattern, text, re.IGNORECASE):
                    tech_counts[name] += 1
            role_counts[classify(p.title, ROLE_PATTERNS)] += 1
            industry_counts[classify(text, INDUSTRY_PATTERNS, default="Other / Unclassified")] += 1

        tech = [{"name": k, "count": v, "pct": round(v / n * 100, 1)} for k, v in tech_counts.most_common(15)]
        roles = [{"name": k, "count": v, "pct": round(v / n * 100, 1)} for k, v in role_counts.most_common()]
        industries = [{"name": k, "count": v, "pct": round(v / n * 100, 1)} for k, v in industry_counts.most_common()]

        buckets = [
            (0, 80_000, "<80k"), (80_000, 110_000, "80-110k"), (110_000, 140_000, "110-140k"),
            (140_000, 170_000, "140-170k"), (170_000, 200_000, "170-200k"),
            (200_000, 250_000, "200-250k"), (250_000, float("inf"), "250k+"),
        ]
        comp_hist = []
        for lo, hi, label in buckets:
            avg_n = sum(1 for _, p in scored if p.avg_comp and lo <= p.avg_comp < hi)
            low_n = sum(1 for _, p in scored if p.lowest_comp and lo <= p.lowest_comp < hi)
            comp_hist.append({"label": label, "avg_comp_count": avg_n, "lowest_comp_count": low_n})

        score_buckets = defaultdict(int)
        for sc in scores:
            score_buckets[(sc // 10) * 10] += 1
        score_dist = [{"bucket": f"{b}-{b + 9}", "count": score_buckets.get(b, 0)} for b in range(0, 70, 10)]

        avg_comps = [p.avg_comp for _, p in scored if p.avg_comp]
        low_comps = [p.lowest_comp for _, p in scored if p.lowest_comp]
        top_companies = [
            {"name": k, "count": v}
            for k, v in Counter(p.company.name or p.company.slug for _, p in scored).most_common(top_companies_n)
        ]

        summary = {
            "n_postings": n,
            "date_range": {
                "from": min(p.posted_at.isoformat() for _, p in scored if p.posted_at),
                "to": max(p.posted_at.isoformat() for _, p in scored if p.posted_at),
            },
            "score_mean": round(mean(scores), 1),
            "score_median": median(scores),
            "score_min": min(scores),
            "score_max": max(scores),
            "comp_disclosed_count": len(avg_comps),
            "comp_disclosed_pct": round(len(avg_comps) / n * 100, 1) if avg_comps else 0,
            "avg_comp_mean": round(mean(avg_comps)) if avg_comps else 0,
            "avg_comp_median": round(median(avg_comps)) if avg_comps else 0,
            "lowest_comp_mean": round(mean(low_comps)) if low_comps else 0,
            "lowest_comp_median": round(median(low_comps)) if low_comps else 0,
        }

        data = {
            "summary": summary,
            "trend": trend,
            "tech": tech,
            "roles": roles,
            "industries": industries,
            "comp_hist": comp_hist,
            "score_dist": score_dist,
            "top_companies": top_companies,
        }

        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        html = html.replace("__DATA_JSON__", json.dumps(data)).replace("__N__", str(n))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html, encoding="utf-8")

        self.stdout.write(self.style.SUCCESS(
            f"Wrote {n} postings to {output_path.resolve()} "
            f"(mean score {summary['score_mean']}, {summary['comp_disclosed_pct']}% comp disclosed)"
        ))
