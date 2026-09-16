"""Response Synthesizer: NodeResults -> natural-language output, the last stage of
the pipeline in the brief's architecture diagram. Aggregates whatever every agent
found/did across possibly-multiple services into one coherent answer, in the style
of the brief's example:

    "I found your Turkish Airlines booking (TK1234) in an email from Oct 15.
     [check] Calendar event 'Istanbul -> NYC Flight' on Nov 5 at 10:30 AM
     [check] Drafted cancellation email to support@turkishairlines.com
     Would you like me to send it?"
"""

import json

from openai import OpenAI

from app.config import get_settings
from app.schemas import Intent, NodeResult

settings = get_settings()
_client = OpenAI(api_key=settings.openai_api_key)

SYSTEM_PROMPT = """You are the response synthesizer for a Google Workspace orchestrator.
You are given the user's original query, the classified intent, and the raw results of
every step the orchestrator ran (search hits, fetched context, and any write actions
performed). Write a short, natural-language reply to the user summarizing exactly what
was found and done. Rules:
- Only state facts present in the provided results. Never invent booking numbers,
  dates, names, or email addresses that aren't in the data.
- If a step has status "error" or "skipped", acknowledge the gap plainly (e.g. "I
  couldn't find a matching calendar event") rather than pretending it succeeded.
- If a write action (draft/create/update/delete/share/move) was performed, mention it
  explicitly and, for anything not yet sent/finalized (e.g. a drafted-but-unsent
  email), ask the user to confirm before it goes out.
- Return ONLY a JSON object: {"response": "<the reply text>", "actions_taken": ["<short phrase>", ...]}.
  "actions_taken" should list only things actually done (found X, drafted Y), not steps that failed.
"""


def synthesize_response(original_query: str, intent: Intent, results: dict[str, NodeResult]) -> tuple[str, list[str]]:
    """Turn the executor's raw NodeResults into a (response_text, actions_taken) pair."""
    serialized_results = {
        node_id: {"status": r.status, "data": r.data, "error": r.error} for node_id, r in results.items()
    }

    user_message = (
        f"Original query: {original_query}\n"
        f"Classified intent: {intent.model_dump_json()}\n"
        f"Step results: {json.dumps(serialized_results, default=str)}"
    )

    response = _client.chat.completions.create(
        model=settings.openai_chat_model,
        response_format={"type": "json_object"},
        temperature=0.2,  # small amount of freedom for natural phrasing, but still mostly deterministic
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )

    parsed = json.loads(response.choices[0].message.content)
    return parsed.get("response", ""), parsed.get("actions_taken", [])
