import base64
import email
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from config import CREDENTIALS_PATH, TOKEN_PATH, GMAIL_QUERY
import os

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]


def get_google_credentials() -> Credentials:
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return creds


class GmailPoller:
    def __init__(self):
        creds = get_google_credentials()
        self.service = build("gmail", "v1", credentials=creds)

    def fetch_new_emails(self, db) -> list[dict]:
        """Return unprocessed emails matching the job query."""
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

    def _parse_message(self, msg: dict) -> dict | None:
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        subject = headers.get("Subject", "")
        sender = headers.get("From", "")
        date_str = headers.get("Date", "")
        thread_id = msg.get("threadId", "")

        body = self._extract_body(msg["payload"])

        return {
            "id": msg["id"],
            "thread_id": thread_id,
            "subject": subject,
            "sender": sender,
            "date": date_str,
            "body": body[:8000],  # cap at 8k chars before sending to Claude
        }

    def _extract_body(self, payload: dict) -> str:
        if "parts" in payload:
            for part in payload["parts"]:
                if part["mimeType"] == "text/plain":
                    data = part["body"].get("data", "")
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                if part["mimeType"] == "text/html":
                    data = part["body"].get("data", "")
                    html = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                    return self._strip_html(html)
        else:
            data = payload["body"].get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        return ""

    def _strip_html(self, html: str) -> str:
        import re
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
