"""Tests for the retry helper."""

from __future__ import annotations

import httpx
import pytest

from gxy_tool_bot.retry import retry


def test_retry_succeeds_first_attempt() -> None:
    """No retry needed when the callable succeeds."""
    calls = []
    def _fn() -> str:
        calls.append(1)
        return "ok"

    result = retry(_fn, max_attempts=3, backoff_base=0)
    assert result == "ok"
    assert len(calls) == 1


def test_retry_4xx_raises_immediately() -> None:
    """4xx errors should not be retried."""
    calls = []
    def _fn() -> None:
        calls.append(1)
        resp = httpx.Response(404, request=httpx.Request("GET", "https://example.com"))
        raise httpx.HTTPStatusError("not found", request=resp.request, response=resp)

    with pytest.raises(httpx.HTTPStatusError):
        retry(_fn, max_attempts=3, backoff_base=0)
    assert len(calls) == 1


def test_retry_5xx_retries_then_succeeds() -> None:
    """5xx errors should be retried."""
    calls = []
    def _fn() -> str:
        calls.append(1)
        if len(calls) < 2:
            resp = httpx.Response(503, request=httpx.Request("GET", "https://example.com"))
            raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)
        return "ok"

    result = retry(_fn, max_attempts=3, backoff_base=0)
    assert result == "ok"
    assert len(calls) == 2


def test_retry_5xx_exhausts_attempts() -> None:
    """5xx errors should exhaust all attempts then raise."""
    calls = []
    def _fn() -> None:
        calls.append(1)
        resp = httpx.Response(500, request=httpx.Request("GET", "https://example.com"))
        raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)

    with pytest.raises(httpx.HTTPStatusError):
        retry(_fn, max_attempts=3, backoff_base=0)
    assert len(calls) == 3


def test_retry_connection_error_retries() -> None:
    """Connection errors should be retried."""
    calls = []
    def _fn() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ConnectError("connection refused")
        return "ok"

    result = retry(_fn, max_attempts=3, backoff_base=0)
    assert result == "ok"
    assert len(calls) == 2


def test_retry_read_timeout_retries() -> None:
    """Read timeouts should be retried."""
    calls = []
    def _fn() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ReadTimeout("timed out")
        return "ok"

    result = retry(_fn, max_attempts=3, backoff_base=0)
    assert result == "ok"
    assert len(calls) == 2
