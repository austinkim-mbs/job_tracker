import json
import hashlib
import pdfplumber
import anthropic
from config import ANTHROPIC_API_KEY, RESUME_PATH

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class ResumeScorer:
    def __init__(self):
        self._resume_text: str | None = None
        self._resume_hash: str | None = None
        self._load_resume()

    def _load_resume(self):
        """Parse resume PDF and cache the text. Re-parse only if file changes."""
        with open(RESUME_PATH, "rb") as f:
            raw = f.read()

        file_hash = hashlib.md5(raw).hexdigest()
        if file_hash == self._resume_hash:
            return

        self._resume_hash = file_hash
        with pdfplumber.open(RESUME_PATH) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        self._resume_text = "\n".join(pages).strip()

    def get_resume_text(self) -> str:
        """Public accessor for the cached resume text -- loads/refreshes first."""
        self._load_resume()
        return self._resume_text

    def score(self, job_description: str) -> dict:
        """
        ATS-style score of the resume against a job description.

        The resume text is placed in the system prompt with cache_control so it's
        cached across all scoring calls. Only the JD changes per call.

        Returns:
            {
                "score": int (0-100),
                "matched_skills": list[str],
                "missing_skills": list[str],
                "seniority_fit": str,
                "notes": str,
                "confidence": str  ("high" | "medium" | "low")
            }
        """
        self._load_resume()  # re-check if file changed

        system_text = (
            "You are an ATS scoring assistant. Compare the candidate's resume "
            "against job descriptions and return structured JSON only.\n\n"
            f"CANDIDATE RESUME:\n{self._resume_text}"
        )

        user_text = (
            "Score the candidate's resume against this job description. "
            "Return valid JSON only — no prose, no markdown fences:\n"
            "{\n"
            '  "score": <integer 0-100>,\n'
            '  "matched_skills": ["skill1", "skill2"],\n'
            '  "missing_skills": ["skill3"],\n'
            '  "seniority_fit": "strong | moderate | weak",\n'
            '  "notes": "<2-3 sentence summary of fit>",\n'
            '  "confidence": "high | medium | low"\n'
            "}\n\n"
            f"JOB DESCRIPTION:\n{job_description[:4000]}"
        )

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=800,
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_text}],
        )

        text = next(
            (b.text for b in response.content if b.type == "text"), ""
        ).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            import re
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                return json.loads(match.group())
            return {
                "score": 0,
                "matched_skills": [],
                "missing_skills": [],
                "seniority_fit": "unknown",
                "notes": "Could not parse score response.",
                "confidence": "low",
            }
