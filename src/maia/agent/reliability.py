"""HTTP failure classification for outbound provider calls.

WHY THIS IS A SEPARATE MODULE
-----------------------------
`maia.loops.resilience.RetryConfig.retryable` is an exception-TYPE tuple, and
for `requests` that tuple cannot distinguish a 503 from a 401:

    requests.exceptions.HTTPError.__mro__
      HTTPError -> RequestException -> OSError -> Exception

Because `HTTPError` subclasses `OSError`, a config of
`(TimeoutError, ConnectionError, OSError)` retries EVERY http error status.
That makes a malformed request (400) or a rejected credential (401) retryable,
which is not resilience — it is hammering a permanent failure and, for 429,
burning someone else's quota faster.

So the type tuple stays as the necessary-but-insufficient first filter, and this
module supplies the rule it cannot express: which STATUS CODES are worth
another attempt.

    retryable -> 408, 429, and any 5xx   (transient; the request may succeed later)
    fatal     -> every other 4xx         (permanent; retrying cannot help)

The classification is deliberately explicit rather than clever. On a whiteboard
it is one table, and an interviewer can check the reasoning directly.
"""

from __future__ import annotations

#: 4xx that are genuinely transient. 408 Request Timeout and 429 Too Many
#: Requests are the only 4xx where the same request can later succeed.
RETRYABLE_4XX: frozenset[int] = frozenset({408, 429})

RETRYABLE = "retryable"
FATAL = "fatal"


def classify_http_status(status_code: int) -> str:
    """Return RETRYABLE or FATAL for an HTTP response status.

    `status_code` is the numeric status. Unknown or non-standard values are
    treated as fatal rather than optimistically retried: an unrecognised code
    is more likely a proxy or gateway artefact than a transient blip, and
    defaulting to "retry" would make an outage unbounded.
    """
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return FATAL

    if code in RETRYABLE_4XX:
        return RETRYABLE
    if 500 <= code <= 599:
        return RETRYABLE
    return FATAL


def is_retryable_response(status_code: int) -> bool:
    """Boolean convenience for call sites."""
    return classify_http_status(status_code) == RETRYABLE