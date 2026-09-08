"""
Shared location canonicalization + region classification. Single source of
truth used by both the live ingestion path (fetch_postings.py, as postings
are fetched) and the one-time normalize_locations backfill -- so
normalization done today doesn't decay the next time the scrapers run.

Region is a coarse bucket (us / europe / other / remote / unknown) for
company-level flagging ("do they have US or Europe postings"), independent
of the fine-grained Location.city value used for display. A raw string can
canonicalize to a specific city AND resolve to a region even when the two
disagree in specificity (e.g. "Remote" -> region="remote", city=None).

classify_location() intentionally returns is_confident=False for strings it
can't place -- callers (the backfill command) should route those to a
"confusing" file for manual review rather than guessing.
"""

import re
from dataclasses import dataclass

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC",
}
STATE_ABBRS = sorted(set(US_STATES.values()))
STATE_ABBR_RE = re.compile(r",\s*(" + "|".join(STATE_ABBRS) + r")\b(?!\.)")
STATE_NAME_RE = re.compile(r"\b(" + "|".join(re.escape(n) for n in US_STATES) + r")\b", re.IGNORECASE)
# No trailing \b after the optional dot: \b requires a word/non-word
# transition, but a literal "." is itself non-word, so "\b" fails right
# after it whenever followed by another non-word char (space, end-of-string)
# -- "U.S." at the end of "Remote U.S." would never match with a trailing \b.
# The negative lookahead alone already prevents matching into a longer word.
US_COUNTRY_RE = re.compile(r"\bunited states\b|\bu\.s\.a?\.?(?!\w)", re.IGNORECASE)
# Bare "US"/"USA" (no periods) -- extremely common as a standalone location
# value ("US", "US Remote", "USA - Remote") and missed by US_COUNTRY_RE above,
# which requires the dotted form. Case-sensitive on purpose: an
# IGNORECASE \bus\b would match the pronoun "us" in ordinary text, which
# US_COUNTRY_RE's dotted form doesn't risk. Location strings that spell it
# lowercase are rare enough to fall through to REMOTE_US_RE or manual review.
US_BARE_ABBR_RE = re.compile(r"\bUSA?\b")
REMOTE_US_RE = re.compile(r"remote.{0,20}\b(us|usa|u\.s\.)\b", re.IGNORECASE)
BARE_REMOTE_RE = re.compile(r"^\s*remote\s*$", re.IGNORECASE)
# Bare workplace-type descriptors carry zero geographic signal -- treat like
# BARE_REMOTE (auto-accept as region "unknown", not "confusing": a human
# reviewing "Hybrid" alone has nothing more to go on either).
BARE_WORKPLACE_TYPE_RE = re.compile(r"^\s*(hybrid|on-?site|in-?office|remote)\s*$", re.IGNORECASE)
# Bare continent/multi-country region words. EMEA/APAC/LATAM span more than
# one of our buckets (EMEA includes the Middle East and Africa, not just
# Europe) so they resolve to "other", not "europe" -- mapping EMEA to Europe
# would be a real misclassification, not just an imprecise one.
OTHER_REGION_WORD_RE = re.compile(r"\b(emea|apac|latam|north america|latin america|\basia\b)\b", re.IGNORECASE)
GLOBAL_REMOTE_RE = re.compile(r"\b(global|anywhere|world.?wide|remoto)\b", re.IGNORECASE)
EUROPE_REGION_WORD_RE = re.compile(r"\b(europe|european union)\b", re.IGNORECASE)
# Unambiguous-in-context US-only phrase with no single city/state token to
# match against -- "Bay Area" always means the SF Bay Area on a US tech
# company's job board.
US_REGION_PHRASE_RE = re.compile(r"\bbay area\b", re.IGNORECASE)

# City -> canonical display name + state. Deliberately excludes names
# ambiguous with a well-known foreign city (Cambridge/Portland/Birmingham UK)
# since there's no country context in a bare city string to disambiguate.
US_CITIES = {
    "san francisco": ("San Francisco", "CA"), "sf": ("San Francisco", "CA"),
    "south san francisco": ("South San Francisco", "CA"), "palo alto": ("Palo Alto", "CA"),
    "mountain view": ("Mountain View", "CA"), "sunnyvale": ("Sunnyvale", "CA"),
    "cupertino": ("Cupertino", "CA"), "menlo park": ("Menlo Park", "CA"),
    "redwood city": ("Redwood City", "CA"), "san mateo": ("San Mateo", "CA"),
    "santa clara": ("Santa Clara", "CA"), "fremont": ("Fremont", "CA"),
    "berkeley": ("Berkeley", "CA"), "oakland": ("Oakland", "CA"), "emeryville": ("Emeryville", "CA"),
    "burlingame": ("Burlingame", "CA"), "san jose": ("San Jose", "CA"),
    "los angeles": ("Los Angeles", "CA"), "san diego": ("San Diego", "CA"),
    "sacramento": ("Sacramento", "CA"), "irvine": ("Irvine", "CA"), "pasadena": ("Pasadena", "CA"),
    "santa monica": ("Santa Monica", "CA"), "santa barbara": ("Santa Barbara", "CA"),
    "seattle": ("Seattle", "WA"), "bellevue": ("Bellevue", "WA"),
    "chicago": ("Chicago", "IL"), "boston": ("Boston", "MA"), "somerville": ("Somerville", "MA"),
    "austin": ("Austin", "TX"), "dallas": ("Dallas", "TX"), "houston": ("Houston", "TX"),
    "san antonio": ("San Antonio", "TX"), "plano": ("Plano", "TX"),
    "denver": ("Denver", "CO"), "boulder": ("Boulder", "CO"), "atlanta": ("Atlanta", "GA"),
    "miami": ("Miami", "FL"), "orlando": ("Orlando", "FL"), "tampa": ("Tampa", "FL"),
    "nashville": ("Nashville", "TN"), "phoenix": ("Phoenix", "AZ"), "scottsdale": ("Scottsdale", "AZ"),
    "philadelphia": ("Philadelphia", "PA"), "pittsburgh": ("Pittsburgh", "PA"),
    "detroit": ("Detroit", "MI"), "ann arbor": ("Ann Arbor", "MI"),
    "minneapolis": ("Minneapolis", "MN"), "st. louis": ("St. Louis", "MO"),
    "saint louis": ("St. Louis", "MO"), "kansas city": ("Kansas City", "MO"),
    "cincinnati": ("Cincinnati", "OH"), "columbus": ("Columbus", "OH"), "cleveland": ("Cleveland", "OH"),
    "milwaukee": ("Milwaukee", "WI"), "las vegas": ("Las Vegas", "NV"),
    "salt lake city": ("Salt Lake City", "UT"), "charlotte": ("Charlotte", "NC"),
    "raleigh": ("Raleigh", "NC"), "durham": ("Durham", "NC"), "new orleans": ("New Orleans", "LA"),
    "baltimore": ("Baltimore", "MD"), "brooklyn": ("Brooklyn", "NY"), "manhattan": ("Manhattan", "NY"),
    "bronx": ("Bronx", "NY"), "queens": ("Queens", "NY"), "staten island": ("Staten Island", "NY"),
    "nyc": ("New York City", "NY"), "new york city": ("New York City", "NY"),
    "new york": ("New York City", "NY"),
    "washington dc": ("Washington", "DC"), "washington, d.c.": ("Washington", "DC"),
    "arlington": ("Arlington", "VA"), "reston": ("Reston", "VA"),
    "jersey city": ("Jersey City", "NJ"), "hoboken": ("Hoboken", "NJ"),
    "providence": ("Providence", "RI"), "portland, or": ("Portland", "OR"),
}
MAJOR_CITY_RE = re.compile(r"\b(" + "|".join(re.escape(c) for c in sorted(US_CITIES, key=len, reverse=True)) + r")\b", re.IGNORECASE)

EUROPE_COUNTRIES = {
    # "wales" deliberately excluded -- collides with "New South Wales"
    # (Australia); "england"/"scotland"/"united kingdom"/"uk" cover the UK
    # signal without that collision risk.
    "united kingdom": "UK", "uk": "UK", "england": "UK", "scotland": "UK",
    "ireland": "Ireland", "germany": "Germany", "france": "France", "spain": "Spain",
    "portugal": "Portugal", "italy": "Italy", "netherlands": "Netherlands",
    "belgium": "Belgium", "switzerland": "Switzerland", "austria": "Austria",
    "sweden": "Sweden", "norway": "Norway", "denmark": "Denmark", "finland": "Finland",
    "poland": "Poland", "czech republic": "Czech Republic", "czechia": "Czech Republic",
    "romania": "Romania", "greece": "Greece", "hungary": "Hungary", "serbia": "Serbia",
    "croatia": "Croatia", "bulgaria": "Bulgaria", "slovakia": "Slovakia",
    "estonia": "Estonia", "latvia": "Latvia", "lithuania": "Lithuania",
    "ukraine": "Ukraine", "iceland": "Iceland", "luxembourg": "Luxembourg", "malta": "Malta",
    "slovenia": "Slovenia", "cyprus": "Cyprus",
}
EUROPE_COUNTRY_RE = re.compile(r"\b(" + "|".join(re.escape(c) for c in EUROPE_COUNTRIES) + r")\b", re.IGNORECASE)
EUROPE_CITIES = {
    "london": ("London", "UK"), "manchester": ("Manchester", "UK"), "edinburgh": ("Edinburgh", "UK"),
    "birmingham, uk": ("Birmingham", "UK"), "dublin": ("Dublin", "Ireland"),
    "berlin": ("Berlin", "Germany"), "munich": ("Munich", "Germany"), "hamburg": ("Hamburg", "Germany"),
    "frankfurt": ("Frankfurt", "Germany"), "cologne": ("Cologne", "Germany"),
    "paris": ("Paris", "France"), "lyon": ("Lyon", "France"),
    "madrid": ("Madrid", "Spain"), "barcelona": ("Barcelona", "Spain"),
    "lisbon": ("Lisbon", "Portugal"), "porto": ("Porto", "Portugal"),
    "milan": ("Milan", "Italy"), "rome": ("Rome", "Italy"),
    "amsterdam": ("Amsterdam", "Netherlands"), "rotterdam": ("Rotterdam", "Netherlands"),
    "brussels": ("Brussels", "Belgium"), "zurich": ("Zurich", "Switzerland"),
    "geneva": ("Geneva", "Switzerland"), "vienna": ("Vienna", "Austria"),
    "stockholm": ("Stockholm", "Sweden"), "oslo": ("Oslo", "Norway"),
    "copenhagen": ("Copenhagen", "Denmark"), "helsinki": ("Helsinki", "Finland"),
    "warsaw": ("Warsaw", "Poland"), "krakow": ("Krakow", "Poland"), "prague": ("Prague", "Czech Republic"),
    "bucharest": ("Bucharest", "Romania"), "athens": ("Athens", "Greece"),
    "budapest": ("Budapest", "Hungary"), "reykjavik": ("Reykjavik", "Iceland"),
    "vilnius": ("Vilnius", "Lithuania"), "bristol": ("Bristol", "UK"), "kyiv": ("Kyiv", "Ukraine"),
    "kiev": ("Kyiv", "Ukraine"),
}
EUROPE_CITY_RE = re.compile(r"\b(" + "|".join(re.escape(c) for c in sorted(EUROPE_CITIES, key=len, reverse=True)) + r")\b", re.IGNORECASE)

# Defensive: catch other-continent (non-US, non-Europe) signals so they don't
# fall through to "unknown" and get misread as ambiguous-but-maybe-US/EU.
OTHER_RE = re.compile(
    r"\b(india|bangalore|bengaluru|mumbai|delhi|hyderabad|pune|chennai|kolkata|ahmedabad|"
    r"gurgaon|gurugram|noida|singapore|australia|sydney|melbourne|canada|toronto|vancouver|"
    r"montreal|calgary|ottawa|mississauga|ontario|alberta|qu[eé]bec|"
    r"japan|tokyo|osaka|china|beijing|shanghai|shenzhen|guangzhou|hong kong|taiwan|"
    r"taipei|korea|seoul|philippines|manila|indonesia|jakarta|malaysia|kuala lumpur|thailand|"
    r"bangkok|vietnam|hanoi|ho chi minh|new zealand|auckland|south africa|cape town|mexico|"
    r"brazil|brasil|s[aã]o paulo|belo horizonte|buenos aires|argentina|colombia|bogot[aá]|"
    r"chile|santiago|peru|lima|costa rica|panama|nigeria|lagos|kenya|nairobi|egypt|cairo|israel|"
    r"tel aviv|dubai|uae|united arab emirates|saudi arabia|turkey|istanbul|kazakhstan|"
    r"pakistan|bangladesh)\b",
    re.IGNORECASE,
)

# Genuinely carries zero location signal -- a human reviewing "Distributed" or
# "Location TBD" has nothing more to go on than the classifier does, so these
# auto-accept as unknown rather than sitting in the review queue forever.
NON_INFORMATIVE_RE = re.compile(
    r"^\s*(distributed|location\s*tbd|international|multiple locations?|"
    r"blank,?\s*blank|various|n/?a|tbd|see (job )?description|worldwide)\s*$|"
    r"^\s*blank,blank,",
    re.IGNORECASE,
)


@dataclass
class LocationClassification:
    city: str | None       # canonical display name, or None if not resolved to a specific city
    state: str              # state/region code, "" if not applicable
    country: str            # best-guess country name, "" if unknown
    region: str              # "us" | "europe" | "other" | "remote" | "unknown"
    is_confident: bool       # False -> route to the "confusing" review file instead of trusting this


def classify_location(location: str) -> LocationClassification:
    loc = (location or "").strip()

    if not loc:
        return LocationClassification(None, "", "", "unknown", is_confident=False)
    if BARE_REMOTE_RE.match(loc):
        return LocationClassification(None, "", "", "remote", is_confident=True)
    if NON_INFORMATIVE_RE.match(loc):
        return LocationClassification(None, "", "", "unknown", is_confident=True)
    if BARE_WORKPLACE_TYPE_RE.match(loc):
        return LocationClassification(None, "", "", "unknown", is_confident=True)

    other = OTHER_RE.search(loc)
    other_region_word = OTHER_REGION_WORD_RE.search(loc)
    europe_country = EUROPE_COUNTRY_RE.search(loc) or EUROPE_REGION_WORD_RE.search(loc)
    europe_city = EUROPE_CITY_RE.search(loc)
    us_state_abbr = STATE_ABBR_RE.search(loc)
    us_state_name = STATE_NAME_RE.search(loc)
    us_country = (US_COUNTRY_RE.search(loc) or US_BARE_ABBR_RE.search(loc)
                  or REMOTE_US_RE.search(loc) or US_REGION_PHRASE_RE.search(loc))
    us_city = MAJOR_CITY_RE.search(loc)
    global_remote = GLOBAL_REMOTE_RE.search(loc)

    # A state-level match (explicit ", ST" or a full state name paired with an
    # explicit US country mention) is a much stronger signal than a solitary
    # bare-city name that happens to collide with a same-named foreign city
    # ("Vienna, Virginia, United States" vs. "Vienna, Austria") -- let the
    # state win over a bare europe_city coincidence instead of bailing to
    # "ambiguous". Doesn't override a genuine second-country mention
    # (europe_country/other/other_region_word), since that's a real
    # multi-location string, not a name collision.
    strong_us = bool(us_state_abbr or (us_state_name and us_country))
    if strong_us and not (europe_country or other or other_region_word):
        state = us_state_abbr.group(1).upper() if us_state_abbr else US_STATES[us_state_name.group(1).lower()]
        return LocationClassification(None, state, "United States", "us", is_confident=True)

    us_hit = bool(us_state_abbr or us_state_name or us_country or us_city)
    europe_hit = bool(europe_country or europe_city)
    other_hit = bool(other or other_region_word)

    # Mixed signals (e.g. "Austin, TX or Bangalore", "London or NYC") --
    # genuinely ambiguous, a human should decide rather than us guessing.
    if sum([other_hit, europe_hit, us_hit]) > 1:
        return LocationClassification(None, "", "", "unknown", is_confident=False)

    if other_region_word:
        return LocationClassification(None, "", "", "other", is_confident=True)
    if other:
        return LocationClassification(None, "", "", "other", is_confident=True)

    if us_city:
        city, state = US_CITIES[us_city.group(1).lower()]
        return LocationClassification(city, state, "United States", "us", is_confident=True)
    if us_state_name:
        return LocationClassification(None, US_STATES[us_state_name.group(1).lower()], "United States", "us", is_confident=True)
    if us_country:
        return LocationClassification(None, "", "United States", "us", is_confident=True)

    if europe_city:
        city, country = EUROPE_CITIES[europe_city.group(1).lower()]
        return LocationClassification(city, "", country, "europe", is_confident=True)
    if europe_country:
        # .get() with a fallback, not direct indexing: europe_country can
        # also come from EUROPE_REGION_WORD_RE ("europe", "european union"),
        # which isn't a key in EUROPE_COUNTRIES (that dict only holds actual
        # country names) -- those matches don't resolve to a specific country.
        country = EUROPE_COUNTRIES.get(europe_country.group(1).lower(), "")
        return LocationClassification(None, "", country, "europe", is_confident=True)

    if global_remote:
        return LocationClassification(None, "", "", "remote", is_confident=True)

    # No recognized signal at all -- don't guess, flag for review.
    return LocationClassification(None, "", "", "unknown", is_confident=False)


def country_to_region(country: str) -> str:
    """For dicts that already carry structured country data (Ashby's
    postalAddress.addressCountry) -- trust the given country/city rather than
    re-deriving them from a string, but still need a region bucket."""
    c = (country or "").strip().lower()
    if not c:
        return "unknown"
    if c in ("us", "usa", "united states", "united states of america"):
        return "us"
    if c in EUROPE_COUNTRIES or c in {v.lower() for v in EUROPE_COUNTRIES.values()}:
        return "europe"
    return "other"


def classify_location_dict(loc: dict) -> dict:
    """Enrich a {"city", "state", "country"} dict (as built by
    fetch_postings.py's single_location/split_locations/ashby_locations) with
    a region bucket, and canonicalize the city if it looks like a raw
    free-text fragment rather than already-structured ATS data.

    Structured data (country already present, e.g. from Ashby's
    postalAddress) is trusted as-is -- only region gets added. A bare city
    string with no country (the common case for Greenhouse/Lever, and
    Ashby's own bare-location fallback) gets run through classify_location();
    its result overrides city/state/country only when confident, otherwise
    the original raw fragment is kept (so no data is lost) with region left
    "unknown" for later manual review.
    """
    if loc.get("country"):
        return {**loc, "region": country_to_region(loc["country"])}

    result = classify_location(loc.get("city", ""))
    if result.is_confident:
        return {
            "city": result.city or loc.get("city", ""),
            "state": result.state or loc.get("state", ""),
            "country": result.country or loc.get("country", ""),
            "region": result.region,
        }
    return {**loc, "region": "unknown"}


# Broad "employs engineers" signal for company-level flagging -- deliberately
# WIDER than score_bay_area_postings.py's SWE_TITLE_PATTERNS (which is scoped
# to Austin's personal resume fit and excludes DevOps/SRE/QA/Data/ML/Security
# on purpose). For "does this company have real engineering headcount",
# those disciplines should count; only non-technical titles that happen to
# use the word "engineer" (Sales/Solutions/Support/Field/Release Engineer)
# are excluded.
ENGINEERING_TITLE_RE = re.compile(r"\bengineer(ing)?\b", re.IGNORECASE)
NON_ENGINEERING_TITLE_RE = re.compile(
    r"sales engineer|solutions? engineer|support engineer|field engineer|"
    r"release engineer|customer engineer|reliability engineer.{0,20}\(?sales|"
    r"revenue engineer|deal engineer",
    re.IGNORECASE,
)


def is_engineering_title(title: str) -> bool:
    return bool(ENGINEERING_TITLE_RE.search(title)) and not NON_ENGINEERING_TITLE_RE.search(title)
