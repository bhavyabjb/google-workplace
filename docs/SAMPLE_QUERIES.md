# Sample Queries

13 worked examples: the assignment's own sample queries, its three "hard cases,"
plus a couple of extra edge cases (rate limiting, partial failure) that don't fit
neatly into either bucket. "Expected behavior" describes what the code actually
does (with file references), not just the LLM's guess - so this doubles as a map of
which module handles which case.

---

## Single-service

### 1. "What's on my calendar next week?"
- **Intent:** `{"services": ["gcal"], "intent": "list_events_by_date_range", "entities": {"date_range": {...resolved from CURRENT_DATETIME...}}, "actions": []}`
- **Plan:** `build_plan` (`app/orchestrator/planner.py`) builds a `search_gcal` + `context_gcal` pair for every service in `services` - here just `gcal`. No `actions` were given, so no `execute` node.
- **Execution:** `GCalAgent.search` resolves the date range (`_resolve_date_range`) and, since there's no semantic hint in the entities, skips the embedding call entirely and just filters+sorts by `start_time` (`app/embeddings/search.py:search_gcal`) - a pure metadata query.
- **Expected response:** a plain-language list of events with times, no vector search overhead.

### 2. "Find emails from sarah@company.com about the budget"
- **Intent:** `{"services": ["gmail"], "intent": "search_emails", "entities": {"sender": "sarah@company.com", "topic": "budget"}}`
- **Plan:** one `search` node on `gmail`.
- **Execution:** `GmailAgent.search` filters by `sender` (indexed `ILIKE`) first, then ranks the (much smaller) result set by cosine distance to an embedding of `"search_emails budget"`.

### 3. "Show me PDFs in Drive from last month"
- **Intent:** `{"services": ["gdrive"], "intent": "search_files", "entities": {"file_type": "pdf", "modified_after": "<ISO date 1 month ago>"}}`
- **Plan:** one `search` node on `gdrive`.
- **Execution:** `DriveAgent.search` maps `"pdf"` -> `application/pdf` via `FILE_TYPE_TO_MIME` and filters `mime_type` + `modified_at` before any ranking.

---

## Multi-service

### 4. "Cancel my Turkish Airlines flight"
- **Intent:** `{"services": ["gmail", "gcal"], "intent": "cancel_flight", "entities": {"airline": "Turkish Airlines"}, "actions": [{"service": "gmail", "verb": "draft", "needs_context_from": ["gmail", "gcal"]}]}` - the classifier decides the write action directly (see `Action` in `app/schemas.py`) rather than the planner inferring it from the word "cancel."
- **Plan:** `build_plan` creates `search_gmail`/`context_gmail` and `search_gcal`/`context_gcal` in parallel (one pair per service in `services`), then one `execute_draft_gmail` node depending on both `context_gmail` and `context_gcal` (from the action's `needs_context_from`) - it drafts (not sends) a cancellation email via `GmailAgent.execute`.
- **Expected response** (matches the brief's own example):
  > I found your Turkish Airlines booking (TK1234) in an email from Oct 15.
  > ✓ Calendar event "Istanbul → NYC Flight" on Nov 5 at 10:30 AM
  > ✓ Drafted cancellation email to support@turkishairlines.com
  >
  > Would you like me to send it?
- Note the email is **drafted, not sent** - `GmailAgent.execute` only ever implements the `draft` verb (see `SERVICE_VERBS` in `app/schemas.py`); sending would need an explicit follow-up query and confirmation. This is a deliberate safety choice, not a gap: irreversible actions get a confirmation step.

### 5. "Prepare for tomorrow's meeting with Acme Corp"
- **Intent:** `{"services": ["gcal", "gmail", "gdrive"], "intent": "prepare_for_meeting", "entities": {"client": "Acme Corp", "date": "<tomorrow>"}, "actions": []}` - purely read-only, so no actions.
- **Plan:** `build_plan` creates a `search`+`context` pair for all three services, **run fully in parallel with no cross-service ordering** - `search_gmail`/`search_gdrive` do NOT wait on `context_gcal` to resolve attendee emails first. This is a known tradeoff of the fully-generic planner (see the module docstring in `app/orchestrator/planner.py`): an earlier version hand-sequenced this specific case, but that doesn't generalize to arbitrary intents, so the current planner treats every service's search as independent. A more complete fix would have the classifier emit explicit read-ordering, not just write actions.
- **Expected response:** a synthesized brief combining the meeting time/attendees, relevant email thread(s), and any matching Drive docs.

### 6. "Find events next week that conflict with my out-of-office doc"
- **Intent:** `{"services": ["gcal", "gdrive"], "intent": "find_conflicting_events", "entities": {"date_range": "<next week>", "doc_name": "out-of-office"}, "actions": []}` - purely read-only.
- **Plan:** `build_plan` creates parallel `search_gcal`/`context_gcal` + `search_gdrive`/`context_gdrive`; no `actions` given, so no `execute` node.
- **Execution:** `DriveAgent.search` finds the out-of-office doc; `GCalAgent.search` lists next week's events. Conflict detection itself (comparing the OOO date range against event times) happens in the Response Synthesizer's prompt, which is given both raw result sets and asked to identify overlaps - see the "Bonus: Conflict detection" note in DESIGN.md/README for why this is prompt-based rather than a separate deterministic step in this version.

---

## Hard cases (from the brief's "What Makes This Hard")

### 7. "Move the meeting with John" (ambiguous entity)
- **Intent:** the classifier is instructed (see `SYSTEM_PROMPT` in `app/orchestrator/intent_classifier.py`) to set `needs_clarification: true` when a reference can't be disambiguated from the query or recent context - e.g. two upcoming meetings both have a "John" attendee.
- **Behavior:** `app/orchestrator/pipeline.py:run_query` checks `intent.needs_clarification` and returns the model's `clarification_question` directly - **no plan is built, no Google API call is made.** This is the key safety property: an ambiguous write request never guesses.
- **Expected response:** `"You have multiple meetings with people named John. Which meeting, and what new time?"`

### 8. "That email about the proposal" (conversational reference)
- **Behavior:** `classify_intent` is given `RECENT_CONTEXT` - the user's last 5 turns (`app/cache/conversation.py:get_recent_context`). If a recent turn clearly referenced one specific email (e.g. the previous query was "find emails about the Q3 proposal" and returned exactly one hit), the classifier resolves "that email" to that email's identifying details in `entities` instead of asking for clarification. If nothing in the last 5 turns disambiguates it, `needs_clarification` is set instead (same path as case 7) - a silent wrong guess is never made either way.

### 9. "Next Tuesday" (temporal reasoning)
- **Behavior:** `classify_intent`'s prompt includes `CURRENT_DATETIME` (real UTC "now," not the model's training-data notion of "today") and `USER_TIMEZONE` (`User.timezone`, defaults to `"UTC"`), with an explicit instruction to resolve relative dates into ISO 8601 using those two values. `app/orchestrator/agents/gcal_agent.py:_resolve_date_range` then defensively parses whatever shape comes back (a `{"start": ..., "end": ...}` dict, a `"start/end"` string, or a single `"date"`), falling back to a 30-day window if the LLM's date resolution is somehow missing - so a malformed date never crashes the query, it just widens the search.

---

## Additional edge cases

### 10. "What's on my calendar next week where john@company.com is invited?"
- **Intent:** `{"services": ["gcal"], "intent": "list_events_by_attendee", "entities": {"attendee": "john@company.com", "date_range": "<next week>"}, "actions": []}`
- **Plan:** `build_plan` creates `search_gcal` + `context_gcal`. Note the current generic planner always builds a context node per service, even for a "list many things" query where zooming into just the top hit isn't that useful - a minor, accepted inefficiency (one extra live Calendar API call) versus the old hand-written template, which specifically omitted it for this case.
- **Execution:** `search_gcal` applies BOTH the date-range filter AND a JSONB `@>` containment filter on `attendees` (`app/embeddings/search.py`) before returning - two cheap metadata filters, no vector ranking needed at all.

### 11. "Share the Q3 report with john@company.com"
- **Intent:** `{"services": ["gdrive"], "intent": "share_q3_report", "entities": {"doc_name": "Q3 report", "share_with": "john@company.com"}, "actions": [{"service": "gdrive", "verb": "share", "needs_context_from": ["gdrive"]}]}` - the classifier names the write action directly instead of the planner keyword-matching "share" out of the intent label.
- **Plan:** `build_plan` creates `search_gdrive` + `context_gdrive` (finds the file), then `execute_share_gdrive` depending on `context_gdrive`.
- **Execution:** `DriveAgent.execute` resolves the target file id from `upstream_context`, calls `DriveClient.share_file`, and records the grant in `audit_log` regardless of outcome.

### 12. Rate limit exceeded
- **Request:** a user's 101st query within a rolling hour.
- **Behavior:** `app/api/routes_query.py` calls `check_and_increment` (Redis `INCR`, fixed hourly window) **before** any LLM/Google API work - fails fast and cheaply.
- **Response:** `429 Too Many Requests`, `{"detail": "User <id> exceeded 100 queries/hour"}`.

### 13. Partial cross-service failure
- **Scenario:** "Cancel my Turkish Airlines flight" where the Gmail search succeeds but the Calendar API times out (transient 503, exhausts `app/google/retry.py`'s 4 attempts).
- **Behavior:** `execute_plan` marks `search_gcal` as `status="error"`; `search_gmail` and `context_gmail` (independent of the failed node) still complete normally; `draft_cancellation_email` depends on both, so it's marked `status="skipped"` rather than running with incomplete data.
- **Expected response:** the Response Synthesizer is instructed to acknowledge gaps plainly rather than pretend success - e.g. *"I found your Turkish Airlines booking, but couldn't check your calendar right now (the service timed out), so I haven't drafted a cancellation email yet. Want me to try again?"*
