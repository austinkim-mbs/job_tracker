"""
Job Application Tracker
-----------------------
Polls Gmail every 5 minutes, classifies job-related emails with Claude,
scores them against your resume, and writes results to Google Sheets.

Run directly:   python main.py
Run as service: see service.py
"""

import logging
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler

from config import POLL_INTERVAL_SECONDS, GHOSTING_THRESHOLD_DAYS
from database import Database
from gmail_poller import GmailPoller
from email_classifier import EmailClassifier
from resume_scorer import ResumeScorer
from state_machine import StateMachine
from sheets_writer import SheetsWriter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


class JobTracker:
    def __init__(self):
        log.info("Initializing Job Tracker...")
        self.db = Database()
        self.poller = GmailPoller()
        self.classifier = EmailClassifier()
        self.scorer = ResumeScorer()
        self.state_machine = StateMachine()
        self.writer = SheetsWriter()
        log.info("Ready.")

    def run_once(self):
        log.info("Polling Gmail...")
        emails = self.poller.fetch_new_emails(self.db)
        log.info(f"Found {len(emails)} new email(s) to process.")

        for email in emails:
            self._process_email(email)

        self._check_ghosting()

    def _process_email(self, email: dict):
        log.info(f"Processing email: {email['subject'][:60]}")

        parsed = self.classifier.classify(email)
        if not parsed:
            log.info("  → Not job-related or low confidence, skipping.")
            self.db.mark_email_processed(email["id"], email["thread_id"], -1)
            return

        company = parsed.get("company") or "Unknown"
        job_title = parsed.get("job_title") or "Unknown Role"
        event_type = parsed.get("event_type", "unknown")

        log.info(f"  → {company} | {job_title} | event: {event_type}")

        # Get existing application state
        existing = self.db.get_application(company, job_title)
        current_status = existing.status if existing else None

        # Determine new status
        new_status = self.state_machine.transition(current_status, event_type)

        # ATS score — only on first encounter when JD is available
        ats_score = existing.ats_score if existing else None
        if ats_score is None and parsed.get("job_description"):
            log.info("  → Scoring resume against job description...")
            result = self.scorer.score(parsed["job_description"])
            ats_score = result.get("score")
            score_notes = result.get("notes", "")
            log.info(f"  → ATS score: {ats_score}")
        else:
            score_notes = None

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

        app = self.db.upsert_application(app_data)
        row_num = self.writer.upsert(app)
        self.db.update_application(app.id, sheet_row=row_num)
        self.db.mark_email_processed(email["id"], email["thread_id"], app.id)

        log.info(f"  → Written to sheet row {row_num} | status: {new_status}")

    def _check_ghosting(self):
        stale = self.db.get_stale_applications(GHOSTING_THRESHOLD_DAYS)
        if stale:
            log.info(f"Marking {len(stale)} application(s) as Ghosted.")
        for app in stale:
            self.db.update_application(app.id, status="Ghosted", date_updated=datetime.utcnow())
            app.status = "Ghosted"
            app.date_updated = datetime.utcnow()
            self.writer.upsert(app)


def main():
    tracker = JobTracker()
    tracker.run_once()  # run immediately on startup

    scheduler = BlockingScheduler()
    scheduler.add_job(
        tracker.run_once,
        "interval",
        seconds=POLL_INTERVAL_SECONDS,
        id="poll_gmail",
    )

    log.info(f"Scheduler started. Polling every {POLL_INTERVAL_SECONDS}s.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
