import json
from pathlib import Path

import anthropic
from django.http import HttpResponseNotAllowed, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render

from config import ANTHROPIC_API_KEY
from resume_scorer import ResumeScorer
from tracker.models import Posting

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "prep" / "resume_chat.md"
MODEL = "claude-opus-4-6"

# Loads + caches the resume PDF once per process, same as resume_scorer.py's
# own module-level usage elsewhere.
_scorer = ResumeScorer()
_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _system_blocks(posting: Posting):
    instructions = PROMPT_PATH.read_text(encoding="utf-8")
    resume_text = _scorer.get_resume_text()
    return [
        {
            # Instructions + resume are stable across turns and across
            # postings -- cache them so a multi-turn conversation isn't
            # re-billing full price for the same resume text every message.
            "type": "text",
            "text": f"{instructions}\n\nCANDIDATE RESUME:\n{resume_text}",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"JOB POSTING -- {posting.title} @ {posting.company.name}\n\n{posting.description[:6000]}",
        },
    ]


def chat_index(request):
    postings = (
        Posting.objects.filter(title__icontains="software engineer")
        .select_related("company")
        .order_by("-posted_at")[:20]
    )
    return render(request, "interview_funnel/chat_index.html", {"postings": postings})


def chat_view(request, posting_id):
    posting = get_object_or_404(Posting.objects.select_related("company"), id=posting_id)
    return render(request, "interview_funnel/chat.html", {"posting": posting})


def chat_stream(request, posting_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    posting = get_object_or_404(Posting.objects.select_related("company"), id=posting_id)
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    messages = payload.get("messages") or []

    def event_stream():
        try:
            with _client.messages.stream(
                model=MODEL,
                max_tokens=1024,
                system=_system_blocks(posting),
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'delta': text})}\n\n"
            yield "data: [DONE]\n\n"
        except anthropic.AuthenticationError:
            yield f"data: {json.dumps({'error': 'ANTHROPIC_API_KEY is invalid -- update .env and restart the server.'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
