"""Shared validation for ATS board slugs discovered via job_crawler.py.

Wayback occasionally mis-captures a URL path that isn't really a company slug
-- template placeholders left unrendered ("${company}"), reserved paths
(".well-known", "sitemap.xml"), or garbage query/session data that ended up
looking like a path segment. is_garbage_slug() filters those out without
rejecting real (if unusually punctuated) company names like "weights&biases"
or "s'well".
"""

import re

PRICE_RE = re.compile(r"^\$[\d.]+k$", re.IGNORECASE)
HAS_ALNUM_RE = re.compile(r"[a-z0-9]", re.IGNORECASE)
BAD_CHARS_RE = re.compile(r'["{}\\]')
PURE_NUMBER_RE = re.compile(r"^\d+;?$")
# CSS-value-shaped tokens, e.g. "100vh", "100%;", "0;" -- these came from a
# historical bot that fed mis-parsed CSS/JS tokens to Ashby's SPA, which
# returns 200 for any path and let Wayback archive them as if real.
CSS_VALUE_RE = re.compile(r"^\d+(\.\d+)?(vh|vw|px|em|rem|pt|%);?$", re.IGNORECASE)
# Ashby client-side router IDs, e.g. "root.6489aa4b_831c_4391_b849_dfc08988b82c"
# -- same root cause as CSS_VALUE_RE (the jobs.ashbyhq.com SPA shell returns
# 200 for literally any path), just a different mis-captured token shape.
# Confirmed via a full fetch_postings re-pull: these are ~58% of all
# newly-invalid Ashby slugs and 100% 404 on the real posting API.
ASHBY_ROUTE_ID_RE = re.compile(r"^root\.[0-9a-f]{8}[_-][0-9a-f]{4}", re.IGNORECASE)


def is_garbage_slug(slug: str) -> bool:
    if len(slug) < 2:
        return True
    if len(slug) > 80:
        return True
    if BAD_CHARS_RE.search(slug):
        return True
    if slug.startswith("."):
        return True
    if PRICE_RE.match(slug):
        return True
    if PURE_NUMBER_RE.match(slug):
        return True
    if CSS_VALUE_RE.match(slug):
        return True
    if ASHBY_ROUTE_ID_RE.match(slug):
        return True
    if not HAS_ALNUM_RE.search(slug):
        return True
    if slug == "${company}":
        return True
    return False
