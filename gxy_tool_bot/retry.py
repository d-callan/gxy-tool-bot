"""HTTP retry helpers for transient failures."""

from __future__ import annotations

import datetime
import logging
import time
from email.utils import parsedate_to_datetime
from typing import Callable, TypeVar

import httpx

T = TypeVar("T")

logger = logging.getLogger(__name__)

# Retry-After is honored but capped so a misbehaving server can't stall a
# run for hours.
_MAX_RETRY_DELAY = 120.0


def retry_after_seconds(value: str) -> float | None:
    """Parse a Retry-After value: delta-seconds, or an HTTP-date (rarely
    used but legal). Returns seconds to wait, or None if unparseable."""
    try:
        return float(value)
    except ValueError:
        pass
    try:
        until = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    seconds = (until - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    return max(seconds, 0.0)


def retry_delay(resp: httpx.Response | None, attempt: int, base: float = 2.0) -> float:
    """Backoff for attempt N: ``base * (attempt + 1)``, raised to the
    server's Retry-After hint (capped) when one is present."""
    delay = base * (attempt + 1)
    retry_after = resp.headers.get("Retry-After") if resp is not None else None
    if retry_after:
        seconds = retry_after_seconds(retry_after)
        if seconds is not None:
            delay = max(delay, min(seconds, _MAX_RETRY_DELAY))
    return delay


def retry(
    fn: Callable[[], T],
    max_attempts: int = 2,
    backoff_base: float = 1.0,
) -> T:
    """
    Retry a callable on transient errors (429, 5xx, connection errors, timeouts).
    Uses exponential backoff: backoff_base * 2^attempt (1s, 2s, 4s by default),
    raised to the server's Retry-After hint (capped) when one is present.
    4xx errors other than 429 are raised immediately (not retried).
    """
    last_exc: Exception | None = None
    last_resp: httpx.Response | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except httpx.HTTPStatusError as e:
            if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                raise
            last_resp = e.response
            last_exc = e
            logger.warning(
                "HTTP %d on attempt %d/%d: %s",
                e.response.status_code, attempt + 1, max_attempts, e,
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
            last_exc = e
            logger.warning("Connection error on attempt %d/%d: %s", attempt + 1, max_attempts, e)

        if attempt < max_attempts - 1:
            delay = backoff_base * (2 ** attempt)
            retry_after = last_resp.headers.get("Retry-After") if last_resp is not None else None
            if retry_after:
                seconds = retry_after_seconds(retry_after)
                if seconds is not None:
                    delay = max(delay, min(seconds, _MAX_RETRY_DELAY))
            last_resp = None
            time.sleep(delay)

    raise last_exc  # type: ignore[misc]
