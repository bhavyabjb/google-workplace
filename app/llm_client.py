"""Shared OpenAI SDK client + response-parsing helpers.

Every LLM/embedding call site in this app (app/embeddings/embedder.py,
app/orchestrator/intent_classifier.py, app/orchestrator/synthesizer.py,
app/orchestrator/agents/gmail_agent.py) builds its client through `build_openai_client()`
instead of constructing `OpenAI(...)` directly, and parses JSON replies through
`parse_json_response()` instead of a bare `json.loads()`. That means switching to any
OpenAI-compatible provider (Gemini, Groq, a local vLLM server, ...) - or working around
a provider's quirks - is a change in this one file, not four.

Why this exists right now: Gemini exposes an OpenAI-compatible endpoint
(https://ai.google.dev/gemini-api/docs/openai) - same OpenAI Python SDK, just a
different `base_url`/`api_key`/model name, which is what OPENAI_BASE_URL below is for.
But it has a documented quirk: `response_format={"type": "json_object"}` is silently
ignored on at least some Gemini models (see
https://github.com/Wei-Shaw/sub2api/issues/7088), so the model can reply with prose or
a ```json fenced block instead of a bare JSON object - something real OpenAI models
essentially never do once that mode is requested. `parse_json_response` tolerates
that by falling back to extracting the first {...} block if a direct parse fails,
rather than the caller crashing on a provider quirk outside its control.
"""

import json
import re

from openai import OpenAI

from app.config import get_settings


def build_openai_client() -> OpenAI:
    """Build an OpenAI SDK client, pointed at OPENAI_BASE_URL if set (e.g. Gemini's
    OpenAI-compatible endpoint), or real OpenAI if unset (base_url=None is the SDK's
    own default)."""
    settings = get_settings()
    return OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)


def parse_json_response(raw: str) -> dict:
    """Parse a chat completion's content as JSON, tolerating providers that ignore
    response_format={"type": "json_object"} and wrap the JSON in prose or a fenced
    code block instead of returning it bare. Raises the original JSONDecodeError if
    no JSON object can be recovered at all, so a genuinely broken response still
    fails loudly rather than being swallowed.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))
