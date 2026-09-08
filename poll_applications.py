"""
Rate-Limited Application Poller
--------------------------------
Polls Gmail for new job-related email, classifies each message, and upserts
the `applications` table (SQLite, via database.Database) as matches are
found. State is committed after every single email — not batched at the end
of a cycle — so a crash mid-poll never loses or reprocesses already-handled
messages.

Run directly: python poll_applications.py            (loop forever)
              python poll_applications.py --once      (single poll, then exit)
              python poll_applications.py --rps 3      (Gmail API calls/sec cap)
"""

import argparse
import logging
import time
from datetime import datetime

from googleapiclient.discovery import build

from config import GHOSTING_THRESHOLD_DAYS, GMAIL_QUERY, POLL_INTERVAL_SECONDS
from database import Database
from email_classifier import EmailClassifier
from gmail_poller import GmailPoller, get_google_credentials
from resume_scorer import ResumeScorer
from state_machine import StateMachine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


class RateLimiter:
    """Enforces a minimum interval between successive calls (e.g. Gmail API requests)."""

    def __init__(self, requests_per_second: float = 5.0):
        self.min_interval = 1.0 / requests_per_second
        self._last_call = 0.0

    def wait(self):
        remaining = self.min_interval - (time.monotonic() - self._last_call)
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()


class RateLimitedGmailPoller(GmailPoller):
    """Same message-fetching logic as gmail_poller.GmailPoller, but every Gmail
    API call (list + each individual get) passes through a RateLimiter first,
    instead of firing as fast as the network allows."""

    def __init__(self, limiter: RateLimiter):
        self.limiter = limiter
        creds = get_google_credentials()
        self.service = build("gmail", "v1", credentials=creds)

    def fetch_new_emails(self, db: Database) -> list[dict]:
        self.limiter.wait()
        results = (
            self.service.users()
            .messages()
            .list(userId="me", q=GMAIL_QUERY, maxResults=50)
            .execute()
        )

        messages = results.get("messages", [])
        new_emails = []

        for msg_ref in messages:
            if db.is_email_processed(msg_ref["id"]):
                continue

            self.limiter.wait()
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=msg_ref["id"], format="full")
                .execute()
            )

            parsed = self._parse_message(msg)
            if parsed:
                new_emails.append(parsed)

        return new_emails


def process_email(email: dict, db: Database, classifier: EmailClassifier,
                   scorer: ResumeScorer, state_machine: StateMachine):
    log.info(f"Processing email: {email['subject'][:60]!r}")

    try:
        parsed = classifier.classify(email)
    except Exception:
        log.exception("  -> classifier call failed, skipping this email (will retry next poll)")
        return

    if not parsed:
        log.info("  -> not job-related or low confidence, skipping")
        db.mark_email_processed(email["id"], email["thread_id"], -1)
        return

    company = parsed.get("company") or "Unknown"
    job_title = parsed.get("job_title") or "Unknown Role"
    event_type = parsed.get("event_type", "unknown")
    log.info(f"  -> {company} | {job_title} | event: {event_type}")

    existing = db.get_application(company, job_title)
    current_status = existing.status if existing else None
    new_status = state_machine.transition(current_status, event_type)

    ats_score = existing.ats_score if existing else None
    score_notes = None
    if ats_score is None and parsed.get("job_description"):
        log.info("  -> scoring resume against job description...")
        try:
            result = scorer.score(parsed["job_description"])
            ats_score = result.get("score")
            score_notes = result.get("notes", "")
            log.info(f"  -> ATS score: {ats_score}")
        except Exception:
            log.exception("  -> ATS scoring failed, continuing without a score")

    now = datetime.utcnow()
    app_data = {
        "company": company,
        "job_title": job_title,
        "location": parsed.get("location") or (existing.location if existing else None),
        "status": new_status,
        "comp": parsed.get("comp") or (existing.comp if existing else None),
        "ats_score": ats_score,
        "platform": parsed.get("platform") or (existing.platform if existing else None),
        "date_applied": existing.date_applied if existing else now,
        "date_updated": now,
        "notes": score_notes or parsed.get("notes"),
    }

    app = db.upsert_application(app_data)
    # Persist processed-state immediately after this email is committed, so a
    # crash on the next email never reprocesses or drops this one.
    db.mark_email_processed(email["id"], email["thread_id"], app.id)
    log.info(f"  -> application id={app.id} status={new_status}")


def check_ghosting(db: Database):
    stale = db.get_stale_applications(GHOSTING_THRESHOLD_DAYS)
    for app in stale:
        db.update_application(app.id, status="Ghosted", date_updated=datetime.utcnow())
        log.info(f"Marked application id={app.id} ({app.company} | {app.job_title}) as Ghosted")


def poll_once(poller: RateLimitedGmailPoller, db: Database, classifier: EmailClassifier,
              scorer: ResumeScorer, state_machine: StateMachine):
    log.info("Polling Gmail...")
    emails = poller.fetch_new_emails(db)
    log.info(f"Found {len(emails)} new email(s) to process.")

    for email in emails:
        process_email(email, db, classifier, scorer, state_machine)

    check_ghosting(db)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="poll a single time then exit, instead of looping")
    parser.add_argument("--rps", type=float, default=5.0, help="max Gmail API requests/sec (default: 5.0)")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SECONDS,
                         help=f"seconds between poll cycles when looping (default: {POLL_INTERVAL_SECONDS})")
    args = parser.parse_args()

    limiter = RateLimiter(requests_per_second=args.rps)
    db = Database()
    poller = RateLimitedGmailPoller(limiter)
    classifier = EmailClassifier()
    scorer = ResumeScorer()
    state_machine = StateMachine()

    if args.once:
        poll_once(poller, db, classifier, scorer, state_machine)
        return

    log.info(f"Looping every {args.interval}s, Gmail calls capped at {args.rps}/s.")
    try:
        while True:
            poll_once(poller, db, classifier, scorer, state_machine)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
