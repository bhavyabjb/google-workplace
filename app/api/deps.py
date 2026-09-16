"""Shared FastAPI dependencies for the API routes.

`get_current_user` is the multi-tenant isolation boundary the brief's "Security"
section calls for: every request that touches user data must go through this, so
there is exactly one place that maps "the Bearer token on this request" to "the
User row (and therefore the Google credentials) this request is allowed to act as."
Route handlers never accept a user_id directly from the client for this reason.
"""

from fastapi import Depends, Header, HTTPException, status
# Header: pulls the raw `Authorization` header value out of the request.
# HTTPException/status: how we reject unauthenticated/invalid-session requests.

from sqlalchemy.orm import Session

from app.db.models import User
from app.db.session import get_db
from app.security import decode_session_token


def get_current_user(authorization: str = Header(default=None), db: Session = Depends(get_db)) -> User:
    """Resolve the User for this request from the `Authorization: Bearer <jwt>` header.

    Raises 401 if the header is missing/malformed, the JWT is invalid/expired, or the
    user id it names no longer exists (e.g. deleted between login and this request).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or malformed Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    user_id = decode_session_token(token)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session token")

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")

    return user
