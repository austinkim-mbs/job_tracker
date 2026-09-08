from datetime import datetime
from googleapiclient.discovery import build
from gmail_poller import get_google_credentials
from config import (
    SHEET_ID,
    SHEET_NAME,
    STATUS_COLORS,
    TOTAL_COLS,
    COL_COMPANY,
    COL_JOB_TITLE,
    COL_LOCATION,
    COL_STATUS,
    COL_COMP,
    COL_ATS_SCORE,
    COL_DATE_APPLIED,
    COL_DATE_UPDATED,
    COL_PLATFORM,
    COL_NOTES,
)

HEADER_ROW = [
    "Company", "Job Title", "Location", "Status",
    "Comp Range", "ATS Score", "Date Applied", "Date Updated",
    "Platform", "Notes",
]

DATE_FMT = "%Y-%m-%d"


class SheetsWriter:
    def __init__(self):
        creds = get_google_credentials()
        self.service = build("sheets", "v4", credentials=creds)
        self.sheet = self.service.spreadsheets()
        self._ensure_header()

    def _ensure_sheet_tab(self):
        """Create the sheet tab if it doesn't exist."""
        meta = self.sheet.get(spreadsheetId=SHEET_ID).execute()
        existing = [s["properties"]["title"] for s in meta["sheets"]]
        if SHEET_NAME not in existing:
            self.sheet.batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]},
            ).execute()

    def _ensure_header(self):
        """Create the sheet tab if needed, then write the header row if empty."""
        self._ensure_sheet_tab()
        result = (
            self.sheet.values()
            .get(spreadsheetId=SHEET_ID, range=f"{SHEET_NAME}!A1:J1")
            .execute()
        )
        if not result.get("values"):
            self._write_row(1, HEADER_ROW)

    def _write_row(self, row_number: int, values: list):
        range_name = f"{SHEET_NAME}!A{row_number}:{chr(ord('A') + TOTAL_COLS - 1)}{row_number}"
        self.sheet.values().update(
            spreadsheetId=SHEET_ID,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body={"values": [values]},
        ).execute()

    def _find_row(self, company: str, job_title: str) -> int | None:
        """Return 1-based row number for an existing application, or None."""
        result = (
            self.sheet.values()
            .get(spreadsheetId=SHEET_ID, range=f"{SHEET_NAME}!A:B")
            .execute()
        )
        rows = result.get("values", [])
        for i, row in enumerate(rows):
            if len(row) >= 2 and row[0] == company and row[1] == job_title:
                return i + 1  # 1-based
        return None

    def _next_empty_row(self) -> int:
        result = (
            self.sheet.values()
            .get(spreadsheetId=SHEET_ID, range=f"{SHEET_NAME}!A:A")
            .execute()
        )
        return len(result.get("values", [])) + 1

    def upsert(self, app) -> int:
        """Write or update an application row. Returns the row number."""
        row_num = app.sheet_row or self._find_row(app.company, app.job_title)

        def fmt_date(d):
            return d.strftime(DATE_FMT) if d else ""

        values = [None] * TOTAL_COLS
        values[COL_COMPANY] = app.company or ""
        values[COL_JOB_TITLE] = app.job_title or ""
        values[COL_LOCATION] = app.location or ""
        values[COL_STATUS] = app.status or ""
        values[COL_COMP] = app.comp or ""
        values[COL_ATS_SCORE] = app.ats_score if app.ats_score is not None else ""
        values[COL_DATE_APPLIED] = fmt_date(app.date_applied)
        values[COL_DATE_UPDATED] = fmt_date(app.date_updated)
        values[COL_PLATFORM] = app.platform or ""
        values[COL_NOTES] = app.notes or ""

        if row_num is None:
            row_num = self._next_empty_row()

        self._write_row(row_num, values)
        self._apply_color(row_num, app.status)

        return row_num

    def _apply_color(self, row_number: int, status: str):
        color = STATUS_COLORS.get(status, STATUS_COLORS["Applied"])
        sheet_id = self._get_sheet_id()

        requests = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_number - 1,  # 0-based
                        "endRowIndex": row_number,
                        "startColumnIndex": 0,
                        "endColumnIndex": TOTAL_COLS,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color,
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        ]

        self.sheet.batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": requests},
        ).execute()

    def _get_sheet_id(self) -> int:
        """Return the numeric sheetId for SHEET_NAME."""
        meta = self.sheet.get(spreadsheetId=SHEET_ID).execute()
        for s in meta["sheets"]:
            if s["properties"]["title"] == SHEET_NAME:
                return s["properties"]["sheetId"]
        raise ValueError(f"Sheet '{SHEET_NAME}' not found in spreadsheet.")
