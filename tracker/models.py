import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.db import models

# Query params known to be pure tracking noise, safe to strip so a
# utm_source=... variant of a URL hashes the same as the bare one. Anything
# NOT in this set is kept -- some ATS embeds (e.g. a company's own branded
# careers page proxying Greenhouse, like mongodb.com/careers/job/?gh_jid=...)
# put the actual per-job identifier in the query string with an identical
# path across every posting. Stripping the whole query string there collapsed
# every one of that company's jobs onto the same canonical URL/hash, so only
# the last-fetched job survived each upsert. See gh_jid incident: MongoDB
# showed 1 stored posting against 406 live ones until this was fixed.
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                    "gh_src", "ref", "source", "trk", "_ga", "mc_cid", "mc_eid"}


def canonicalize_url(url: str) -> str:
    """Strip known tracking params and trailing slash so a utm_source=...
    variant of a URL hashes the same as the bare one -- but keep every other
    query param, since some platforms encode the actual job identity there
    (see TRACKING_PARAMS docstring)."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    kept = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    )
    query = urlencode(kept)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


class Company(models.Model):
    # Crawler discovery only gives us a platform + slug, not a real display
    # name -- name starts as a best-effort derivation from the slug and can be
    # corrected later (e.g. via the admin, or once we scrape the board itself).
    name = models.CharField(max_length=255, blank=True, default="")
    ats_platform = models.CharField(max_length=50)  # "ashby" | "greenhouse" | "lever" | "smartrecruiters" | "workable" | "gem" | "breezy" | "rippling"
    slug = models.CharField(max_length=255)  # the ATS board slug
    # Set when the ATS API itself rejects this slug (non-200 / malformed
    # response) -- i.e. the slug doesn't correspond to a real board at all.
    # NOT set just because a real company currently has zero postings --
    # those get rechecked on future runs since they might post something later.
    is_invalid = models.BooleanField(default=False)
    # Freeform due-diligence writeup (WLB, leadership sentiment, recurring
    # themes, recent news, etc.) produced by the company_researcher prompt --
    # see interview_funnel/prompts/prep/company_researcher.md and the
    # update_company_gen_info management command. None until researched;
    # left alone (not re-researched) once set.
    gen_info = models.JSONField(null=True, blank=True, default=None)
    # Personal watchlist rank -- null means "not on the list". Lower number =
    # higher priority; ties are fine, this isn't a strict ordering. Set only
    # by a human (admin, management command); nothing in the codebase clears
    # or auto-computes it -- unlike last_us_engineering_posting_at below,
    # this is a manual curation signal, not a derived one.
    preference_rank = models.PositiveSmallIntegerField(null=True, blank=True, default=None)
    # Stamped by fetch_postings whenever an engineering-titled (broad sense --
    # see location_utils.is_engineering_title) posting classified as US-located
    # is upserted for this company. Deliberately a timestamp, not a boolean:
    # "currently hiring engineers in the US" is evaluated at query time via a
    # rolling window (e.g. within the last 90 days) against this field, so it
    # never needs an explicit recompute pass and just ages out naturally.
    last_us_engineering_posting_at = models.DateTimeField(null=True, blank=True)
    # Manual opt-out from ats_score_prompt.md scoring -- set for companies
    # whose boards repost the same requisition(s) on a near-daily cadence
    # (e.g. via automated ATS re-listing), where scoring each "new" repost
    # produces no new signal and just wastes effort. Doesn't affect crawling
    # or watchlist rank, only whether scoring runs bother with this company.
    ats_scoring_blacklisted = models.BooleanField(default=False)

    class Meta:
        unique_together = ("ats_platform", "slug")

    def __str__(self):
        return f"{self.name} ({self.ats_platform}/{self.slug})"


class Location(models.Model):
    """A normalized city/state/country. Kept separate from Posting.location
    (the raw ATS-reported string, e.g. "Cambridge" or "Remote") because a
    single posting can list more than one office -- Ashby's
    secondaryLocations, for example -- and the two locations are genuinely
    different postable places (San Francisco != New York), not variants of
    the same one."""

    REGION_CHOICES = [
        ("us", "US"), ("europe", "Europe"), ("other", "Other"),
        ("remote", "Remote"), ("unknown", "Unknown"),
    ]

    city = models.CharField(max_length=255)
    state = models.CharField(max_length=255, blank=True, default="")
    country = models.CharField(max_length=255, blank=True, default="")
    # Coarse bucket for company-level flagging ("has US or Europe postings"),
    # independent of city's granularity -- see tracker/location_utils.py for
    # the classification logic this is derived from.
    region = models.CharField(max_length=10, choices=REGION_CHOICES, default="unknown")

    class Meta:
        unique_together = ("city", "state", "country")

    def __str__(self):
        return ", ".join(p for p in (self.city, self.state, self.country) if p)


class PostingManager(models.Manager):
    def upsert(self, *, company: Company, title: str, url: str, location: str = "", description: str = "",
               avg_comp: int | None = None, lowest_comp: int | None = None,
               posted_at=None, workplace_type: str = "", locations: list[dict] | None = None):
        """Create or update a posting keyed by a hash of (platform, company slug,
        canonicalized url). Re-discovering the same posting through a different
        tracking-param variant of the URL resolves to the same row.

        `locations` is an optional list of {"city", "state", "country"} dicts
        (state/country may be blank) -- each is resolved to a Location row and
        the posting's locations m2m is set to match. Passing None leaves the
        existing m2m links untouched, since not every ATS fetcher has
        structured location data to offer."""
        canonical = canonicalize_url(url)
        raw = f"{company.ats_platform}:{company.slug}:{canonical}"
        posting_id = hashlib.sha256(raw.encode()).hexdigest()
        obj, _ = self.update_or_create(
            id=posting_id,
            defaults={
                "company": company,
                "title": title,
                "location": location,
                "url": url,
                "description": description,
                "avg_comp": avg_comp,
                "lowest_comp": lowest_comp,
                "posted_at": posted_at,
                "workplace_type": workplace_type,
            },
        )
        if locations is not None:
            location_objs = []
            for loc in locations:
                # Empty city is meaningful for "remote"/"other"/bare-country
                # entries (region-only classification, no specific city) --
                # only skip a dict that's entirely empty (nothing to record).
                if not (loc.get("city") or loc.get("state") or loc.get("country") or loc.get("region")):
                    continue
                city, state, country = loc.get("city", ""), loc.get("state", ""), loc.get("country", "")
                existing = Location.objects.filter(city=city, state=state, country=country).first()
                if existing:
                    region = loc.get("region")
                    if region and region != "unknown" and existing.region != region:
                        existing.region = region
                        existing.save(update_fields=["region"])
                    location_objs.append(existing)
                else:
                    location_objs.append(Location.objects.create(
                        city=city, state=state, country=country, region=loc.get("region", "unknown"),
                    ))
            obj.locations.set(location_objs)
        return obj

    def bulk_upsert(self, rows: list[dict], location_cache: dict | None = None) -> int:
        """Batch version of upsert() for the ingest_postings pipeline --
        dump_postings fetches with no DB access at all (safe to run several
        platforms concurrently), then this does the one-writer-only SQLite
        work in large batches instead of one upsert() call (several
        round-trips each) per posting.

        Each row is the same shape as upsert()'s kwargs: company, title, url,
        location, description, avg_comp, lowest_comp, posted_at,
        workplace_type, locations (list of {"city","state","country","region"}
        dicts or None).

        Returns the number of postings written. Location resolution is
        cached -- a handful of cities (San Francisco, Remote, ...) recur
        across thousands of postings, so this turns thousands of individual
        get_or_create() round-trips into one lookup query plus one
        bulk_create() for whatever's actually missing.

        `location_cache`, if passed, is mutated in place and reused --
        callers doing many sequential bulk_upsert() calls (ingest_postings,
        batching thousands of postings at a time) should pass the same dict
        across every call so common locations get resolved once for the
        whole run instead of once per batch. Defaults to a fresh, batch-only
        cache if not given.
        """
        if not rows:
            return 0
        if location_cache is None:
            location_cache = {}

        # Pass 1: figure out every distinct location tuple this batch needs
        # that isn't already in the cache, resolve those in one query + one
        # bulk_create for whatever's still missing after that.
        needed = {}  # (city, state, country) -> region (last one wins if they disagree)
        for row in rows:
            for loc in row.get("locations") or []:
                if not (loc.get("city") or loc.get("state") or loc.get("country") or loc.get("region")):
                    continue
                key = (loc.get("city", ""), loc.get("state", ""), loc.get("country", ""))
                needed[key] = loc.get("region", "unknown")

        uncached = {key: region for key, region in needed.items() if key not in location_cache}
        if uncached:
            # Chunked rather than one Q() OR-chain over every uncached tuple --
            # a single batch can need enough distinct locations that the combined
            # expression tree exceeds SQLite's default max depth (1000), which a
            # large first-time full-sweep batch (e.g. Breezy's ~9k companies) hit
            # in practice. 200 tuples/chunk (~600 leaf comparisons) stays well clear.
            CHUNK_SIZE = 200
            uncached_keys = list(uncached)
            for i in range(0, len(uncached_keys), CHUNK_SIZE):
                chunk = uncached_keys[i:i + CHUNK_SIZE]
                existing_q = models.Q()
                for city, state, country in chunk:
                    existing_q |= models.Q(city=city, state=state, country=country)
                for loc in Location.objects.filter(existing_q):
                    location_cache[(loc.city, loc.state, loc.country)] = loc

            missing = [key for key in uncached if key not in location_cache]
            if missing:
                created = Location.objects.bulk_create([
                    Location(city=city, state=state, country=country, region=uncached[(city, state, country)])
                    for city, state, country in missing
                ])
                for loc in created:
                    location_cache[(loc.city, loc.state, loc.country)] = loc

            # Region may have been learned since a location row was first
            # created (e.g. classifier improvements) -- keep it current.
            stale = [loc for key, loc in location_cache.items()
                     if needed.get(key) not in (None, "unknown") and loc.region != needed[key]]
            for loc in stale:
                loc.region = needed[(loc.city, loc.state, loc.country)]
            if stale:
                Location.objects.bulk_update(stale, ["region"])

        # Pass 2: build every Posting instance with its id precomputed (our
        # PK is a deterministic hash, not autoincrement, so we already know
        # it -- no need to round-trip through bulk_create's return value to
        # find out what got written).
        # last_seen (auto_now) is in this list deliberately -- bulk_create's
        # ON CONFLICT DO UPDATE clause only touches columns named in
        # update_fields, so omitting it here silently stopped last_seen from
        # ever advancing on re-upserted postings (caught via a before/after
        # staleness check showing 0 "confirmed still live" postings after a
        # real ingest run that should have refreshed hundreds of thousands).
        mutable_fields = ["company", "title", "location", "url", "description",
                           "avg_comp", "lowest_comp", "posted_at", "workplace_type", "last_seen"]
        instances = []
        ids = []
        for row in rows:
            canonical = canonicalize_url(row["url"])
            raw = f"{row['company'].ats_platform}:{row['company'].slug}:{canonical}"
            posting_id = hashlib.sha256(raw.encode()).hexdigest()
            ids.append(posting_id)
            instances.append(Posting(
                id=posting_id, company=row["company"], title=row["title"],
                location=row.get("location", ""), url=row["url"],
                description=row.get("description", ""), avg_comp=row.get("avg_comp"),
                lowest_comp=row.get("lowest_comp"), posted_at=row.get("posted_at"),
                workplace_type=row.get("workplace_type", ""),
            ))

        self.bulk_create(instances, update_conflicts=True, unique_fields=["id"], update_fields=mutable_fields)

        # Pass 3: m2m links. Still one .set() per posting (locations.set()
        # itself does the clear+add), but every Location lookup inside it is
        # now a cache hit instead of a query, which is where the real
        # per-posting cost was.
        posting_by_id = {p.id: p for p in self.filter(id__in=ids)}
        for row, posting_id in zip(rows, ids):
            if row.get("locations") is None:
                continue
            location_objs = [
                location_cache[(loc.get("city", ""), loc.get("state", ""), loc.get("country", ""))]
                for loc in row["locations"]
                if loc.get("city") or loc.get("state") or loc.get("country") or loc.get("region")
            ]
            posting_by_id[posting_id].locations.set(location_objs)

        return len(rows)


class Posting(models.Model):
    id = models.CharField(max_length=64, primary_key=True, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="postings")
    title = models.CharField(max_length=500)
    location = models.CharField(max_length=255, blank=True)
    url = models.URLField(max_length=1000)
    description = models.TextField(blank=True)
    # Comp as advertised on the listing itself (parsed from a stated range, e.g.
    # "$140k - $180k" -> avg_comp=160000, lowest_comp=140000).
    avg_comp = models.PositiveIntegerField(null=True, blank=True)
    lowest_comp = models.PositiveIntegerField(null=True, blank=True)
    # When the ATS itself says the job was published (Ashby publishedAt,
    # Greenhouse first_published, Lever createdAt) -- distinct from
    # first_seen/last_seen below, which track when *we* crawled it.
    posted_at = models.DateTimeField(null=True, blank=True)
    # "Remote" | "Hybrid" | "Onsite" as reported by the platform, when available.
    workplace_type = models.CharField(max_length=20, blank=True)
    # Structured locations this posting is based in -- a posting can list more
    # than one (e.g. "San Francisco or New York"). `location` above stays as
    # the raw ATS string for display/back-compat; this is the queryable form.
    locations = models.ManyToManyField(Location, related_name="postings", blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    # Both scored per interview_funnel/prompts/prep/ats_score_prompt.md, which
    # deliberately produces two divergent numbers -- a literal keyword-parser
    # score and a qualitative recruiter-read score -- rather than one blended
    # score, since the gap between them is itself signal (e.g. a resume that
    # clears keywords but is a weak substantive match, or vice versa). Null
    # until scored; nothing recomputes these automatically.
    ats_score = models.PositiveSmallIntegerField(null=True, blank=True, default=None)
    recruiter_fit_score = models.PositiveSmallIntegerField(null=True, blank=True, default=None)
    # Full prompt output (stack/keyword table, requirements walk, real gaps,
    # bottom line) -- the two scores above are just the headline numbers.
    fit_notes = models.TextField(blank=True, default="")
    # Set during the same qualitative scoring pass as ats_score/recruiter_fit_score
    # -- a human/LLM read of whether this is actually a software engineering role,
    # not the keyword-title-matching heuristic in score_bay_area_postings.py.
    # Exists because titles like "Design Engineer" or "Application Engineer" are
    # used by both software product teams (Gumloop, Supabase) and hardware/civil/
    # chip teams (Jane Street ASIC, SpaceX avionics) with no reliable regex split --
    # this field is the judgment call, made once, at scoring time. Blank/unset
    # until scored; nothing recomputes it automatically.
    ROLE_TYPE_CHOICES = [
        ("software", "Software"),
        ("hardware", "Hardware/Physical"),
        ("hybrid", "Hybrid"),
        ("other", "Other/Non-Engineering"),
    ]
    role_type = models.CharField(max_length=20, blank=True, default="", choices=ROLE_TYPE_CHOICES)

    objects = PostingManager()

    def __str__(self):
        return f"{self.title} @ {self.company.name}"


class ApplicationManager(models.Manager):
    def upsert(self, *, posting: Posting, date_applied, status: str = "Applied",
               avg_comp: int | None = None, lowest_comp: int | None = None, ats_score=None, notes: str = ""):
        """Create or update an application keyed by a hash of (posting id, date
        applied). Reprocessing the same application event twice — e.g. the Gmail
        poller and the crawler both seeing it — collapses to one row instead of
        duplicating."""
        raw = f"{posting.id}:{date_applied.isoformat()}"
        application_id = hashlib.sha256(raw.encode()).hexdigest()
        obj, _ = self.update_or_create(
            id=application_id,
            defaults={
                "posting": posting,
                "date_applied": date_applied,
                "status": status,
                "avg_comp": avg_comp,
                "lowest_comp": lowest_comp,
                "ats_score": ats_score,
                "notes": notes,
            },
        )
        return obj


class Application(models.Model):
    id = models.CharField(max_length=64, primary_key=True, editable=False)
    posting = models.ForeignKey(Posting, on_delete=models.CASCADE, related_name="applications")
    date_applied = models.DateField()
    status = models.CharField(max_length=20, default="Applied")
    # What was actually discussed/offered for this specific application -- can
    # differ from Posting.avg_comp/lowest_comp (the range as originally advertised).
    avg_comp = models.PositiveIntegerField(null=True, blank=True)
    lowest_comp = models.PositiveIntegerField(null=True, blank=True)
    ats_score = models.IntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)
    date_updated = models.DateTimeField(auto_now=True)

    objects = ApplicationManager()

    def __str__(self):
        return f"{self.posting.title} @ {self.posting.company.name} ({self.date_applied})"


class InterviewStage(models.Model):
    STAGE_CHOICES = [
        ("recruiter", "Recruiter"),
        ("hiring_manager", "Hiring Manager"),
        ("technical", "Technical"),
        ("system_design", "System Design"),
        ("onsite", "Onsite"),
    ]

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="interview_stages")
    stage = models.CharField(max_length=20, choices=STAGE_CHOICES)
    date_updated = models.DateTimeField()
    date_ended = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=255, blank=True, default="")

    def __str__(self):
        return f"{self.get_stage_display()} @ {self.application}"
