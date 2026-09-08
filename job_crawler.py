"""
Job Board Discovery Crawler
----------------------------
Finds companies (and their job-posting URLs) hosted on Ashby, Greenhouse,
Lever, and Gem by querying the Wayback Machine CDX Server API:
https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server

Each run is incremental: it stores the newest capture timestamp seen per
target in crawler_state.json and passes it back as `from=` on the next run,
so repeat runs only fetch what's new since last time instead of re-scanning
the whole index.

Run directly: python job_crawler.py
              python job_crawler.py --rps 2
              python job_crawler.py --target ashby
"""

import argparse
import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass

import requests

from config import BASE_DIR
from slug_filters import is_garbage_slug

log = logging.getLogger(__name__)

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
STATE_PATH = os.path.join(BASE_DIR, "crawler_state.json")
USER_AGENT = "job-tracker-crawler/0.1 (personal job search; contact: kdaustin94@gmail.com)"


class RateLimiter:
    """Enforces a minimum interval between successive requests."""

    def __init__(self, requests_per_second: float = 1.0):
        self.min_interval = 1.0 / requests_per_second
        self._last_call = 0.0

    def wait(self):
        remaining = self.min_interval - (time.monotonic() - self._last_call)
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()


@dataclass(frozen=True)
class Target:
    """One ATS platform to discover companies on via the CDX index."""

    name: str
    url_pattern: str  # value for the CDX `url` param
    match_type: str  # "domain" or "prefix"
    slug_regex: str  # first capture group is the company slug


TARGETS = [
    Target(
        name="ashby",
        url_pattern="jobs.ashbyhq.com",
        match_type="domain",
        slug_regex=r"^https?://jobs\.ashbyhq\.com/([^/?]+)",
    ),
    Target(
        name="lever",
        url_pattern="jobs.lever.co",
        match_type="domain",
        slug_regex=r"^https?://jobs\.lever\.co/([^/?]+)",
    ),
    Target(
        # Greenhouse's embed widget gets captured whenever Wayback crawls ANY
        # company career page that embeds it, not just direct links to
        # boards.greenhouse.io -- much wider discovery surface than the
        # board domain alone.
        name="greenhouse",
        url_pattern="boards.greenhouse.io/embed/job_board/js",
        match_type="prefix",
        slug_regex=r"[?&]for=([^&]+)",
    ),
    Target(
        # Greenhouse's default public job-board URL since their ~2023
        # rebrand -- companies that only ever linked to
        # job-boards.greenhouse.io/<slug> directly (not the embed widget
        # above) were invisible to this crawler entirely. Kept as its own
        # target (own crawler_state.json bookkeeping/pagination) but feeds
        # into the same "greenhouse" ATS platform on import -- see
        # PLATFORM_ALIASES in import_companies.py -- since it's the same
        # underlying boards-api.greenhouse.io backend either way.
        name="greenhouse-jobboards",
        url_pattern="job-boards.greenhouse.io",
        match_type="domain",
        slug_regex=r"^https?://job-boards\.greenhouse\.io/([^/?]+)",
    ),
    Target(
        name="gem",
        url_pattern="jobs.gem.com",
        match_type="domain",
        slug_regex=r"^https?://jobs\.gem\.com/([^/?]+)",
    ),
    Target(
        # Breezy boards live at a subdomain (<slug>.breezy.hr) rather than a
        # path under a shared domain -- "domain" matchType against the bare
        # apex still covers every subdomain, so this needs no "prefix"
        # special-casing the way the Greenhouse embed widget does.
        name="breezy",
        url_pattern="breezy.hr",
        match_type="domain",
        slug_regex=r"^https?://([^./]+)\.breezy\.hr",
    ),
]


class WaybackCDXClient:
    """Minimal, rate-limited, paginated client for the CDX Server API."""

    def __init__(self, requests_per_second: float = 1.0, max_retries: int = 5):
        self.limiter = RateLimiter(requests_per_second)
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def query(self, target: Target, since: str | None = None):
        """Yield CDX rows for a target, optionally only rows captured on/after `since`."""
        numpages = self._numpages(target)
        base_params = {
            "url": target.url_pattern,
            "matchType": target.match_type,
            "output": "json",
            "filter": "statuscode:200",
            "collapse": "urlkey",
        }
        if since:
            base_params["from"] = since

        for page in range(numpages):
            rows = self._get_json(dict(base_params, page=str(page)))
            if len(rows) < 2:
                continue
            header = rows[0]
            for row in rows[1:]:
                yield dict(zip(header, row))

    def _numpages(self, target: Target) -> int:
        rows = self._get_json({
            "url": target.url_pattern,
            "matchType": target.match_type,
            "output": "json",
            "showNumPages": "true",
        })
        if len(rows) < 2:
            return 0
        return int(rows[1][0])

    def _get_json(self, params: dict) -> list:
        backoff = 1.0
        for attempt in range(self.max_retries):
            self.limiter.wait()
            try:
                resp = self.session.get(CDX_ENDPOINT, params=params, timeout=30)
                if resp.status_code in (429, 503) and attempt < self.max_retries - 1:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                resp.raise_for_status()
                return resp.json() if resp.text.strip() else []
            except requests.RequestException:
                if attempt < self.max_retries - 1:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise
        return []


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def save_target_state(target_name: str, target_state: dict):
    """Re-reads the state file at write time and merges in just this target's
    key, instead of blindly overwriting with an in-memory copy loaded at process
    start. Without this, two targets run concurrently (e.g. via separate
    `--target` processes) can race: whichever finishes last silently drops the
    other's newly-discovered slugs."""
    state = load_state()
    state[target_name] = target_state
    save_state(state)


def run(requests_per_second: float = 1.0, only_target: str | None = None):
    client = WaybackCDXClient(requests_per_second=requests_per_second)
    state = load_state()

    targets = [t for t in TARGETS if only_target in (None, t.name)]
    for target in targets:
        slug_pattern = re.compile(target.slug_regex, re.IGNORECASE)
        target_state = state.setdefault(target.name, {"slugs": [], "last_timestamp": None})
        known_slugs = set(target_state["slugs"])
        since = target_state["last_timestamp"]
        newest_timestamp = since
        new_slugs = set()

        log.info(f"[{target.name}] querying since={since or 'beginning of index'}")
        for row in client.query(target, since=since):
            match = slug_pattern.search(row["original"])
            if not match:
                continue
            slug = urllib.parse.unquote(match.group(1)).lower().strip()
            if is_garbage_slug(slug):
                continue
            if slug not in known_slugs:
                new_slugs.add(slug)
            timestamp = row["timestamp"]
            if newest_timestamp is None or timestamp > newest_timestamp:
                newest_timestamp = timestamp

        if new_slugs:
            preview = sorted(new_slugs)[:20]
            suffix = "..." if len(new_slugs) > 20 else ""
            log.info(f"[{target.name}] +{len(new_slugs)} new companies: {preview}{suffix}")
        else:
            log.info(f"[{target.name}] no new companies")

        target_state["slugs"] = sorted(known_slugs | new_slugs)
        target_state["last_timestamp"] = newest_timestamp
        save_target_state(target.name, target_state)  # persist after each target so a crash mid-run doesn't lose progress


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rps", type=float, default=1.0, help="max requests per second against the CDX server (default: 1.0)")
    parser.add_argument("--target", choices=[t.name for t in TARGETS], default=None, help="run a single target instead of all")
    args = parser.parse_args()

    run(requests_per_second=args.rps, only_target=args.target)


if __name__ == "__main__":
    main()
