import os
from dotenv import load_dotenv

# Load .env from the job_tracker directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Paths
RESUME_PATH = os.environ.get(
    "RESUME_PATH",
    os.path.join(BASE_DIR, "..", "Resume v12 (1).pdf"),
)
DB_PATH = os.path.join(BASE_DIR, "job_tracker.db")
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials", "google_oauth.json")
TOKEN_PATH = os.path.join(BASE_DIR, "credentials", "token.json")

# Google
SHEET_ID = os.environ.get("SHEET_ID", "")
SHEET_NAME = "Applications"

# Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Classifier backend — "claude" or "ollama"
CLASSIFIER_BACKEND = os.environ.get("CLASSIFIER_BACKEND", "claude")

# Ollama settings (used when CLASSIFIER_BACKEND="ollama")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

# Polling
POLL_INTERVAL_SECONDS = 300  # every 5 minutes

# Gmail search query — catches Ashby, Greenhouse, and general job emails
GMAIL_QUERY = (
    'subject:("application" OR "interview" OR "offer" OR "position" OR "role") '
    'newer_than:1d'
)

# State machine
GHOSTING_THRESHOLD_DAYS = 21
TERMINAL_STATUSES = {"Accepted", "Rejected", "Ghosted"}

# Known ATS sender domains
ATS_PLATFORMS = {
    "ashbyhq.com": "Ashby",
    "greenhouse.io": "Greenhouse",
    "lever.co": "Lever",
    "myworkday.com": "Workday",
    "icims.com": "iCIMS",
    "jobvite.com": "Jobvite",
    "smartrecruiters.com": "SmartRecruiters",
}

# Sheet column indices (0-based internally, 1-based for Sheets API)
COL_COMPANY = 0
COL_JOB_TITLE = 1
COL_LOCATION = 2
COL_STATUS = 3
COL_COMP = 4
COL_ATS_SCORE = 5
COL_DATE_APPLIED = 6
COL_DATE_UPDATED = 7
COL_PLATFORM = 8
COL_NOTES = 9
TOTAL_COLS = 10

# Status colors (RGB 0-1 floats for Sheets API)
STATUS_COLORS = {
    "Applied":      {"red": 0.733, "green": 0.871, "blue": 0.984},  # #BBDEFB light blue
    "Screening":    {"red": 1.0,   "green": 0.976, "blue": 0.769},  # #FFF9C4 yellow
    "Interviewing": {"red": 1.0,   "green": 0.878, "blue": 0.706},  # #FFE0B2 orange
    "Offer":        {"red": 0.784, "green": 0.902, "blue": 0.788},  # #C8E6C9 green
    "Accepted":     {"red": 0.220, "green": 0.557, "blue": 0.235},  # #388E3C dark green
    "Rejected":     {"red": 1.0,   "green": 0.804, "blue": 0.820},  # #FFCDD2 red
    "Ghosted":      {"red": 0.933, "green": 0.933, "blue": 0.933},  # #EEEEEE gray
}
