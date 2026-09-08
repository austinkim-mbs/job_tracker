import json
import re
import requests
import anthropic
from config import (
    ANTHROPIC_API_KEY,
    ATS_PLATFORMS,
    CLASSIFIER_BACKEND,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
)

_anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """\
You are an expert at parsing job application emails. Given an email, extract structured information and return valid JSON only — no prose, no markdown fences.

Return this exact shape:
{
  "platform": "Ashby | Greenhouse | Lever | Workday | iCIMS | Other",
  "event_type": "application_received | recruiter_outreach | phone_screen | interview_scheduled | technical_assessment | offer | rejection | unknown",
  "company": "<company name or null>",
  "job_title": "<job title or null>",
  "location": "<city, state or Remote or null>",
  "comp": "<salary/comp range as string or null>",
  "job_description": "<job description text if present in email, else null>",
  "notes": "<1-2 sentence summary of this email>",
  "confidence": <integer 0-100>
}

Rules:
- If the email is clearly not job-related, return confidence < 30.
- For event_type: application_received means a confirmation you applied; recruiter_outreach means someone reached out to you; rejection means they declined you.
- Extract comp only if explicitly stated (e.g. "$150k-180k", "$80/hr").
- For job_description: only include if the email contains an actual job description with responsibilities/requirements.
"""


def detect_platform_from_sender(sender: str) -> str | None:
    sender_lower = sender.lower()
    for domain, platform in ATS_PLATFORMS.items():
        if domain in sender_lower:
            return platform
    return None


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


def _classify_claude(user_content: str) -> dict | None:
    """Call Claude with prompt caching on the system prompt."""
    response = _anthropic_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1000,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    return _parse_json(text)


def _classify_ollama(user_content: str) -> dict | None:
    """Call a local Ollama model via its generate endpoint with JSON mode."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": f"{SYSTEM_PROMPT}\n\n{user_content}",
        "format": "json",   # Ollama's built-in JSON mode
        "stream": False,
        "options": {"temperature": 0},
    }
    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/generate",
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    return _parse_json(resp.json().get("response", ""))


class EmailClassifier:
    def classify(self, email: dict) -> dict | None:
        """
        Returns parsed job data or None if not job-related.

        Backend is controlled by CLASSIFIER_BACKEND env var:
          "claude"  → Anthropic API with prompt caching (default)
          "ollama"  → local Ollama model (set OLLAMA_MODEL to e.g. qwen2.5:7b)
        """
        platform_hint = detect_platform_from_sender(email["sender"])
        platform_context = f"Sender platform hint: {platform_hint}. " if platform_hint else ""

        user_content = (
            f"{platform_context}"
            f"Subject: {email['subject']}\n"
            f"From: {email['sender']}\n"
            f"Date: {email['date']}\n\n"
            f"Body:\n{email['body']}"
        )

        if CLASSIFIER_BACKEND == "ollama":
            parsed = _classify_ollama(user_content)
        else:
            parsed = _classify_claude(user_content)

        if parsed is None:
            return None

        if parsed.get("confidence", 0) < 40:
            return None

        if platform_hint:
            parsed["platform"] = platform_hint

        return parsed
