"""
Pulls job listings off the custom company careers pages located by
locate_custom_careers.py (rows with status=custom_career_page_found in
unmatched_companies_careers.csv) and saves them locally as Posting rows,
same as fetch_postings.py does for ATS-API companies. No LLM calls -- pure
heuristics, so it needs no API key and costs nothing to run.

Two extraction paths per page, tried in order:

1. ATS-embed detection: some "custom" pages are just a company's own
   careers page embedding a widget from a platform this project can already
   pull structured data from (Greenhouse/Lever/Ashby/SmartRecruiters/
   Workable) -- locate_custom_careers.py's CDX search missed these because
   it only looks at same-domain URLs, not cross-domain embeds. If the
   rendered page's HTML references one of those platforms' known domains,
   the slug is pulled out and fetched through the real structured API (the
   same fetchers fetch_postings.py uses) instead of guessed from link text
   -- strictly better data, and the Company gets stored under its real
   ats_platform going forward instead of "custom".

2. Generic link-pattern fallback: for everything else, filter the page's
   links down to ones whose path looks like an individual job posting
   (…/careers/senior-backend-engineer, not …/careers or …/about) and whose
   link text isn't obvious nav/footer chrome. No description/location/comp
   without visiting each job's own page (not done here) -- title + URL
   only. This is intentionally coarse: some real postings will be missed,
   some false positives will get through. Re-run score_bay_area_postings.py
   (or similar) afterward to see how these actually check out.

Runs in batches (default 50 domains). Each batch scrapes with its own
Playwright session, then closes it before touching the DB -- Playwright's
sync API keeps a background event loop alive for its whole `with` block,
and Django refuses ORM queries while it detects a running loop on the
current thread (SynchronousOnlyOperation). Resume-safe like
locate_custom_careers.py: skips domains already logged in --out, so a
killed/restarted run doesn't lose progress or redo work.

Run: python manage.py scrape_custom_careers
     python manage.py scrape_custom_careers --limit 100 --batch-size 50
"""

import csv
import os
import re
import sys
import time

import requests
from django.core.management.base import BaseCommand
from django.db.utils import OperationalError
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from comp_parser import parse_comp_range
from job_crawler import RateLimiter
from tracker.management.commands.fetch_postings import FETCHERS, single_location
from tracker.models import Company, Posting

USER_AGENT = "job-tracker-crawler/0.1 (personal job search; contact: kdaustin94@gmail.com)"
FIELDNAMES = ["domain", "careers_url", "status", "platform", "postings_found", "notes"]
UPSERT_MAX_RETRIES = 4

EXTRACT_JS = """
() => {
  const seen = new Set();
  const links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    // Many job-board cards wrap title + badges (employment type, department,
    // location) in a single <a>, so innerText is everything concatenated.
    // innerText preserves block-level line breaks, so the title is reliably
    // the first line and, when there's a trailing badge line, it's often
    // the location -- keep both instead of collapsing to one blob.
    const lines = (a.innerText || a.textContent || '').split('\\n').map(s => s.trim()).filter(Boolean);
    const href = a.href;
    if (!href || seen.has(href) || lines.length === 0) continue;
    seen.add(href);
    links.push({
      text: lines[0].slice(0, 200),
      lastLine: lines.length > 1 ? lines[lines.length - 1].slice(0, 100) : '',
      href,
    });
    if (links.length >= 500) break;
  }
  return { links, html: document.documentElement.outerHTML.slice(0, 300000) };
}
"""

# Known ATS domains this project already knows how to fetch structured data
# from -- if a "custom" page just embeds one of these, use the real API
# instead of guessing from link text. Pattern captures the board slug.
# Not exhaustive (e.g. <company>.workable.com custom subdomains aren't
# covered) -- a miss here just falls through to the generic heuristic below.
ATS_EMBED_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([a-zA-Z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?for=([a-zA-Z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([a-zA-Z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)")),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([a-zA-Z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([a-zA-Z0-9_-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v\d+/widget/accounts/)?([a-zA-Z0-9_-]+)")),
]

# Link text that's almost never a job title -- filters out obvious nav/
# footer/social chrome before the path-shape heuristic even runs.
NAV_TEXT_RE = re.compile(
    r"^(home|about( us)?|contact( us)?|blog|news|press|login|log in|sign in|sign up|"
    r"privacy( policy)?|terms( of service)?|cookies?( policy)?|pricing|product|products|"
    r"docs|documentation|support|faq|status|security|investors|careers|jobs|"
    r"see all( jobs| openings| positions)?|view all|learn more|apply( now)?|"
    r"twitter|linkedin|facebook|instagram|youtube|github|subscribe|newsletter)$",
    re.IGNORECASE,
)

# Common non-job slugs that otherwise satisfy the path-shape regex below
# (e.g. "/careers/students" or "/careers/life-at-company").
NAV_SLUG_BLOCKLIST = {
    "apply", "faq", "benefits", "culture", "diversity", "life", "students",
    "interns", "internships", "alumni", "team", "about", "contact", "login",
    "signin", "signup", "life-at-company", "why-us", "our-culture", "perks",
}

# A job-posting URL under a careers-ish path, with a slug-shaped trailing
# segment (not just "/careers" or "/careers/engineering" -- a bare
# category). Requires a hyphen or 5+ alnum chars in the last segment so
# broad category links don't get swept in as postings.
JOB_PATH_RE = re.compile(
    r"/(?:careers?|jobs?|positions?|openings?|join(?:-us|-the-team)?)/"
    r"([a-z0-9]+(?:-[a-z0-9]+){1,}|[a-z0-9]{5,})/?(?:[?#].*)?$",
    re.IGNORECASE,
)

# Second, independent signal: the link TEXT itself looks like a job title
# ("Senior Backend Engineer", "Product Designer"), regardless of URL shape.
# Needed because plenty of real postings link out to a third-party board
# with a URL that doesn't match JOB_PATH_RE at all -- e.g. a non-English
# path like "/posao/Full-Stack-Developer/..." on a local job board. Requires
# at least one qualifier/word before the role noun so bare nav text like
# "Support" or "Sales" (single word, no role noun attached) can't match.
JOB_TITLE_TEXT_RE = re.compile(
    r"\b[\w&/-]+\s+(?:Engineer|Developer|Manager|Director|Designer|Analyst|"
    r"Architect|Scientist|Specialist|Coordinator|Associate|Representative|"
    r"Recruiter|Researcher|Consultant|Intern)s?\b"
)


def upsert_with_retry(**kwargs):
    """Retry on 'database is locked', same rationale as fetch_postings.py."""
    backoff = 5.0
    for attempt in range(UPSERT_MAX_RETRIES):
        try:
            return Posting.objects.upsert(**kwargs)
        except OperationalError as e:
            if "database is locked" not in str(e) or attempt == UPSERT_MAX_RETRIES - 1:
                raise
            time.sleep(backoff)
            backoff *= 2


def detect_ats_embed(html):
    for platform, pattern in ATS_EMBED_PATTERNS:
        match = pattern.search(html)
        if match:
            return platform, match.group(1)
    return None, None


def _title_from_slug(url):
    match = JOB_PATH_RE.search(url)
    if not match:
        return ""
    words = [w for w in match.group(1).split("-") if not w.isdigit()]
    return " ".join(w.capitalize() for w in words) if words else ""


def extract_job_links(links):
    """Heuristic fallback: links that look like an individual job posting,
    matched either by URL shape (JOB_PATH_RE) or by the link text itself
    reading like a job title (JOB_TITLE_TEXT_RE) -- a real posting only
    needs one of the two, since some link out to third-party boards whose
    URL shape gives no signal at all (see module docstring)."""
    out = []
    seen_urls = set()
    for link in links:
        href = link["href"]
        # Icon-font glyphs (private-use-area chars) commonly prefix nav link
        # text with no separating whitespace -- strip leading non-word noise
        # so neither matching nor the stored title carries it through.
        text = re.sub(r"^[\W_]+", "", link["text"]).strip()
        if href in seen_urls or not href.startswith(("http://", "https://")):
            continue
        if text and NAV_TEXT_RE.match(text):
            continue
        # Marketing copy like "For Developers" or "Built for Engineers"
        # satisfies JOB_TITLE_TEXT_RE's [word]+role-noun shape too.
        if text and re.match(r"^(built |made |designed |perfect )?for\s+\w", text, re.I):
            continue

        path_match = JOB_PATH_RE.search(href)
        if path_match and path_match.group(1).lower().replace("-", "") in NAV_SLUG_BLOCKLIST:
            path_match = None
        title_match = JOB_TITLE_TEXT_RE.search(text) if text else None
        if not path_match and not title_match:
            continue

        seen_urls.add(href)
        title = text if text and len(text) > 2 else _title_from_slug(href)
        if not title:
            continue
        # The card's trailing line is often a location badge -- but only
        # trust it if it doesn't itself look like another job title (a sign
        # this is actually a list of multiple postings concatenated, not a
        # title+badges card for one).
        last_line = link.get("lastLine", "")
        location = last_line if last_line and not JOB_TITLE_TEXT_RE.search(last_line) else ""
        out.append({"title": title, "url": href, "location": location})
    return out


class Command(BaseCommand):
    help = "Scrape job listings off located custom careers pages (no LLM) and save them as Postings, in batches."

    def add_arguments(self, parser):
        parser.add_argument("--in", dest="in_path", default="unmatched_companies_careers.csv")
        parser.add_argument("--out", default="scrape_custom_careers_log.csv")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--batch-size", type=int, default=50)
        parser.add_argument("--rps", type=float, default=1.0, help="page-load rate (default: 1/s)")
        parser.add_argument("--timeout", type=float, default=20, help="page load timeout in seconds (default: 20)")

    def handle(self, *args, **options):
        in_path = options["in_path"]
        if not os.path.exists(in_path):
            self.stderr.write(f"{in_path} not found -- run locate_custom_careers first.")
            return
        with open(in_path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["status"] == "custom_career_page_found"]

        seen_domains = set()
        targets = []
        for r in rows:
            if r["domain"] in seen_domains:
                continue
            seen_domains.add(r["domain"])
            targets.append(r)

        out_path = options["out"]
        already_done = set()
        if os.path.exists(out_path):
            with open(out_path, newline="", encoding="utf-8") as f:
                already_done = {row["domain"] for row in csv.DictReader(f)}
            self.stdout.write(f"Resuming: {len(already_done)} domains already logged in {out_path}")

        remaining = [r for r in targets if r["domain"] not in already_done]
        if options["limit"]:
            remaining = remaining[: options["limit"]]

        self.stdout.write(f"{len(targets)} distinct careers pages found; {len(remaining)} left to scrape.")
        if not remaining:
            return

        write_header = not os.path.exists(out_path)
        out_file = open(out_path, "a", newline="", encoding="utf-8")
        writer = csv.DictWriter(out_file, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
            out_file.flush()

        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        limiter = RateLimiter(options["rps"])
        timeout_ms = options["timeout"] * 1000
        batch_size = options["batch_size"]
        total_postings = total_via_ats = 0
        start = time.monotonic()

        for batch_start in range(0, len(remaining), batch_size):
            batch = remaining[batch_start: batch_start + batch_size]
            batch_num = batch_start // batch_size + 1
            self.stdout.write(f"\n--- batch {batch_num}: {len(batch)} domains ---")
            sys.stdout.flush()

            # Phase 1: scrape with Playwright, fully closing it before any DB access.
            scraped = []
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(user_agent=USER_AGENT)
                for row in batch:
                    url = row["careers_url"]
                    limiter.wait()
                    status, notes, page_data = "ok", "", None
                    page = context.new_page()
                    try:
                        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                        page.wait_for_timeout(1000)  # let client-rendered listings (React/etc.) mount
                        page_data = page.evaluate(EXTRACT_JS)
                    except PlaywrightError as e:
                        status, notes = "load_error", str(e).splitlines()[0]
                    page.close()
                    scraped.append((row, page_data, status, notes))
                browser.close()

            # Phase 2: ATS-embed fetch or heuristic extraction + DB upsert.
            for i, (row, page_data, status, notes) in enumerate(scraped, 1):
                domain, url, name = row["domain"], row["careers_url"], row["name"]
                n_found, platform_used = 0, ""

                if page_data is not None:
                    ats_platform, ats_slug = detect_ats_embed(page_data["html"])
                    if ats_platform:
                        limiter.wait()
                        try:
                            ats_postings = FETCHERS[ats_platform](session, ats_slug)
                        except requests.RequestException:
                            ats_postings = None
                        if ats_postings is None:
                            ats_platform = None  # embed detected but slug didn't resolve -- fall back
                        else:
                            platform_used = ats_platform
                            company, _ = Company.objects.get_or_create(
                                ats_platform=ats_platform, slug=ats_slug, defaults={"name": name},
                            )
                            for p in ats_postings:
                                if not p["url"] or not p["title"]:
                                    continue
                                avg_comp, lowest_comp = parse_comp_range(p["description"])
                                upsert_with_retry(
                                    company=company, title=p["title"], url=p["url"],
                                    location=p["location"], description=p["description"],
                                    avg_comp=avg_comp, lowest_comp=lowest_comp,
                                    posted_at=p.get("posted_at"), workplace_type=p.get("workplace_type", ""),
                                    locations=p.get("locations"),
                                )
                            n_found = len(ats_postings)

                    if not ats_platform:
                        platform_used = "custom"
                        job_links = extract_job_links(page_data["links"])
                        company, _ = Company.objects.get_or_create(
                            ats_platform="custom", slug=domain, defaults={"name": name},
                        )
                        for p in job_links:
                            location = p.get("location", "")
                            upsert_with_retry(
                                company=company, title=p["title"], url=p["url"],
                                location=location, description="", avg_comp=None, lowest_comp=None,
                                workplace_type="", locations=single_location(location),
                            )
                        n_found = len(job_links)

                total_postings += n_found
                if platform_used and platform_used != "custom":
                    total_via_ats += n_found

                writer.writerow({
                    "domain": domain, "careers_url": url, "status": status,
                    "platform": platform_used, "postings_found": n_found, "notes": notes,
                })
                out_file.flush()

                idx = batch_start + i
                if status == "ok":
                    tag = f"[{platform_used}]" if platform_used else "[no form/links found]"
                    self.stdout.write(f"[{idx}/{len(remaining)}] {name} {tag}: {n_found} posting(s)")
                else:
                    self.stdout.write(f"[{idx}/{len(remaining)}] {name}: {status} ({notes})")
                sys.stdout.flush()

        out_file.close()
        elapsed = time.monotonic() - start
        self.stdout.write(
            f"\nDone: {len(remaining)} companies checked in {elapsed / 60:.1f}m, "
            f"{total_postings} postings saved ({total_via_ats} via a detected real ATS embed). Log at {out_path}"
        )
