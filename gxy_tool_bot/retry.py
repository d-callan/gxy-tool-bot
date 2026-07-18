"""HTTP retry helper for transient failures."""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


def retry(
    fn: Callable[[], T],
    max_attempts: int = 2,
    backoff_base: float = 1.0,
) -> T:
    """
    Retry a callable on transient errors (5xx, connection errors, timeouts).
    Uses exponential backoff: backoff_base * 2^attempt (1s, 2s, 4s by default).
    4xx errors are raised immediately (not retried).
    """
    import httpx

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except httpx.HTTPStatusError as e:
            if 400 <= e.response.status_code < 500:
                raise
            last_exc = e
            logger.warning(
                "HTTP %d on attempt %d/%d: %s",
                e.response.status_code, attempt + 1, max_attempts, e,
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
            last_exc = e
            logger.warning("Connection error on attempt %d/%d: %s", attempt + 1, max_attempts, e)

        if attempt < max_attempts - 1:
            wait = backoff_base * (2 ** attempt)
            time.sleep(wait)

    raise last_exc  # type: ignore[misc]
