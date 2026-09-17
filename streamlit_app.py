"""Streamlit UI for the Agentic Google Workspace Orchestrator.

Talks to the FastAPI backend (app/main.py) over HTTP - it holds no business logic of
its own, just: drive the Google OAuth login, hold the resulting session JWT in
st.session_state, and call POST /api/v1/sync/trigger, GET /api/v1/sync/status, and
POST /api/v1/query with it as a Bearer token.

Login flow: GET /api/v1/auth/google must be opened as a real browser navigation, not
a fetch/XHR - Google's consent screen doesn't return CORS headers, so a JS-driven
request (which is how Streamlit's own backend calls work, and how Swagger's "Try it
out" works) fails there. st.link_button renders a plain <a href> the browser
navigates directly, which works. After consent, Google redirects to our backend's
/callback; if FRONTEND_REDIRECT_URL is set in the backend's .env, that callback
redirects again, straight back here with the session token as a query param, which
we pick up below. If it isn't set, the callback just returns raw JSON instead - in
that case, paste the "session_token" value into the manual field in the sidebar.

Run with: streamlit run streamlit_app.py
"""

import httpx
import streamlit as st

st.set_page_config(page_title="Workspace Orchestrator", page_icon="📬", layout="wide")

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"


def _init_session_state() -> None:
    st.session_state.setdefault("api_base_url", DEFAULT_API_BASE_URL)
    st.session_state.setdefault("session_token", None)
    st.session_state.setdefault("user_email", None)
    st.session_state.setdefault("user_id", None)
    st.session_state.setdefault("conversation_id", None)
    st.session_state.setdefault("messages", [])  # [{role, content, intent?, actions_taken?}]


def _consume_oauth_redirect() -> None:
    """Pick up ?session_token=...&user_id=...&email=... if the backend's OAuth
    callback redirected here (see FRONTEND_REDIRECT_URL in app/config.py)."""
    params = st.query_params
    if "session_token" in params:
        st.session_state["session_token"] = params["session_token"]
        st.session_state["user_id"] = params.get("user_id")
        st.session_state["user_email"] = params.get("email")
        st.query_params.clear()  # don't leave the token sitting in the URL/browser history


def _auth_header() -> dict:
    return {"Authorization": f"Bearer {st.session_state['session_token']}"}


def _api_call(method: str, path: str, **kwargs) -> tuple[bool, dict | list | str]:
    """POST/GET against the backend. Returns (ok, body_or_error_message). Centralizing
    this means every call site handles connection errors, 401s, and 429s the same way
    instead of repeating try/except around every httpx call."""
    url = f"{st.session_state['api_base_url']}{path}"
    try:
        response = httpx.request(method, url, headers=_auth_header(), timeout=60, **kwargs)
    except httpx.ConnectError:
        return False, f"Couldn't reach the API at {url}. Is `uvicorn app.main:app` running?"
    except httpx.TimeoutException:
        return False, "Request timed out - the backend took too long to respond."

    if response.status_code == 401:
        st.session_state["session_token"] = None  # session expired/invalid - force re-login
        return False, "Session expired or invalid. Please log in again."
    if response.status_code == 429:
        return False, "Rate limit exceeded (100 queries/user/hour) - try again later."
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        return False, f"API error {response.status_code}: {detail}"

    return True, response.json()


def _render_sidebar() -> None:
    with st.sidebar:
        st.header("Connection")
        st.session_state["api_base_url"] = st.text_input("API base URL", value=st.session_state["api_base_url"])

        st.divider()
        st.header("Authentication")

        if st.session_state["session_token"]:
            st.success(f"Logged in as {st.session_state['user_email']}")
            if st.button("Log out"):
                for key in ("session_token", "user_email", "user_id", "conversation_id", "messages"):
                    st.session_state[key] = None if key != "messages" else []
                st.rerun()
        else:
            login_url = f"{st.session_state['api_base_url']}/api/v1/auth/google"
            # A plain st.link_button always opens in a new tab (target="_blank", not
            # overridable) - after Google redirects back through our callback, the
            # authenticated app would then load in THAT new tab, leaving this original
            # tab stuck on the logged-out view. target="_self" instead navigates this
            # same tab all the way through Google's consent screen and back, so the
            # login flow feels like it stays on one page.
            st.markdown(
                f'<a href="{login_url}" target="_self" '
                'style="display:inline-block;padding:0.5rem 1rem;background-color:#FF4B4B;'
                'color:white;border-radius:0.5rem;text-decoration:none;font-weight:600;">'
                "Login with Google</a>",
                unsafe_allow_html=True,
            )
            st.caption(
                "Navigates this tab to Google's consent screen. If your backend has "
                "`FRONTEND_REDIRECT_URL` set, you'll land back here automatically. "
                "Otherwise, paste the `session_token` from the JSON response below."
            )
            with st.expander("Paste session token manually"):
                token = st.text_input("session_token", type="password", key="manual_token")
                email = st.text_input("email (optional, for display only)", key="manual_email")
                if st.button("Use this token") and token:
                    st.session_state["session_token"] = token
                    st.session_state["user_email"] = email or "(unknown)"
                    st.rerun()


def _render_sync_panel() -> None:
    st.subheader("Data sync")
    col1, col2 = st.columns(2)

    with col1:
        if st.button("Trigger sync"):
            ok, body = _api_call("POST", "/api/v1/sync/trigger")
            if ok:
                st.success(f"Sync queued (task_id: {body.get('task_id')})")
            else:
                st.error(body)

    with col2:
        if st.button("Refresh status"):
            ok, body = _api_call("GET", "/api/v1/sync/status")
            if ok:
                st.session_state["sync_status"] = body
            else:
                st.error(body)

    if st.session_state.get("sync_status"):
        st.table(st.session_state["sync_status"])


def _render_chat_panel() -> None:
    st.subheader("Ask a question")

    for message in st.session_state["messages"]:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message.get("actions_taken"):
                st.caption("Actions taken: " + ", ".join(message["actions_taken"]))
            if message.get("intent"):
                with st.expander("Classified intent"):
                    st.json(message["intent"])

    query = st.chat_input("e.g. Cancel my Turkish Airlines flight")
    if not query:
        return

    st.session_state["messages"].append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            ok, body = _api_call(
                "POST",
                "/api/v1/query",
                json={"query": query, "conversation_id": st.session_state.get("conversation_id")},
            )
        if ok:
            st.session_state["conversation_id"] = body.get("conversation_id")
            st.markdown(body["response"])
            if body.get("actions_taken"):
                st.caption("Actions taken: " + ", ".join(body["actions_taken"]))
            with st.expander("Classified intent"):
                st.json(body["intent"])
            st.session_state["messages"].append(
                {
                    "role": "assistant",
                    "content": body["response"],
                    "actions_taken": body.get("actions_taken"),
                    "intent": body.get("intent"),
                }
            )
        else:
            st.error(body)
            st.session_state["messages"].append({"role": "assistant", "content": f"⚠️ {body}"})


def main() -> None:
    _init_session_state()
    _consume_oauth_redirect()
    _render_sidebar()

    st.title("📬 Agentic Google Workspace Orchestrator")

    if not st.session_state["session_token"]:
        st.info("Log in with Google from the sidebar to get started.")
        return

    _render_sync_panel()
    st.divider()
    _render_chat_panel()


if __name__ == "__main__":
    main()
