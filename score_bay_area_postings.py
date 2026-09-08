"""
One-off export: score Bay Area SWE/Fullstack postings (last 3 months) against
Austin Kim's resume, ATS-style. Deterministic keyword/seniority scoring --
no LLM calls, so it's fast and free to re-run.

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
    r"support engineer|field engineer|release engineer|systems engineer|test engineer",
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

CATEGORY_KEYWORDS = {
    "languages": [r"\bpython\b", r"\btypescript\b", r"\bjava\b(?!script)", r"\bmysql\b", r"\bsql\b"],
    "frontend": [r"\breact\b", r"\bredux\b", r"\bd3(\.js)?\b", r"\bhtml5?\b", r"\bcss3?\b",
                 r"\bmaterial.?ui\b", r"\btypescript\b", r"\bjavascript\b"],
    "backend": [r"\bdjango\b", r"\bnode\.?js\b", r"\brest(ful)?\s*api", r"\bgraphql\b",
                r"api design", r"data model"],
    "infra": [r"\bdocker\b", r"\bci\s?/\s?cd\b", r"\bterraform\b", r"\baws\b",
              r"\bgithub actions\b", r"\bcloud\b"],
    "data": [r"\bpandas\b", r"\bnumpy\b", r"\bjupyter\b", r"\betl\b", r"data pipeline"],
}

# Core language/framework signals for a DIFFERENT primary stack than the
# resume's -- if the description leans heavily on one of these, it's a real
# stack mismatch, not just a missing nice-to-have.
STACK_MISMATCH_SIGNALS = [
    ("Go/Golang", r"\bgo(lang)?\b"),
    ("Ruby/Rails", r"\bruby\b|\brails\b"),
    ("PHP", r"\bphp\b"),
    ("C++", r"c\+\+"),
    ("C#/.NET", r"c#|\.net\b"),
    ("Rust", r"\brust\b"),
    ("Angular", r"\bangular\b"),
    ("Vue", r"\bvue(\.js)?\b"),
]

# "staff" alone means over-leveled (Staff Engineer), but "Member of Technical
# Staff" -- a normal IC title at a lot of AI/research orgs, not a seniority
# signal -- also contains the substring "staff". The negative lookbehind
# keeps "Staff Engineer"/"Senior Staff" flagged while leaving MTS titles alone.
SENIORITY_PATTERNS = [
    ("over", r"principal|founding|tech(nical)?\s+lead|(?<!technical )staff"),
    ("senior", r"senior|\bsr\.?\b"),
    ("junior", r"junior|\bjr\.?\b|entry.?level|new grad|intern"),
]

DOMAIN_BONUS_RE = re.compile(r"biotech|therapeutics|biosciences|genomics|life sciences|pharmaceutical", re.IGNORECASE)


def classify_seniority(title: str) -> str:
    for label, pattern in SENIORITY_PATTERNS:
        if re.search(pattern, title, re.IGNORECASE):
            return label
    return "mid"


def score_posting(title: str, description: str) -> tuple[int, str]:
    text = f"{title}\n{description}"

    matched, gaps = [], []
    for category, patterns in CATEGORY_KEYWORDS.items():
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            matched.append(category)
        else:
            gaps.append(category)

    mismatch = None
    for label, pattern in STACK_MISMATCH_SIGNALS:
        if re.search(pattern, text, re.IGNORECASE):
            mismatch = label
            break

    seniority = classify_seniority(title)
    domain_bonus = bool(DOMAIN_BONUS_RE.search(text))

    score = 20 + 8 * len(matched)
    if mismatch:
        score -= 15
    if seniority == "over":
        score -= 25
    elif seniority == "senior":
        score -= 10
    elif seniority == "junior":
        score -= 8
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
        notes_parts.append("slightly senior-leaning but plausible stretch fit")
    elif seniority == "junior":
        notes_parts.append("likely overqualified (junior/entry-level role)")
    else:
        notes_parts.append("good seniority match (mid-level IC)")
    if domain_bonus:
        notes_parts.append("domain bonus: biotech/life-sciences overlap with candidate background")

    return score, "; ".join(notes_parts)


def main():
    loc_q = build_bay_area_loc_q()
    swe_filter = build_swe_title_q()
    # Same "technical staff" carve-out as SENIORITY_PATTERNS: don't let a
    # Member of Technical Staff title get excluded just because "staff" is
    # a substring of it. Senior is deliberately not excluded here -- classify_seniority
    # below still scores it as a softer "senior-leaning stretch fit" penalty rather
    # than a hard cut, since Senior roles are allowed through now.
    exclude_filter = Q(title__iregex=r"(?<!technical )staff")
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)

    qs = (
        Posting.objects.filter(swe_filter, loc_q, posted_at__gte=cutoff)
        .exclude(exclude_filter)
        .exclude(title__iregex=DISQUALIFY_RE.pattern)
        .select_related("company")
        .distinct()
    )
    print(f"scoring {qs.count()} postings...")

    rows = []
    for p in qs:
        score, notes = score_posting(p.title, p.description)
        rows.append({
            "company": p.company.name or p.company.slug,
            "title": p.title,
            "location": p.location,
            "avg_comp": p.avg_comp or "",
            "lowest_comp": p.lowest_comp or "",
            "workplace_type": p.workplace_type,
            "posted_at": p.posted_at.date().isoformat() if p.posted_at else "",
            "ats_fit_score": score,
            "ats_notes": notes,
            "url": p.url,
        })

    rows.sort(key=lambda r: r["ats_fit_score"], reverse=True)

    fieldnames = ["company", "title", "location", "avg_comp", "lowest_comp",
                  "workplace_type", "posted_at", "ats_fit_score", "ats_notes", "url"]
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} rows to {os.path.abspath(OUT_PATH)}")
    print(f"score distribution: min={rows[-1]['ats_fit_score']}, max={rows[0]['ats_fit_score']}, "
          f"avg={sum(r['ats_fit_score'] for r in rows) / len(rows):.1f}")


if __name__ == "__main__":
    main()
