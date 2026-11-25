"""
Stage 1 gatekeeper: score Bay Area + remote-US SWE/Fullstack postings (last
3 months) against Austin Kim's resume. Deterministic keyword/seniority
scoring -- no LLM calls, so it's fast and free to re-run. Postings marked
stage1_pass=yes are the ones `manage.py score_postings` (Stage 2,
resume_scorer.py) spends API calls on.

Candidate profile (from Resume v12.pdf):
  ~4.75 YOE combined (Workiva Aug'19-Feb'20, Mammoth Biosciences Feb'21-May'25)
  languages: Python, TypeScript, Java, MySQL
  backend:   Django, Node.js, REST APIs, GraphQL, API design, data modeling
  frontend:  React, Redux, D3, Material-UI, HTML5, CSS3
  infra:     Docker, Docker Compose, CI/CD, Prefect, Terraform, AWS, GitHub Actions
  data:      Pandas, Numpy, Jupyter
  domain:    Life sciences / biotech / R&D data platforms

Run: python score_bay_area_postings.py
"""

import csv
import html
import os
import re
from datetime import datetime, timedelta, timezone

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dj_config.settings")
django.setup()

from django.db.models import Q  # noqa: E402

from tracker.models import Posting  # noqa: E402

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "bay_area_fullstack_postings_scored.csv")

BAY_AREA_CITIES = [
    "san francisco", r"\bsf\b", "oakland", "berkeley", "san jose",
    "palo alto", "mountain view", "redwood city", "san mateo",
    "sunnyvale", "menlo park", "santa clara", "fremont", "cupertino",
    "south san francisco", "burlingame", "foster city", "emeryville",
    "daly city", "walnut creek", "san rafael", "novato", "pleasanton",
    "bay area",
]

# Whitelist of qualifier+"engineer" phrases that denote a general software
# engineering IC role, as opposed to an adjacent discipline that also happens
# to use the word "engineer" (QA, DevOps/SRE, Data, ML, Security, Sales,
# Solutions, Support, Hardware/Firmware, Network...). Inclusion is by phrase
# match, not a bare "engineer" catch-all, so those adjacent disciplines are
# excluded by construction rather than needing to be named individually --
# except where a whitelisted phrase can still legitimately appear inside one
# of them (e.g. "Software Engineer in Test", "Founding Engineer, DevOps"),
# which DISQUALIFY_RE below catches as a second pass.
SWE_TITLE_PATTERNS = [
    r"software engineer", r"\bswe\b", r"full[- ]?stack",
    r"member of technical staff", r"design engineer", r"founding engineer",
    r"back[- ]?end engineer", r"front[- ]?end engineer",
    r"application engineer", r"platform engineer", r"product engineer",
]

# Subset of SWE_TITLE_PATTERNS specific enough that a match is never a
# hardware/civil/chip role wearing an "engineer" title -- used to short-circuit
# HARDWARE_ROLE_RE below. "design engineer" and "application engineer" are
# deliberately excluded from this set: both are used interchangeably by
# software product teams (Gumloop, Supabase -- "Design Engineer" meaning a
# UI-focused SWE) and by hardware/civil/chip teams (Jane Street ASIC Physical
# Design Engineer, SpaceX avionics Design Engineer, field pre-sales Application
# Engineer roles) with no reliable title-only split -- see HARDWARE_ROLE_RE.
STRONG_SWE_TITLE_RE = re.compile(
    r"software engineer|\bswe\b|full[- ]?stack|back[- ]?end engineer|front[- ]?end engineer",
    re.IGNORECASE,
)

# Signal that a "Design Engineer" / "Application Engineer" / "Product Engineer"
# / "Platform Engineer" title is actually a hardware, civil, chip, or field
# discipline, not software -- checked against title+description together
# since the title alone is frequently identical between a Gumloop-style UI
# design engineer and a Jane Street ASIC physical design engineer (see
# is_hardware_flavored_role below). Discovered by auditing 494 live "design
# engineer" and 123 "application engineer" postings on the SWE dashboard,
# where ~90% turned out to be electrical/mechanical/civil/chip/field roles the
# title-only filter couldn't distinguish from real software roles.
HARDWARE_ROLE_RE = re.compile(
    r"manufactur|prefabricat|sheet metal|\bCAD\b|SolidWorks|AutoCAD|\bGD&T\b|"
    r"mechanical|electrical|structural|civil engineer|thermal|\bHVAC\b|plumbing|"
    r"\bRF filter|avionics|\bPCB\b|assembly line|tooling design|fixture tooling|fabrication|machining|"
    r"schematic capture|circuit board|automotive|bumper|fascia|aerospace structures|"
    r"power group|piping|welding|hydraulic|pneumatic|"
    r"\bASIC\b|\bFPGA\b|physical design|analog design|digital design|\bPnR\b|"
    r"place.and.route|propulsion|roadway|highway design|water/wastewater|"
    r"wastewater|building design|distribution design|silicon(?!\s+valley)|semiconductor|chip design|"
    r"instructional systems",
    re.IGNORECASE,
)

DISQUALIFY_RE = re.compile(
    r"\bqa\b|quality assurance|\bsdet\b|in test\b|devops|site reliability|\bsre\b|"
    r"machine learning|\bml\b|data engineer|security engineer|network engineer|"
    r"hardware engineer|firmware engineer|sales engineer|solutions engineer|"
    r"support engineer|field engineer|release engineer|systems engineer|test engineer|"
    r"\bintern(ship)?\b",
    re.IGNORECASE,
)


def build_swe_title_q() -> Q:
    q = Q()
    for pattern in SWE_TITLE_PATTERNS:
        q |= Q(title__iregex=pattern)
    return q


def is_hardware_flavored_role(title: str, description: str) -> bool:
    """True if an ambiguous SWE-title-pattern match (Design/Application/
    Product/Platform Engineer) is actually a hardware, civil, chip, or field
    role rather than software. Titles matching STRONG_SWE_TITLE_RE are never
    flagged -- an unambiguous "Software Engineer, Design Engineering" stays in
    even if the description mentions avionics elsewhere."""
    if STRONG_SWE_TITLE_RE.search(title or ""):
        return False
    return bool(HARDWARE_ROLE_RE.search(title or "") or HARDWARE_ROLE_RE.search(description or ""))


def build_bay_area_loc_q() -> Q:
    """Matches Bay Area either from the raw ATS location string (works for
    every platform) or the normalized Location m2m city (catches Ashby
    secondary locations that don't show up in the primary location string,
    e.g. a posting listed as "Remote" whose secondary office is SF)."""
    q = Q()
    for city in BAY_AREA_CITIES:
        q |= Q(location__iregex=city) | Q(locations__city__iregex=city)
    return q

# Stage-1 gatekeeper. This script is the free, deterministic first pass: it
# decides which postings are worth a paid Stage-2 read (resume_scorer.py,
# `manage.py score_postings`), it does not try to be the fit score itself.
# So the keyword categories below only ADD points -- a JD that names a
# different-but-transferable web framework (Rails, FastAPI, Next.js, Vue...)
# or no framework at all is not penalized, only a dominant non-transferable
# primary language is (STACK_MISMATCH_SIGNALS).
CATEGORY_KEYWORDS = {
    "languages": [r"\bpython\b", r"\btypescript\b", r"\bjavascript\b", r"\bjava\b(?!script)",
                  r"\bmysql\b", r"\bsql\b", r"\bpostgres(ql)?\b"],
    # Transferable full-stack rule: React <-> Next.js/Vue/Angular/Svelte.
    "frontend": [r"\breact\b", r"\bredux\b", r"\bd3(\.js)?\b", r"\bhtml5?\b", r"\bcss3?\b",
                 r"\bmaterial.?ui\b", r"\bnext\.?js\b", r"\bvue(\.js)?\b", r"\bangular\b",
                 r"\bsvelte\b", r"\bfront[- ]?end\b"],
    # Transferable full-stack rule: Python/Django <-> Node/TS/Rails/FastAPI/Flask/Express/Spring.
    "backend": [r"\bdjango\b", r"\bnode\.?js\b", r"\brest(ful)?\b", r"\bgraphql\b",
                r"\bapis?\b", r"api design", r"data model", r"\bfastapi\b", r"\bflask\b",
                r"\brails\b", r"\bexpress(\.js)?\b", r"\bnest\.?js\b", r"\bspring\b",
                r"\bmicroservices?\b", r"\bback[- ]?end\b"],
    "infra": [r"\bdocker\b", r"\bci\s?/\s?cd\b", r"\bterraform\b", r"\baws\b",
              r"\bgithub actions\b", r"\bcloud\b", r"\bgcp\b"],
    "data": [r"\bpandas\b", r"\bnumpy\b", r"\bjupyter\b", r"\betl\b", r"data pipeline"],
}
CATEGORY_POINTS = {"languages": 15, "frontend": 10, "backend": 10, "infra": 5, "data": 5}
BASE_SCORE = 40

# A primary language the resume doesn't have and that isn't a transferable
# web-framework swap. Ruby/Rails, Vue and Angular used to be here; under the
# transferable full-stack rules they're credited above instead.
#
# "Go" is matched case-sensitively: lowercase "go" is an English verb
# ("you want to go deep with React" -- the Quantifind false positive) and
# "GO" shows up in headings/acronyms. Golang is unambiguous in any case.
STACK_MISMATCH_SIGNALS = [
    ("Go/Golang", r"(?-i:\bGo\b)|\bgolang\b"),
    ("PHP", r"\bphp\b"),
    ("C++", r"c\+\+"),
    ("C#/.NET", r"c#|\.net\b"),
    ("Rust", r"\brust\b"),
]

# A mismatch language listed as one option among languages the resume DOES
# have ("a compiled language (C++, Java, etc)", "Java, Go or Python") is an
# alternatives list, not a primary-stack requirement -- the Everlaw false
# positives. Each mention is checked against a window of text around it.
RESUME_LANGUAGE_RE = re.compile(r"\bpython\b|\btypescript\b|\bjavascript\b|\bjava\b(?!script)", re.IGNORECASE)
ALTERNATIVES_WINDOW = 60

# Posting descriptions come from ATS APIs as HTML, often entity-escaped
# ("&lt;li&gt;"), so tags have to be unescaped then stripped before matching.
_TAG_RE = re.compile(r"<[^>]+>")


def clean_description(description: str) -> str:
    text = _TAG_RE.sub(" ", html.unescape(html.unescape(description or "")))
    return re.sub(r"\s+", " ", text)


def find_stack_mismatch(text: str) -> str | None:
    """Label of the first non-transferable primary language that appears at
    least once OUTSIDE an alternatives list with one of the resume's own
    languages, or None."""
    for label, pattern in STACK_MISMATCH_SIGNALS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            window = text[max(0, m.start() - ALTERNATIVES_WINDOW): m.end() + ALTERNATIVES_WINDOW]
            if not RESUME_LANGUAGE_RE.search(window):
                return label
    return None

# "staff" alone means over-leveled (Staff Engineer), but "Member of Technical
# Staff" -- a normal IC title at a lot of AI/research orgs, not a seniority
# signal -- also contains the substring "staff". The negative lookbehind
# keeps "Staff Engineer"/"Senior Staff" flagged while leaving MTS titles alone.
SENIORITY_PATTERNS = [
    ("over", r"principal|founding|tech(nical)?\s+lead|\blead\b|(?<!technical )staff"),
    ("senior", r"senior|\bsr\.?\b"),
    ("junior", r"junior|\bjr\.?\b|entry.?level|new grad"),
]

DOMAIN_BONUS_RE = re.compile(r"biotech|therapeutics|biosciences|genomics|life sciences|pharmaceutical", re.IGNORECASE)


def classify_seniority(title: str) -> str:
    for label, pattern in SENIORITY_PATTERNS:
        if re.search(pattern, title, re.IGNORECASE):
            return label
    return "mid"


def score_posting(title: str, description: str) -> tuple[int, str]:
    text = f"{title}\n{clean_description(description)}"

    matched, gaps = [], []
    for category, patterns in CATEGORY_KEYWORDS.items():
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            matched.append(category)
        else:
            gaps.append(category)

    mismatch = find_stack_mismatch(text)

    seniority = classify_seniority(title)
    domain_bonus = bool(DOMAIN_BONUS_RE.search(text))

    score = BASE_SCORE + sum(CATEGORY_POINTS[c] for c in matched)
    if mismatch:
        score -= 15
    if seniority == "over":
        score -= 25
    if domain_bonus:
        score += 5
    score = max(0, min(100, score))

    notes_parts = [
        f"skills matched: {', '.join(matched) if matched else 'none'}",
        f"gaps: {', '.join(gaps) if gaps else 'none'}",
    ]
    if mismatch:
        notes_parts.append(f"stack mismatch: {mismatch}")
    if seniority == "over":
        notes_parts.append("over-leveled (Staff/Principal/Founding/Tech Lead-type role vs ~4-5 YOE)")
    elif seniority == "senior":
        notes_parts.append("senior-leaning, included (no seniority penalty)")
    elif seniority == "junior":
        notes_parts.append("junior/entry-level, included (no seniority penalty)")
    else:
        notes_parts.append("good seniority match (mid-level IC)")
    if domain_bonus:
        notes_parts.append("domain bonus: biotech/life-sciences overlap with candidate background")

    return score, "; ".join(notes_parts)


# Stage-1 gate: postings at/above this keyword score (and not over-leveled)
# go on to the paid Stage-2 calibrated scorer. 55 lets through any posting
# that names one of the resume's languages plus some web stack, even with a
# -15 non-transferable-language hit, while dropping e.g. pure Go/Rust shops.
STAGE1_PASS_THRESHOLD = 55
# A description this short is a scrape/render failure, not a real JD -- let
# Stage 2 (or a human) look rather than rejecting on missing keywords.
MIN_DESCRIPTION_CHARS = 300


def stage1_gate(title: str, description: str, score: int | None = None) -> tuple[bool, str]:
    """(passes, reason) for the free Stage-1 gatekeeper. Title/location/
    disqualifier filtering happens in the query (build_stage1_queryset);
    this is the per-posting content check."""
    if score is None:
        score, _ = score_posting(title, description)
    if classify_seniority(title) == "over":
        return False, "over-leveled title"
    if len(clean_description(description)) < MIN_DESCRIPTION_CHARS:
        return True, "description too short to keyword-score; needs a Stage-2 read"
    if score < STAGE1_PASS_THRESHOLD:
        return False, f"keyword score {score} < {STAGE1_PASS_THRESHOLD}"
    return True, f"keyword score {score} >= {STAGE1_PASS_THRESHOLD}"


def build_remote_us_q() -> Q:
    """US-remote postings: a bare "Remote" / "Remote - US" location string,
    or the platform's own remote flag on a posting whose normalized location
    is in the US. Global/EU-only remote is left out."""
    return (
        Q(location__iregex=r"^\s*remote\s*$")
        | Q(location__iregex=r"remote.{0,20}\b(us|usa|u\.s\.|united states)\b")
        | Q(location__iregex=r"\b(us|usa|united states)\b.{0,5}remote")
        | (Q(workplace_type__iexact="remote") & Q(locations__region="us"))
    )


def build_stage1_queryset(days: int = 90):
    """Recent SWE-titled postings in the Bay Area or remote-US, minus
    Staff-level and adjacent-discipline titles. Shared with
    `manage.py score_postings` so Stage 2 only sees what Stage 1 sees."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    # "technical staff" carve-out: see SENIORITY_PATTERNS. Senior is not
    # excluded here; it's scored without penalty.
    exclude_filter = Q(title__iregex=r"(?<!technical )staff")
    return (
        Posting.objects.filter(build_swe_title_q(), build_bay_area_loc_q() | build_remote_us_q(),
                               posted_at__gte=cutoff)
        .exclude(exclude_filter)
        .exclude(title__iregex=DISQUALIFY_RE.pattern)
        .select_related("company")
        .distinct()
    )


def main():
    qs = build_stage1_queryset()
    print(f"scoring {qs.count()} postings...")

    rows = []
    for p in qs:
        if is_hardware_flavored_role(p.title, p.description):
            continue
        score, notes = score_posting(p.title, p.description)
        passed, reason = stage1_gate(p.title, p.description, score)
        rows.append({
            "company": p.company.name or p.company.slug,
            "title": p.title,
            "location": p.location,
            "avg_comp": p.avg_comp or "",
            "lowest_comp": p.lowest_comp or "",
            "workplace_type": p.workplace_type,
            "posted_at": p.posted_at.date().isoformat() if p.posted_at else "",
            "ats_fit_score": score,
            "stage1_pass": "yes" if passed else "no",
            "stage1_reason": reason,
            "recruiter_fit_score": p.recruiter_fit_score if p.recruiter_fit_score is not None else "",
            "desirability_score": p.desirability_score if p.desirability_score is not None else "",
            "ats_notes": notes,
            "url": p.public_url,
        })

    rows.sort(key=lambda r: (r["stage1_pass"] == "yes", r["ats_fit_score"]), reverse=True)

    fieldnames = ["company", "title", "location", "avg_comp", "lowest_comp",
                  "workplace_type", "posted_at", "ats_fit_score", "stage1_pass", "stage1_reason",
                  "recruiter_fit_score", "desirability_score", "ats_notes", "url"]
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    n_pass = sum(r["stage1_pass"] == "yes" for r in rows)
    print(f"wrote {len(rows)} rows to {os.path.abspath(OUT_PATH)}")
    print(f"stage 1: {n_pass} pass / {len(rows) - n_pass} gated out")
    if rows:
        scores = [r["ats_fit_score"] for r in rows]
        print(f"score distribution: min={min(scores)}, max={max(scores)}, avg={sum(scores) / len(scores):.1f}")


if __name__ == "__main__":
    main()
