"""Tests for the API client response parsing and error handling."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from gxy_tool_bot.api_client import ApiClient, ChatResponse, ToolCall


def _make_client() -> ApiClient:
    return ApiClient("https://api.example.com", "test-key", "test-model", read_timeout=30.0)


def _mock_response(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(data).encode(),
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "https://api.example.com/chat/completions"),
    )


def test_chat_parses_content_and_finish_reason() -> None:
    client = _make_client()
    resp = _mock_response({
        "choices": [{
            "message": {"content": "Hello!"},
            "finish_reason": "stop",
        }],
    })
    with patch.object(client._client, "post", return_value=resp):
        result = client.chat([{"role": "user", "content": "hi"}])
    assert result.content == "Hello!"
    assert result.tool_calls is None
    assert result.finish_reason == "stop"
    client.close()


def test_chat_parses_tool_calls() -> None:
    client = _make_client()
    resp = _mock_response({
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "tool.xml", "content": "<tool/>"}),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
    })
    with patch.object(client._client, "post", return_value=resp):
        result = client.chat([{"role": "user", "content": "write"}])
    assert result.tool_calls is not None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "write_file"
    assert result.tool_calls[0].arguments["path"] == "tool.xml"
    assert result.finish_reason == "tool_calls"
    client.close()


def test_chat_handles_malformed_tool_call_args() -> None:
    """If tool call arguments aren't valid JSON, default to empty dict."""
    client = _make_client()
    resp = _mock_response({
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "write_file",
                        "arguments": "not valid json{",
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
    })
    with patch.object(client._client, "post", return_value=resp):
        result = client.chat([{"role": "user", "content": "write"}])
    assert result.tool_calls is not None
    assert result.tool_calls[0].arguments == {}
    client.close()


def test_chat_retries_on_missing_choices() -> None:
    """Should retry when API returns valid JSON without choices field."""
    client = _make_client()
    bad_resp = _mock_response({"error": "upstream timeout"})
    good_resp = _mock_response({
        "choices": [{
            "message": {"content": "recovered"},
            "finish_reason": "stop",
        }],
    })
    with patch.object(client._client, "post", side_effect=[bad_resp, good_resp]):
        with patch("time.sleep"):
            result = client.chat([{"role": "user", "content": "hi"}])
    assert result.content == "recovered"
    client.close()


def test_chat_raises_after_retries_missing_choices() -> None:
    """Should raise RuntimeError after exhausting retries on missing choices."""
    client = _make_client()
    bad_resp = _mock_response({"error": "upstream timeout"})
    with patch.object(client._client, "post", side_effect=[bad_resp, bad_resp]):
        with patch("time.sleep"):
            with pytest.raises(RuntimeError, match="no choices"):
                client.chat([{"role": "user", "content": "hi"}])
    client.close()


def test_chat_retries_on_non_json_response() -> None:
    """Should retry when API returns non-JSON response."""
    client = _make_client()
    bad_resp = httpx.Response(
        200,
        content=b"   \n  ",
        request=httpx.Request("POST", "https://api.example.com/chat/completions"),
    )
    good_resp = _mock_response({
        "choices": [{
            "message": {"content": "recovered"},
            "finish_reason": "stop",
        }],
    })
    with patch.object(client._client, "post", side_effect=[bad_resp, good_resp]):
        with patch("time.sleep"):
            result = client.chat([{"role": "user", "content": "hi"}])
    assert result.content == "recovered"
    client.close()


def test_chat_raises_after_retries_non_json() -> None:
    """Should raise RuntimeError after exhausting retries on non-JSON."""
    client = _make_client()
    bad_resp = httpx.Response(
        200,
        content=b"   ",
        request=httpx.Request("POST", "https://api.example.com/chat/completions"),
    )
    with patch.object(client._client, "post", side_effect=[bad_resp, bad_resp]):
        with patch("time.sleep"):
            with pytest.raises(RuntimeError, match="non-JSON"):
                client.chat([{"role": "user", "content": "hi"}])
    client.close()


def test_chat_raises_on_http_error() -> None:
    """Should raise on HTTP status errors."""
    client = _make_client()
    error_resp = httpx.Response(
        401,
        content=b'{"error": "unauthorized"}',
        request=httpx.Request("POST", "https://api.example.com/chat/completions"),
    )
    with patch.object(client._client, "post", return_value=error_resp):
        with pytest.raises(httpx.HTTPStatusError):
            client.chat([{"role": "user", "content": "hi"}])
    client.close()
