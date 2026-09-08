import json
import re
import time
from datetime import datetime, timezone

import requests
from django.core.management.base import BaseCommand
from django.db.utils import OperationalError
from django.utils.dateparse import parse_datetime

from comp_parser import parse_comp_range
from job_crawler import RateLimiter
from tracker.location_utils import classify_location, classify_location_dict, is_engineering_title
from tracker.models import Company, Posting

UPSERT_MAX_RETRIES = 4


def retry_on_lock(fn, *args, **kwargs):
    """Retry on 'database is locked'. Django's sqlite busy_timeout (30s)
    already retries internally, but on this machine something (likely
    Defender or another process briefly touching the file -- or, since
    fetch_preferred_companies can now be run as several concurrent
    per-platform processes, a sibling process's own write) has been holding
    the lock past that -- this is a second line of defense so one transient
    lock doesn't kill an hour-long run."""
    backoff = 5.0
    for attempt in range(UPSERT_MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except OperationalError as e:
            if "database is locked" not in str(e) or attempt == UPSERT_MAX_RETRIES - 1:
                raise
            time.sleep(backoff)
            backoff *= 2


def upsert_with_retry(**kwargs):
    return retry_on_lock(Posting.objects.upsert, **kwargs)


def fetch_and_upsert_company(session, company, fetch) -> dict:
    """Fetch + upsert every posting for one company, including location
    normalization and the last_us_engineering_posting_at signal. Shared by
    fetch_postings (the full sweep) and fetch_preferred_companies (the
    tighter-cadence watchlist run) so the two never drift out of sync on
    this logic -- see the gh_jid incident for what duplicated logic costs.

    Returns one of:
      {"error": str}
      {"newly_invalid": True}
      {"empty": True}
      {"postings": int}
    """
    try:
        postings = fetch(session, company.slug)
    except requests.RequestException as e:
        return {"error": str(e)}

    if postings is None:
        company.is_invalid = True
        retry_on_lock(company.save, update_fields=["is_invalid"])
        return {"newly_invalid": True}

    if not postings:
        return {"empty": True}

    total_postings = 0
    us_engineering_seen_at = None
    for p in postings:
        if not p["url"] or not p["title"]:
            continue
        avg_comp, lowest_comp = parse_comp_range(p["description"])
        raw_locations = p.get("locations")
        enriched_locations = (
            [classify_location_dict(loc) for loc in raw_locations]
            if raw_locations is not None else None
        )
        upsert_with_retry(
            company=company,
            title=p["title"],
            url=p["url"],
            location=p["location"],
            description=p["description"],
            avg_comp=avg_comp,
            lowest_comp=lowest_comp,
            posted_at=p.get("posted_at"),
            workplace_type=p.get("workplace_type", ""),
            locations=enriched_locations,
        )
        total_postings += 1

        if is_engineering_title(p["title"]):
            regions = (
                [loc["region"] for loc in enriched_locations] if enriched_locations
                else [classify_location(p["location"]).region]
            )
            if "us" in regions:
                seen_at = p.get("posted_at") or datetime.now(timezone.utc)
                if us_engineering_seen_at is None or seen_at > us_engineering_seen_at:
                    us_engineering_seen_at = seen_at

    if us_engineering_seen_at and (
        company.last_us_engineering_posting_at is None
        or us_engineering_seen_at > company.last_us_engineering_posting_at
    ):
        company.last_us_engineering_posting_at = us_engineering_seen_at
        retry_on_lock(company.save, update_fields=["last_us_engineering_posting_at"])

    return {"postings": total_postings}

USER_AGENT = "job-tracker-crawler/0.1 (personal job search; contact: kdaustin94@gmail.com)"
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def parse_iso(value) -> datetime | None:
    """Parse an ISO-8601 timestamp string (Ashby publishedAt, Greenhouse
    first_published); returns None if missing/unparseable."""
    if not value:
        return None
    try:
        return parse_datetime(value)
    except (TypeError, ValueError):
        return None


def parse_epoch_ms(value) -> datetime | None:
    """Parse a millisecond epoch timestamp (Lever createdAt)."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def parse_epoch_sec(value) -> datetime | None:
    """Parse a second-resolution epoch timestamp (Gem firstPublishedTsSec)."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def single_location(location: str) -> list[dict]:
    """Fallback for platforms that only give us a free-text location string:
    one Location entry, city-only. Better than nothing, and keeps the m2m
    populated consistently across platforms even where only Ashby currently
    has real structured multi-location data."""
    return [{"city": location, "state": "", "country": ""}] if location else []


# Delimiters Greenhouse/Lever use to join genuinely distinct offices in their
# free-text location string -- e.g. "San Francisco, CA | New York City, NY",
# "London, UK; Ontario, CAN", "London & San Francisco". Comma is deliberately
# NOT a split point: both platforms also use it inside a single "City, ST"
# pair ("New York, NY"), and there's no reliable way from the string alone to
# tell "two cities joined by comma" apart from "one city, one state" -- so a
# comma-only combined string is left as one (imperfect) Location entry rather
# than risk shredding a real single location. Only greenhouse/offices' own
# structured `offices` array would resolve that ambiguity properly, but that
# field isn't fetched (or stored) today.
MULTI_LOCATION_SPLIT_RE = re.compile(r"\s*(?:\||;|/|&|\band\b)\s*", re.IGNORECASE)


def split_locations(location: str) -> list[dict]:
    if not location:
        return []
    parts = [p.strip() for p in MULTI_LOCATION_SPLIT_RE.split(location) if p.strip()]
    return [{"city": p, "state": "", "country": ""} for p in parts]


def ashby_locations(j: dict) -> list[dict]:
    """Ashby postings can list a primary location plus any number of
    secondaryLocations (e.g. "San Francisco or New York") -- each with its
    own structured postalAddress when available, falling back to the bare
    location name otherwise."""
    locations = []

    def from_postal_address(addr: dict) -> dict | None:
        city = addr.get("addressLocality")
        if not city:
            return None
        return {"city": city, "state": addr.get("addressRegion", ""), "country": addr.get("addressCountry", "")}

    primary = from_postal_address((j.get("address") or {}).get("postalAddress") or {})
    if primary:
        locations.append(primary)
    elif j.get("location"):
        locations.append({"city": j["location"], "state": "", "country": ""})

    for sec in j.get("secondaryLocations") or []:
        parsed = from_postal_address((sec.get("address") or {}).get("postalAddress") or {})
        if parsed:
            locations.append(parsed)
        elif sec.get("location"):
            locations.append({"city": sec["location"], "state": "", "country": ""})

    return locations


def _extract_app_data(html_text: str) -> dict | None:
    """Pull the `window.__appData = {...};` blob embedded in an Ashby-hosted
    page's server-rendered HTML. Used only by the fetch_ashby_scraped
    fallback below -- json.JSONDecoder().raw_decode from the opening brace
    handles the nested-brace object correctly without needing to hand-match
    the closing brace via regex."""
    marker = "window.__appData = "
    idx = html_text.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    try:
        data, _ = json.JSONDecoder().raw_decode(html_text, start)
        return data
    except ValueError:
        return None


def fetch_ashby_scraped(session, slug):
    """Fallback for Ashby orgs that have disabled their public Job Posting
    API (api.ashbyhq.com/posting-api/...) while leaving their hosted board
    page itself public -- confirmed behavior, not a hypothetical: Whatnot's
    board returns 404 from the public API but jobs.ashbyhq.com/whatnot
    renders a full listing. The board page and each individual posting page
    both server-render the same data Ashby's own frontend uses, as a
    `window.__appData = {...}` JS assignment -- this scrapes that instead of
    the API.

    Two-call pattern like fetch_gem: the board page's `jobPostings` array
    has title/location/comp but no description, so each posting's own page
    is fetched separately for descriptionHtml. No publishedAt-equivalent
    field was found in this data source, so posted_at is always None here
    (degrades gracefully -- the field is nullable on the model).

    Only called when the public API 404s; NOT a general replacement for it,
    since the API is cheaper (one call, includes description) when it's
    available.
    """
    resp = session.get(f"https://jobs.ashbyhq.com/{slug}", timeout=20)
    if resp.status_code != 200:
        return None
    app_data = _extract_app_data(resp.text)
    if app_data is None:
        return None
    postings = (app_data.get("jobBoard") or {}).get("jobPostings")
    if postings is None:
        return None
    if not postings:
        return []

    limiter = RateLimiter(2.0)
    out = []
    for p in postings:
        posting_id = p.get("id")
        if not posting_id:
            continue
        limiter.wait()
        detail_resp = session.get(f"https://jobs.ashbyhq.com/{slug}/{posting_id}", timeout=20)
        if detail_resp.status_code != 200:
            continue
        detail_data = _extract_app_data(detail_resp.text)
        detail = (detail_data or {}).get("posting") or {}
        if not detail.get("isListed", True):
            continue

        location_names = [p.get("locationName", "") or ""]
        location_names += [loc.get("locationName", "") or "" for loc in p.get("secondaryLocations") or []]
        locations = []
        for name in location_names:
            locations.extend(split_locations(name))

        out.append({
            "title": detail.get("title") or p.get("title", ""),
            "location": p.get("locationName", "") or "",
            "url": f"https://jobs.ashbyhq.com/{slug}/{posting_id}",
            "description": strip_html(detail.get("descriptionHtml", "")),
            "posted_at": None,
            "workplace_type": p.get("workplaceType", "") or "",
            "locations": locations,
        })
    return out


def fetch_ashby(session, slug):
    """Returns None if the slug itself is invalid (board doesn't exist), or a
    (possibly empty) list of postings if it's a real, currently-empty board.

    Falls back to fetch_ashby_scraped when the public Job Posting API 404s --
    see that function's docstring for why that happens for some orgs even
    though a real, live board exists.
    """
    resp = session.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=20)
    if resp.status_code != 200:
        return fetch_ashby_scraped(session, slug)
    jobs = resp.json().get("jobs", [])
    out = []
    for j in jobs:
        if not j.get("isListed", True):
            continue
        description = j.get("descriptionPlain") or ""
        out.append({
            "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": j.get("jobUrl", ""),
            "description": description,
            "posted_at": parse_iso(j.get("publishedAt")),
            "workplace_type": j.get("workplaceType", "") or "",
            "locations": ashby_locations(j),
        })
    return out


def fetch_greenhouse(session, slug):
    """Returns None if the slug itself is invalid (board doesn't exist), or a
    (possibly empty) list of postings if it's a real, currently-empty board."""
    resp = session.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"}, timeout=20)
    if resp.status_code != 200:
        return None
    jobs = resp.json().get("jobs", [])
    out = []
    for j in jobs:
        description = strip_html(j.get("content", ""))
        location = (j.get("location") or {}).get("name") or ""
        # Greenhouse has no dedicated workplace-type field; some boards surface
        # it as a free-form metadata entry (e.g. name="Workplace Type").
        workplace_type = ""
        for entry in j.get("metadata") or []:
            name = (entry.get("name") or "").strip().lower()
            if name == "workplace type" and isinstance(entry.get("value"), str):
                workplace_type = entry["value"]
                break
        out.append({
            "title": j.get("title", ""),
            "location": location,
            "url": j.get("absolute_url", ""),
            "description": description,
            "posted_at": parse_iso(j.get("first_published")),
            "workplace_type": workplace_type,
            "locations": split_locations(location),
        })
    return out


def fetch_lever(session, slug):
    """Returns None if the slug itself is invalid (board doesn't exist), or a
    (possibly empty) list of postings if it's a real, currently-empty board."""
    resp = session.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, timeout=20)
    if resp.status_code != 200:
        return None
    jobs = resp.json()
    if not isinstance(jobs, list):
        return None
    out = []
    for j in jobs:
        location = (j.get("categories") or {}).get("location", "") or ""
        description = j.get("descriptionPlain") or strip_html(j.get("description", ""))
        out.append({
            "title": j.get("text", ""),
            "location": location,
            "url": j.get("hostedUrl", ""),
            "description": description,
            "posted_at": parse_epoch_ms(j.get("createdAt")),
            "workplace_type": j.get("workplaceType", "") or "",
            "locations": split_locations(location),
        })
    return out


def fetch_smartrecruiters(session, company_id):
    """Returns None if the slug looks invalid (no postings at all), or a
    (possibly empty) list of postings if there's at least one real one.

    Unlike Ashby/Greenhouse/Lever, SmartRecruiters' API returns HTTP 200 with
    an empty `content` list for *any* company_id string, valid or not -- so
    an empty response can't be trusted as "real board, no current openings"
    the way it can on the other platforms. We treat empty as "not found" to
    avoid creating Company rows off a bare slug guess with nothing behind it.

    The list endpoint doesn't include job body text, so description is
    assembled from the structured department/function/seniority/type fields
    instead of an extra per-posting detail call.
    """
    resp = session.get(f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings", timeout=20)
    if resp.status_code != 200:
        return None
    content = resp.json().get("content") or []
    if not content:
        return None
    out = []
    for j in content:
        location = j.get("location") or {}
        loc_str = location.get("fullLocation") or ", ".join(filter(None, [location.get("city"), location.get("country")]))
        workplace_type = "Remote" if location.get("remote") else ("Hybrid" if location.get("hybrid") else "")
        description = "; ".join(
            (j.get(field) or {}).get("label", "")
            for field in ("department", "function", "experienceLevel", "typeOfEmployment", "industry")
            if (j.get(field) or {}).get("label")
        )
        out.append({
            "title": j.get("name", ""),
            "location": loc_str,
            "url": f"https://jobs.smartrecruiters.com/{company_id}/{j.get('id', '')}",
            "description": description,
            "posted_at": parse_iso(j.get("releasedDate")),
            "workplace_type": workplace_type,
            "locations": [{"city": location.get("city", ""), "state": "", "country": location.get("country", "")}]
                         if location.get("city") else [],
        })
    return out


def _breezy_place_name(value) -> str:
    """A Breezy location's state/country is usually {"name": "..."}, but at
    least one board (discovered scaling this up to breezy's full company
    list) returns a bare string instead -- handle both shapes rather than
    assume the structured one always holds."""
    if isinstance(value, dict):
        return value.get("name", "") or ""
    if isinstance(value, str):
        return value
    return ""


def breezy_locations(j: dict) -> list[dict]:
    """Breezy already gives structured per-location country/state/city (even
    for remote listings, which still carry the office city alongside
    is_remote=true) -- passed straight through as trusted structured data,
    same shape as ashby_locations."""
    out = []
    for loc in j.get("locations") or []:
        out.append({
            "city": loc.get("city", "") or "",
            "state": _breezy_place_name(loc.get("state")),
            "country": _breezy_place_name(loc.get("country")),
        })
    return out


def fetch_breezy(session, slug):
    """Returns None if the slug itself is invalid (no board at that
    subdomain), or a (possibly empty) list of postings if it's a real,
    currently-empty board.

    Breezy boards live at a subdomain (`{slug}.breezy.hr`), not a path under
    a shared domain like the other platforms -- the slug goes into the host,
    not the URL path.

    Unlike Ashby/Greenhouse/Lever, the public `/json` list endpoint doesn't
    include job body text, and (unlike SmartRecruiters) there's no
    structured department/seniority breakdown to assemble a substitute
    description from either -- the per-posting page is a client-rendered
    Angular app with no discovered JSON detail endpoint, so description is
    left blank rather than scraping rendered HTML per posting. Same tradeoff
    fetch_workable.py makes and documents for the same reason.
    """
    resp = session.get(f"https://{slug}.breezy.hr/json", timeout=20)
    if resp.status_code != 200:
        return None
    try:
        jobs = resp.json()
    except ValueError:
        return None
    if not isinstance(jobs, list):
        return None
    out = []
    for j in jobs:
        primary = j.get("location") or {}
        out.append({
            "title": j.get("name", ""),
            "location": primary.get("name", "") or "",
            "url": j.get("url", ""),
            "description": "",
            "posted_at": parse_iso(j.get("published_date")),
            "workplace_type": "Remote" if primary.get("is_remote") else "",
            "locations": breezy_locations(j),
        })
    return out


def parse_ymd(value) -> datetime | None:
    """Parse a YYYY-MM-DD date string (Workable published_on)."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_workable(session, account):
    """Returns None if the account looks invalid (404, or 200 with no jobs),
    or a non-empty list of postings if there's at least one real one.

    Like SmartRecruiters (see fetch_smartrecruiters), Workable's widget API
    returns HTTP 200 for a lot of account strings that were never a
    deliberate company slug -- e.g. probing a garbage/guessed slug like
    "500ms;" or "01c" returns 200 with an unrelated real (if dormant) account
    and an empty jobs list, not a 404. Confirmed empirically: guessing slugs
    from ~50 unrelated dead Ashby boards against Workable produced 21 "hits",
    18 of which were this exact false-positive shape. So empty can't be
    trusted as "real board, no current openings" here either -- treated as
    not-found to avoid onboarding phantom companies off a coincidental string
    match.

    The public widget API only exposes job metadata (title, location,
    department), not the job body text -- description is left blank rather
    than doing a second HTML-scrape fetch per posting.
    """
    resp = session.get(f"https://apply.workable.com/api/v1/widget/accounts/{account}", timeout=20)
    if resp.status_code != 200:
        return None
    jobs = resp.json().get("jobs", [])
    if not jobs:
        return None
    out = []
    for j in jobs:
        location = ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))
        out.append({
            "title": j.get("title", ""),
            "location": location,
            "url": j.get("url") or j.get("shortlink") or "",
            "description": "",
            "posted_at": parse_ymd(j.get("published_on") or j.get("created_at")),
            "workplace_type": "Remote" if j.get("telecommuting") else "",
            "locations": single_location(location),
        })
    return out


GEM_GRAPHQL_URL = "https://jobs.gem.com/api/public/graphql"

# Gem's job board is a client-rendered SPA with no REST endpoint -- these two
# queries are lifted from its public bundle (static.gem.com/scripts/
# ExternalJobBoardList.*.js and jobBoards.*.js). No auth/CSRF token is
# required; both were confirmed working unauthenticated against a live board.
GEM_LIST_QUERY = """
query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings {
      extId
      title
    }
  }
  jobBoardExternal(vanityUrlPath: $boardId) {
    id
  }
}
"""

GEM_DETAIL_QUERY = """
query ExternalJobPostingQuery($boardId: String!, $extId: String!) {
  oatsExternalJobPosting(boardId: $boardId, extId: $extId) {
    title
    descriptionHtml
    firstPublishedTsSec
    locations {
      name
      city
      isoCountry
    }
    job {
      locationType
      department {
        name
      }
    }
  }
}
"""

GEM_LOCATION_TYPE = {"REMOTE": "Remote", "HYBRID": "Hybrid", "IN_OFFICE": "Onsite"}


def _gem_graphql(session, operation_name, query, variables):
    resp = session.post(
        GEM_GRAPHQL_URL,
        json={"operationName": operation_name, "query": query, "variables": variables},
        timeout=20,
    )
    if resp.status_code != 200:
        return None
    return resp.json().get("data")


def fetch_gem(session, slug):
    """Returns None if the slug itself is invalid (board doesn't exist), or a
    (possibly empty) list of postings if it's a real, currently-empty board.

    Unlike the other platforms, listing postings and reading a posting's
    description are two separate GraphQL queries -- there's no single call
    that returns both. So this issues one list call plus one detail call per
    posting, self-rate-limited between the detail calls since the outer
    fetch_and_upsert_company loop only paces between companies, not within
    one company's own requests.
    """
    data = _gem_graphql(session, "JobBoardList", GEM_LIST_QUERY, {"boardId": slug})
    if data is None or data.get("jobBoardExternal") is None:
        return None

    postings = data["oatsExternalJobPostings"]["jobPostings"]
    if not postings:
        return []

    limiter = RateLimiter(3.0)
    out = []
    for p in postings:
        ext_id = p["extId"]
        limiter.wait()
        detail = _gem_graphql(session, "ExternalJobPostingQuery", GEM_DETAIL_QUERY, {"boardId": slug, "extId": ext_id})
        posting = (detail or {}).get("oatsExternalJobPosting")
        if not posting:
            continue

        locations = [
            {"city": loc.get("city") or loc.get("name") or "", "state": "", "country": loc.get("isoCountry") or ""}
            for loc in (posting.get("locations") or [])
        ]
        location = ", ".join(loc["city"] for loc in locations if loc["city"])
        job = posting.get("job") or {}
        out.append({
            "title": posting.get("title", ""),
            "location": location,
            "url": f"https://jobs.gem.com/{slug}/{ext_id}",
            "description": strip_html(posting.get("descriptionHtml", "")),
            "posted_at": parse_epoch_sec(posting.get("firstPublishedTsSec")),
            "workplace_type": GEM_LOCATION_TYPE.get(job.get("locationType") or "", ""),
            "locations": locations,
        })
    return out


def _extract_next_data(html_text: str) -> dict | None:
    """Pull the `<script id="__NEXT_DATA__" type="application/json">{...}</script>`
    blob Next.js embeds in every server-rendered page -- used by fetch_rippling
    below. Simpler than Ashby's window.__appData JS-assignment scrape (this is
    already a plain <script> tag with valid, self-contained JSON), but same
    idea: read the actual data the frontend was hydrated with instead of
    calling an API the frontend itself doesn't expose separately."""
    marker = '<script id="__NEXT_DATA__" type="application/json">'
    start = html_text.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = html_text.find("</script>", start)
    if end == -1:
        return None
    try:
        return json.loads(html_text[start:end])
    except ValueError:
        return None


RIPPLING_WORKPLACE_TYPE = {"REMOTE": "Remote", "HYBRID": "Hybrid", "ONSITE": "Onsite"}
RIPPLING_PAGE_SIZE = 20  # matches the board page's own default -- see fetch_rippling
RIPPLING_MAX_PAGES = 25  # safety cap (== 500 postings) in case pagination guessing misbehaves


def rippling_locations(job: dict) -> list[dict]:
    """Rippling gives fully structured locations (country name + 2-letter
    state code, same convention Location.state uses elsewhere) -- passed
    through as trusted structured data, same shape as ashby_locations."""
    return [
        {
            "city": loc.get("city") or "",
            "state": loc.get("stateCode") or loc.get("state") or "",
            "country": loc.get("country") or "",
        }
        for loc in job.get("locations") or []
    ]


def _rippling_job_posts_query(next_data: dict) -> dict | None:
    """Find the react-query cache entry for the job list among the board
    page's dehydrated queries -- keyed ['board', <slug>, 'job-posts', ...],
    the rest of the key being the filter/pagination params the page was
    rendered with. Returns its `data` payload ({"items", "page", "totalPages",
    ...}), or None if the board doesn't exist at all (see fetch_rippling)."""
    queries = (((next_data.get("props") or {}).get("pageProps") or {}).get("dehydratedState") or {}).get("queries") or []
    for q in queries:
        key = q.get("queryKey") or []
        if len(key) >= 3 and key[0] == "board" and key[2] == "job-posts":
            return (q.get("state") or {}).get("data")
    return None


def fetch_rippling(session, slug):
    """Returns None if the slug itself is invalid (no board at that path), or
    a (possibly empty) list of postings if it's a real, currently-empty board.

    Unlike every other platform here, there's no separate API endpoint at
    all -- ats.rippling.com/<slug>/jobs is a server-rendered Next.js page
    with the full first page of job-posts already embedded as a
    __NEXT_DATA__ JSON blob (see _extract_next_data), and each posting's own
    page (.../jobs/<id>) embeds its full description the same way. Two plain
    GETs per posting, no auth/session/CSRF needed -- confirmed working
    unauthenticated against a live board (QuotaPath, found via a Consider
    VC-portfolio board -- see discover_consider_board.py).

    Pagination beyond the first page is a best-effort guess (?page=N on the
    same board URL causing the SSR to re-render with that page's data,
    following the same page/pageSize the embedded query itself reports) --
    no company with >RIPPLING_PAGE_SIZE openings has been seen to confirm
    this, so it's capped and stops cleanly (keeping page 0's results) if a
    page doesn't look like real forward progress.
    """
    board_url = f"https://ats.rippling.com/{slug}/jobs"
    resp = session.get(board_url, timeout=20)
    if resp.status_code != 200:
        return None
    next_data = _extract_next_data(resp.text)
    if next_data is None:
        return None
    api_data = ((next_data.get("props") or {}).get("pageProps") or {}).get("apiData") or {}
    if not api_data.get("jobBoard"):
        return None  # a real Next.js page, but not an ATS job board (e.g. rippling.com's own marketing site)

    job_posts = _rippling_job_posts_query(next_data)
    if not job_posts:
        return []
    items = list(job_posts.get("items") or [])

    total_pages = job_posts.get("totalPages") or 1
    for page in range(1, min(total_pages, RIPPLING_MAX_PAGES)):
        resp = session.get(board_url, params={"page": page}, timeout=20)
        if resp.status_code != 200:
            break
        page_data = _extract_next_data(resp.text)
        more = _rippling_job_posts_query(page_data) if page_data else None
        more_items = (more or {}).get("items") or []
        if not more_items or more_items == items[-len(more_items):]:
            break  # page param didn't do anything -- stop rather than loop on duplicate data
        items.extend(more_items)

    limiter = RateLimiter(3.0)
    out = []
    for item in items:
        job_id = item.get("id")
        if not job_id:
            continue
        limiter.wait()
        detail_resp = session.get(f"{board_url}/{job_id}", timeout=20)
        if detail_resp.status_code != 200:
            continue
        detail_data = _extract_next_data(detail_resp.text)
        job_post = (((detail_data or {}).get("props") or {}).get("pageProps") or {}).get("apiData", {}).get("jobPost") or {}
        description_sections = job_post.get("description") or {}
        description = strip_html(" ".join(v for v in description_sections.values() if isinstance(v, str)))

        locations = rippling_locations(item)
        location = ", ".join(loc["city"] or loc["state"] for loc in locations if loc["city"] or loc["state"])
        workplace_types = {l.get("workplaceType") for l in item.get("locations") or []}
        workplace_type = RIPPLING_WORKPLACE_TYPE.get(next(iter(workplace_types), ""), "") if len(workplace_types) == 1 else ""

        out.append({
            "title": item.get("name", ""),
            "location": location,
            "url": item.get("url") or f"{board_url}/{job_id}",
            "description": description,
            "posted_at": None,  # not present anywhere in the board or detail payload
            "workplace_type": workplace_type,
            "locations": locations,
        })
    return out


FETCHERS = {
    "ashby": fetch_ashby,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
    "gem": fetch_gem,
    "breezy": fetch_breezy,
    "rippling": fetch_rippling,
}

# Conservative request rates, each set below the platform's own documented or
# observed ceiling so we're a polite citizen of someone else's free API:
#   - Ashby: no official published limit; unofficial/observed ceiling is
#     ~100 req/min (~1.67/s). https://developers.ashbyhq.com/docs/public-job-posting-api
#   - Greenhouse: Job Board API is explicitly "not rate limited... but
#     throttles abusive callers hammering many boards in tight loops."
#     Their authenticated Harvest API caps at 5 req/s, used here as a proxy
#     for their general tolerance. https://developers.greenhouse.io/job-board.html
#   - Lever: documented 10 req/s steady-state / 20 req/s burst (token bucket)
#     for the general API. The much stricter 2 req/s limit only applies to
#     application-submission POSTs, not the postings GET endpoint we use.
#     https://github.com/lever/postings-api
#   - SmartRecruiters / Workable / Breezy: no published limits found; kept
#     conservative since these are newer, lower-confidence additions to this
#     pipeline.
#   - Gem: no public API docs at all (reverse-engineered from their SPA
#     bundle), and unlike the others this is N+1 requests per company (one
#     list call, one detail call per posting) instead of one. This rate
#     paces the between-company calls; fetch_gem's own internal limiter
#     (3.0 req/s) separately paces the per-posting detail calls.
#   - Rippling: no public API docs (there's no API at all -- see
#     fetch_rippling's docstring); same N+1-per-company shape as Gem, kept
#     at the same conservative rate for the same reason.
PLATFORM_RPS = {
    "ashby": 3.0,
    "greenhouse": 6.0,
    "lever": 10.0,
    "smartrecruiters": 3.0,
    "workable": 3.0,
    "gem": 2.0,
    "breezy": 3.0,
    "rippling": 2.0,
}


class Command(BaseCommand):
    help = "Fetch live postings for each Company from its ATS's public JSON API and upsert into Posting."

    def add_arguments(self, parser):
        parser.add_argument("--platform", choices=list(FETCHERS.keys()), default=None)
        parser.add_argument("--rps", type=float, default=None, help="override the per-platform default rate limit")
        parser.add_argument("--limit", type=int, default=None, help="only process the first N companies (per platform)")
        parser.add_argument(
            "--company-ids-file", default=None,
            help="path to a file of newline-separated Company ids -- restricts the run to just those companies "
                 "instead of every company on the platform",
        )

    def handle(self, *args, **options):
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT

        platforms = [options["platform"]] if options["platform"] else list(FETCHERS.keys())

        company_ids = None
        if options["company_ids_file"]:
            with open(options["company_ids_file"]) as f:
                company_ids = [int(line) for line in f if line.strip()]

        for platform in platforms:
            fetch = FETCHERS[platform]
            rps = options["rps"] or PLATFORM_RPS[platform]
            limiter = RateLimiter(rps)
            self.stdout.write(f"[{platform}] rate limit: {rps} req/s")
            companies = Company.objects.filter(ats_platform=platform, is_invalid=False).order_by("slug")
            if company_ids is not None:
                companies = companies.filter(id__in=company_ids)
            skipped_invalid = Company.objects.filter(ats_platform=platform, is_invalid=True).count()
            if skipped_invalid:
                self.stdout.write(f"[{platform}] skipping {skipped_invalid} already-known-invalid companies")
            if options["limit"]:
                companies = companies[: options["limit"]]

            total_companies = 0
            total_postings = 0
            empty_boards = 0
            newly_invalid = 0
            start_time = time.monotonic()
            last_progress_time = start_time
            PROGRESS_INTERVAL_SECONDS = 30

            for company in companies:
                total_companies += 1
                limiter.wait()
                result = fetch_and_upsert_company(session, company, fetch)

                if "error" in result:
                    self.stderr.write(f"[{platform}] {company.slug}: request failed ({result['error']})")
                    continue
                if result.get("newly_invalid"):
                    newly_invalid += 1
                    continue
                if result.get("empty"):
                    empty_boards += 1
                    continue
                total_postings += result["postings"]

                now = time.monotonic()
                if now - last_progress_time >= PROGRESS_INTERVAL_SECONDS:
                    last_progress_time = now
                    elapsed = now - start_time
                    rate = total_companies / elapsed * 60 if elapsed else 0
                    remaining = len(companies) - total_companies
                    eta_min = remaining / rate if rate else 0
                    self.stdout.write(
                        f"[{platform}] {total_companies}/{len(companies)} companies "
                        f"({elapsed / 60:.1f}m elapsed, {rate:.0f}/min, ~{eta_min:.0f}m left), "
                        f"{total_postings} postings so far, {empty_boards} empty boards, "
                        f"{newly_invalid} newly invalid"
                    )
                    self.stdout.flush()

            self.stdout.write(
                f"[{platform}] done: {total_companies} companies checked, "
                f"{total_postings} postings upserted, {empty_boards} empty (valid, no current postings), "
                f"{newly_invalid} newly marked invalid"
            )
            self.stdout.flush()
