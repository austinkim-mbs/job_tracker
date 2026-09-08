from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, Float, DateTime, Text
from sqlalchemy.orm import declarative_base, Session as OrmSession
from config import DB_PATH

Base = declarative_base()


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company = Column(String, nullable=False)
    job_title = Column(String, nullable=False)
    location = Column(String)
    status = Column(String, default="Applied")
    comp = Column(String)
    ats_score = Column(Integer)
    platform = Column(String)
    date_applied = Column(DateTime)
    date_updated = Column(DateTime)
    notes = Column(Text)


class ProcessedEmail(Base):
    __tablename__ = "processed_emails"

    email_id = Column(String, primary_key=True)
    thread_id = Column(String)
    application_id = Column(Integer)
    processed_at = Column(DateTime, default=datetime.utcnow)


class Database:
    def __init__(self):
        self.engine = create_engine(f"sqlite:///{DB_PATH}")
        Base.metadata.create_all(self.engine)

    def session(self) -> OrmSession:
        return OrmSession(self.engine)

    def is_email_processed(self, email_id: str) -> bool:
        with self.session() as s:
            return s.get(ProcessedEmail, email_id) is not None

    def mark_email_processed(self, email_id: str, thread_id: str, application_id: int):
        with self.session() as s:
            s.merge(ProcessedEmail(
                email_id=email_id,
                thread_id=thread_id,
                application_id=application_id,
            ))
            s.commit()

    def get_application(self, company: str, job_title: str) -> Application | None:
        with self.session() as s:
            return (
                s.query(Application)
                .filter_by(company=company, job_title=job_title)
                .first()
            )

    def upsert_application(self, app_data: dict) -> Application:
        """Create or update an application. Returns the updated object."""
        with self.session() as s:
            app = (
                s.query(Application)
                .filter_by(
                    company=app_data["company"],
                    job_title=app_data["job_title"],
                )
                .first()
            )
            if app is None:
                app = Application(**app_data)
                s.add(app)
            else:
                for k, v in app_data.items():
                    if v is not None:
                        setattr(app, k, v)
            s.commit()
            s.refresh(app)
            return app

    def get_stale_applications(self, threshold_days: int) -> list[Application]:
        """Return applications with no update in threshold_days that aren't terminal."""
        from datetime import timedelta
        from config import TERMINAL_STATUSES

        cutoff = datetime.utcnow() - timedelta(days=threshold_days)
        with self.session() as s:
            return (
                s.query(Application)
                .filter(
                    Application.date_updated < cutoff,
                    ~Application.status.in_(TERMINAL_STATUSES),
                )
                .all()
            )

    def update_application(self, app_id: int, **kwargs):
        with self.session() as s:
            app = s.get(Application, app_id)
            if app:
                for k, v in kwargs.items():
                    setattr(app, k, v)
                s.commit()

    def apply_batch(self, records: list[dict]) -> list[dict]:
        """Apply a batch of already-classified emails in a single transaction.

        Each record:
            {
                "email_id": str, "thread_id": str, "job_related": bool,
                # if job_related is True:
                "company": str, "job_title": str, "event_type": str,
                "location": str | None, "comp": str | None,
                "platform": str | None, "notes": str | None,
            }

        One commit for the whole batch, instead of a commit per email —
        used when a human (or an out-of-process classifier) has already
        produced the classifications and just needs them written.
        """
        from state_machine import StateMachine

        state_machine = StateMachine()
        results = []
        with self.session() as s:
            for rec in records:
                if not rec.get("job_related"):
                    s.merge(ProcessedEmail(
                        email_id=rec["email_id"],
                        thread_id=rec.get("thread_id", ""),
                        application_id=-1,
                    ))
                    results.append({"email_id": rec["email_id"], "application_id": -1, "status": "skipped"})
                    continue

                company = rec.get("company") or "Unknown"
                job_title = rec.get("job_title") or "Unknown Role"
                app = (
                    s.query(Application)
                    .filter_by(company=company, job_title=job_title)
                    .first()
                )
                current_status = app.status if app else None
                new_status = state_machine.transition(current_status, rec.get("event_type", "unknown"))
                now = datetime.utcnow()

                if app is None:
                    app = Application(
                        company=company,
                        job_title=job_title,
                        location=rec.get("location"),
                        status=new_status,
                        comp=rec.get("comp"),
                        ats_score=rec.get("ats_score"),
                        platform=rec.get("platform"),
                        date_applied=now,
                        date_updated=now,
                        notes=rec.get("notes"),
                    )
                    s.add(app)
                    s.flush()  # assign app.id without ending the transaction
                else:
                    app.status = new_status
                    app.date_updated = now
                    if rec.get("location"):
                        app.location = rec["location"]
                    if rec.get("comp"):
                        app.comp = rec["comp"]
                    if rec.get("platform"):
                        app.platform = rec["platform"]
                    if rec.get("notes"):
                        app.notes = rec["notes"]

                s.merge(ProcessedEmail(
                    email_id=rec["email_id"],
                    thread_id=rec.get("thread_id", ""),
                    application_id=app.id,
                ))
                results.append({"email_id": rec["email_id"], "application_id": app.id, "status": new_status})

            s.commit()
        return results
