"""Best-effort extraction of an annual comp range from free-text job descriptions.

Not all ATS platforms expose structured salary data, so this regex-parses
whatever dollar figures appear in the description text. Heuristic, not exact.
"""

import re

_NUM = r"\$\s?(\d{2,3}(?:,\d{3})+|\d+(?:\.\d+)?\s?[kK])"
RANGE_RE = re.compile(rf"{_NUM}\s*(?:-|–|—|to)\s*{_NUM}")
SINGLE_RE = re.compile(_NUM)


def _to_int(raw: str) -> int:
    raw = raw.strip()
    if raw[-1].lower() == "k" or raw[-2:].lower().endswith("k"):
        return int(round(float(raw[:-1].replace(",", "").strip()) * 1000))
    return int(raw.replace(",", ""))


def parse_comp_range(text: str) -> tuple[int | None, int | None]:
    """Return (avg_comp, lowest_comp) parsed from text, or (None, None)."""
    if not text:
        return None, None

    match = RANGE_RE.search(text)
    if match:
        lo = _to_int(match.group(1))
        hi = _to_int(match.group(2))
        lo, hi = min(lo, hi), max(lo, hi)
        # Filter out obviously-not-a-salary numbers (e.g. "$5 - $10" perk credits)
        if lo < 10000:
            return None, None
        return round((lo + hi) / 2), lo

    match = SINGLE_RE.search(text)
    if match:
        value = _to_int(match.group(1))
        if value < 10000:
            return None, None
        return value, value

    return None, None
