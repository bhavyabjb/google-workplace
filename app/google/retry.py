"""Shared retry/backoff policy for calling Google APIs.

The assignment brief calls this out explicitly: "Google APIs fail often - implement
retry with backoff." Transient failures (429 rate-limited, 500/502/503/504 server-side
hiccups) are common and usually resolve themselves a few hundred ms later; anything
else (401 unauthenticated, 403 permission denied, 404 not found) is a real error and
retrying it would just waste time and quota, so we only retry the transient set.
"""

from googleapiclient.errors import HttpError
# HttpError: what googleapiclient raises for any non-2xx response; `.resp.status`
# holds the HTTP status code we inspect below.

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
# retry: decorator that wraps a function with the policy below.
# retry_if_exception: lets us retry based on a custom predicate (is_retryable_error)
#   rather than just "retry on any exception of this type".
# stop_after_attempt: caps total attempts so a persistently-broken call doesn't hang forever.
# wait_exponential: exponential backoff between attempts (with jitter-free base here;
#   Google's own client already adds some jitter internally for batch/media calls).

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def is_retryable_error(exc: BaseException) -> bool:
    """True for rate-limit/server errors worth retrying; False for auth/permission/not-found."""
    if isinstance(exc, HttpError):
        return exc.resp is not None and exc.resp.status in RETRYABLE_STATUS_CODES
    # Network-level errors (timeouts, connection resets) surface as generic
    # ConnectionError/TimeoutError from the underlying httplib2 transport - also worth retrying.
    return isinstance(exc, (ConnectionError, TimeoutError))


# Applied as `@google_api_retry` on every method in gmail_client/gcal_client/drive_client
# that makes a real network call to Google. 4 attempts total, waiting 1s, 2s, 4s, 8s
# (capped at 10s) between them - keeps a single orchestration step from stalling too long
# while still giving transient errors room to clear.
google_api_retry = retry(
    retry=retry_if_exception(is_retryable_error),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(4),
    reraise=True,  # if all attempts fail, raise the original HttpError rather than a RetryError
)
