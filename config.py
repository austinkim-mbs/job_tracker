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
