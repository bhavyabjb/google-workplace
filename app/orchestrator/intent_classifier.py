"""Intent Classifier: the first LLM call in the pipeline (User Query -> Intent Classifier
-> Query Planner -> ...). Turns a free-text query into the structured Intent shape given
in the assignment brief.

Prompt-engineering choices (this is called out in the brief as "critical"):
- Few-shot examples cover all three sample-query categories (single-service,
  multi-service, and the "hard cases": ambiguous entity, conversational reference,
  relative time) so the model has a concrete pattern to follow for each.
- We ground "now" explicitly (current UTC timestamp + the user's IANA timezone) in
  the prompt, because relative time phrases like "next Tuesday" or "tomorrow" are
  meaningless without a reference point - and the model has no other way to know
  today's date.
- We pass in the user's last 5 conversation turns so pronoun-like references ("that
  email about the proposal") can resolve against recent context instead of being
  flagged as unresolvable.
- The model is instructed to set needs_clarification=True (with a clarification
  question) rather than guess when a query is genuinely ambiguous (e.g. "move the
  meeting with John" with two Johns on the calendar) - this is what turns a "hard
  case" from a wrong silent action into a safe clarifying question.
- Write actions ("actions" in the output shape) are decided HERE, by the model, not
  reconstructed later from free text. The Query Planner (app/orchestrator/planner.py)
  used to keyword-match words like "cancel"/"move" out of the intent label, but the
  same word means different things per service (see the SERVICE_VERBS rule below) and
  free text is unbounded - the model already resolves that ambiguity when it reads
  the query, so it records the decision directly instead of the planner re-guessing it.
"""

import json
# json: (de)serializes the Intent dict for both the OpenAI response and the Redis cache.

import hashlib
# hashlib: builds a short Redis cache key from the (query + context) pair.

from app.cache.redis_client import get_redis
from app.config import get_settings
from app.llm_client import build_openai_client, parse_json_response
from app.schemas import Intent

settings = get_settings()
_client = build_openai_client()

INTENT_CACHE_TTL_SECONDS = 3600  # matches the brief's "intent classifications" caching hint

SYSTEM_PROMPT = """You are the intent classifier for a Google Workspace orchestrator that \
executes natural language queries across Gmail, Google Calendar, and Google Drive.

Given a user query, return ONLY a JSON object with this exact shape:
{
  "services": ["gmail" | "gcal" | "gdrive", ...],
  "intent": "short_snake_case_label",
  "entities": {"key": "value", ...},
  "steps": ["step_name", ...],
  "actions": [{"service": "gmail" | "gcal" | "gdrive", "verb": "...", "needs_context_from": ["gmail" | "gcal" | "gdrive", ...]}, ...],
  "needs_clarification": false,
  "clarification_question": null
}

Rules:
- "services" lists every Google service this query touches. Multiple services are
  common when one service's result is needed to act on another (e.g. finding a
  booking email, then finding the matching calendar event).
- "steps" should be short, ordered, human-readable step names describing what the
  orchestrator will do (e.g. "search_gmail_for_booking", "find_calendar_event") -
  this is for human readability only; it has no effect on what actually runs.
- "actions" lists every WRITE (mutating) step to perform. Leave it an empty list for
  read-only queries (searching/listing/summarizing - nothing to write). Each action's
  "service" must also appear in "services". Each action's "verb" MUST be one of the
  following - never invent a verb, and never pair a verb with a service that doesn't
  support it:
    - gmail: "draft" (Gmail ALWAYS drafts, never sends automatically - the user
      confirms and sends it themselves. Use "draft" for send/compose/cancel-by-email
      requests alike.)
    - gcal: "create", "update", "reschedule", "delete"
    - gdrive: "share", "move"
  "needs_context_from" lists which services' search/context results this action
  needs before it can run (usually just its own service, but can include others -
  e.g. a flight-cancellation email needs both the Gmail booking AND the Calendar
  event, so needs_context_from: ["gmail", "gcal"]).
- Disambiguating verbs that could mean different things:
    - "cancel"/"remove" applied to something with an associated email or booking
      (a flight, hotel, order, reservation) -> {"service": "gmail", "verb": "draft"}
      (draft a cancellation email; never touch the calendar event directly).
    - "cancel"/"remove" applied to a plain calendar event/meeting with no booking or
      email involved -> {"service": "gcal", "verb": "delete"}.
    - Changing a calendar event's time ("move it to 3pm", "reschedule", "push back")
      -> {"service": "gcal", "verb": "reschedule"}. Only use gdrive's "move" verb for
      relocating a Drive file between folders - never for calendar time changes.
- If the query is genuinely ambiguous and cannot be safely executed as-is (e.g. it
  references a person/thing that could match multiple items, with no way to
  disambiguate from the query or recent conversation context), set
  "needs_clarification" to true, provide a specific "clarification_question", and
  leave "actions" empty. Do not guess.
- Resolve relative time expressions ("tomorrow", "next week", "next Tuesday") using
  the CURRENT_DATETIME and USER_TIMEZONE given below, and put the resolved
  ISO 8601 date/range into "entities".
- Resolve vague references ("that email", "the proposal doc") using RECENT_CONTEXT
  if it clearly points to one specific prior item; otherwise ask for clarification.

Examples:

Query: "Cancel my Turkish Airlines flight"
{"services": ["gmail", "gcal"], "intent": "cancel_flight", "entities": {"airline": "Turkish Airlines"}, "steps": ["search_gmail_for_booking", "find_calendar_event", "draft_cancellation_email"], "actions": [{"service": "gmail", "verb": "draft", "needs_context_from": ["gmail", "gcal"]}], "needs_clarification": false, "clarification_question": null}

Query: "Cancel my 3pm meeting tomorrow"
{"services": ["gcal"], "intent": "cancel_event", "entities": {"date": "<tomorrow's ISO date, resolved from CURRENT_DATETIME>", "time": "15:00"}, "steps": ["find_calendar_event", "delete_calendar_event"], "actions": [{"service": "gcal", "verb": "delete", "needs_context_from": ["gcal"]}], "needs_clarification": false, "clarification_question": null}

Query: "Prepare for tomorrow's client meeting with Acme Corp"
{"services": ["gcal", "gmail", "gdrive"], "intent": "prepare_for_meeting", "entities": {"client": "Acme Corp", "date": "<tomorrow's ISO date, resolved from CURRENT_DATETIME>"}, "steps": ["find_calendar_event", "search_emails_with_client", "pull_drive_documents"], "actions": [], "needs_clarification": false, "clarification_question": null}

Query: "What's on my calendar next week where john@company.com is invited?"
{"services": ["gcal"], "intent": "list_events_by_attendee", "entities": {"attendee": "john@company.com", "date_range": "<next week's ISO start/end, resolved from CURRENT_DATETIME>"}, "steps": ["search_calendar", "filter_by_attendee", "return_formatted_list"], "actions": [], "needs_clarification": false, "clarification_question": null}

Query: "Move the meeting with John"
{"services": ["gcal"], "intent": "reschedule_event", "entities": {"attendee_hint": "John"}, "steps": [], "actions": [], "needs_clarification": true, "clarification_question": "You have multiple meetings with people named John. Which meeting, and what new time?"}

Query: "Move the Q3 budget spreadsheet to my Finance folder"
{"services": ["gdrive"], "intent": "move_file", "entities": {"doc_name": "Q3 budget spreadsheet", "target_folder": "Finance"}, "steps": ["search_drive_for_file", "move_file_to_folder"], "actions": [{"service": "gdrive", "verb": "move", "needs_context_from": ["gdrive"]}], "needs_clarification": false, "clarification_question": null}
"""


def _cache_key(query: str, context_signature: str) -> str:
    digest = hashlib.sha256(f"{query}|{context_signature}".encode()).hexdigest()
    return f"intent:{digest}"


def classify_intent(query: str, conversation_context: list[dict], user_timezone: str, now_iso: str) -> Intent:
    """Classify a natural-language query into a structured Intent.

    `conversation_context` is the list returned by app.cache.conversation.get_recent_context
    (most-recent-first, up to 5 turns). `now_iso`/`user_timezone` ground relative time phrases.
    """
    context_signature = json.dumps(conversation_context, sort_keys=True)
    redis = get_redis()
    cache_key = _cache_key(query, context_signature)

    cached = redis.get(cache_key)
    if cached is not None:
        return Intent.model_validate_json(cached)

    user_message = (
        f"CURRENT_DATETIME: {now_iso}\n"
        f"USER_TIMEZONE: {user_timezone}\n"
        f"RECENT_CONTEXT: {context_signature}\n"
        f"Query: {query}"
    )

    response = _client.chat.completions.create(
        model=settings.openai_chat_model,
        # json_object mode guarantees syntactically valid JSON back; we still validate
        # the *shape* ourselves with the Intent Pydantic model below.
        response_format={"type": "json_object"},
        temperature=0,  # deterministic classification - we want the same query to route the same way every time
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )

    raw = response.choices[0].message.content
    intent = Intent.model_validate(parse_json_response(raw))

    redis.set(cache_key, intent.model_dump_json(), ex=INTENT_CACHE_TTL_SECONDS)
    return intent
