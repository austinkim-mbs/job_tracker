"""
Stage 2 calibrated scorer: a Claude read of one posting against the resume,
using interview_funnel/prompts/prep/fit_scorer_prompt.md (requirements-first
knockout checks, four-part score with caps; stage2_scorer_prompt.md is v1) (calibrated
recruiter-fit bands, transferable full-stack rule, role_type, separate
desirability score). Stage 1 (score_bay_area_postings.py) decides which
postings are worth this call; `manage.py score_postings` runs it in bulk and
saves onto Posting; poll_applications.py and the eval harness call score().
"""

import hashlib
import html
import re
from pathlib import Path
from typing import Literal

import anthropic
import pdfplumber
from pydantic import BaseModel, Field

from config import ANTHROPIC_API_KEY, RESUME_PATH

MODEL = "claude-sonnet-4-6"
PROMPT_PATH = Path(__file__).resolve().parent / "interview_funnel" / "prompts" / "prep" / "fit_scorer_prompt.md"
PROMPT_VERSION = "stage2-v2"
SCORER_VERSION = f"{PROMPT_VERSION}:{MODEL}"

# Tier labels for recruiter_fit_score, derived in code (not by the model) so
# they can't drift from the number. 41-54 is the gap between the Borderline
# and Hard Reject bands.
TIERS = [(75, "must_pass"), (55, "borderline"), (41, "weak"), (0, "hard_reject")]

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class PostingScore(BaseModel):
    reasoning: str = Field(description="Work through role type, stack (direct/transferable/missing), seniority and hard constraints before choosing any number.")
    role_type: Literal["software", "hardware", "hybrid", "other"]
    ats_score: int = Field(description="0-100 literal keyword overlap, exact terms only.")
    recruiter_fit_score: int = Field(description="0-100 calibrated recruiter read; see bands.")
    desirability_score: int = Field(description="0-100 appeal of the role itself; independent of fit.")
    seniority_fit: Literal["strong", "moderate", "weak"]
    matched_skills: list[str]
    transferable_skills: list[str] = Field(description="JD stack items missing verbatim but covered by equivalent resume experience.")
    missing_skills: list[str] = Field(description="Real, non-transferable gaps.")
    hard_constraints: list[str] = Field(description="Location/clearance/citizenship/degree/YOE requirements and whether met, unmet or unknown.")
    desirability_notes: str
    fit_notes: str = Field(description="2-4 sentences: bottom line, main strength, main gap or knockout risk.")
    confidence: Literal["high", "medium", "low"]


_TAG_RE = re.compile(r"<[^>]+>")


def clean_job_text(text: str) -> str:
    """ATS descriptions are HTML, often entity-escaped twice ("&lt;li&gt;")."""
    text = _TAG_RE.sub(" ", html.unescape(html.unescape(text or "")))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def tier_for(score: int) -> str:
    return next(label for floor, label in TIERS if score >= floor)


class ResumeScorer:
    def __init__(self):
        self._resume_text: str | None = None
        self._resume_hash: str | None = None
        self._load_resume()

    def _load_resume(self):
        """Load the resume (PDF, or a plain-text .txt/.md file) and cache the
        text. Re-parse only if the file changes."""
        with open(RESUME_PATH, "rb") as f:
            raw = f.read()
        file_hash = hashlib.md5(raw).hexdigest()
        if file_hash == self._resume_hash:
            return
        self._resume_hash = file_hash
        if str(RESUME_PATH).lower().endswith((".txt", ".md")):
            self._resume_text = raw.decode("utf-8").strip()
            return
        with pdfplumber.open(RESUME_PATH) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        self._resume_text = "\n".join(pages).strip()

    def get_resume_text(self) -> str:
        """Public accessor for the cached resume text -- loads/refreshes first."""
        self._load_resume()
        return self._resume_text

    def score(self, job_description: str, title: str = "", location: str = "",
              company: str = "", comp: str = "") -> dict:
        """
        Calibrated Stage-2 score of the resume against one posting. The rubric
        and resume sit in a cached system prompt; only the posting changes
        per call.

        Returns PostingScore's fields plus:
            "tier": must_pass | borderline | weak | hard_reject
            "score": recruiter_fit_score   (back-compat: poll_applications, eval)
            "notes": fit_notes             (back-compat)
        """
        self._load_resume()
        system_text = (
            PROMPT_PATH.read_text(encoding="utf-8")
            + f"\n\n## Candidate resume\n{self._resume_text}"
        )
        header = "\n".join(
            f"{label}: {value}" for label, value in
            [("Company", company), ("Title", title), ("Location", location), ("Posted compensation", comp)]
            if value
        )
        user_text = f"{header}\n\nJob description:\n{clean_job_text(job_description)}".strip()

        response = client.messages.parse(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_text}],
            output_format=PostingScore,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"scoring refused (request {response._request_id})")
        if response.parsed_output is None:
            raise RuntimeError(f"no parsable score (stop_reason={response.stop_reason}, request {response._request_id})")

        data = response.parsed_output.model_dump()
        for key in ("ats_score", "recruiter_fit_score", "desirability_score"):
            data[key] = max(0, min(100, data[key]))
        if data["role_type"] in ("hardware", "other"):
            data["recruiter_fit_score"] = min(data["recruiter_fit_score"], 25)
        data["tier"] = tier_for(data["recruiter_fit_score"])
        data["score"] = data["recruiter_fit_score"]
        data["notes"] = data["fit_notes"]
        return data

    def score_posting(self, posting, save: bool = True) -> dict:
        """Score a tracker.models.Posting and (by default) write the results
        onto it so the SWE market dashboard's ATS/Fit columns populate."""
        comp = ""
        if posting.lowest_comp and posting.avg_comp and posting.lowest_comp != posting.avg_comp:
            comp = f"${posting.lowest_comp:,}+ (midpoint ${posting.avg_comp:,})"
        elif posting.avg_comp:
            comp = f"${posting.avg_comp:,}"
        result = self.score(
            posting.description, title=posting.title, location=posting.location,
            company=posting.company.name or posting.company.slug, comp=comp,
        )
        if save:
            posting.ats_score = result["ats_score"]
            posting.recruiter_fit_score = result["recruiter_fit_score"]
            posting.desirability_score = result["desirability_score"]
            posting.role_type = result["role_type"]
            posting.fit_notes = format_fit_notes(result)
            posting.fit_scorer_version = SCORER_VERSION
            posting.save(update_fields=[
                "ats_score", "recruiter_fit_score", "desirability_score",
                "role_type", "fit_notes", "fit_scorer_version",
            ])
        return result


def format_fit_notes(r: dict) -> str:
    def items(xs):
        return ", ".join(xs) if xs else "none"
    return "\n".join([
        f"[{r['tier']}] Fit {r['recruiter_fit_score']} | ATS {r['ats_score']} | "
        f"Desirability {r['desirability_score']} | seniority {r['seniority_fit']} | "
        f"confidence {r['confidence']} | {SCORER_VERSION}",
        r["fit_notes"],
        f"Matched: {items(r['matched_skills'])}",
        f"Transferable: {items(r['transferable_skills'])}",
        f"Missing: {items(r['missing_skills'])}",
        f"Constraints: {items(r['hard_constraints'])}",
        f"Desirability: {r['desirability_notes']}",
    ])
