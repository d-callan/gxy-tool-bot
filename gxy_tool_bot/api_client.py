"""Thin wrapper around the OpenAI-compatible chat completions endpoint."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Timeout defaults: 30s connect, 300s read (large completions can be slow on some endpoints).
_DEFAULT_READ_TIMEOUT = 300.0


def _make_timeout(read_timeout: float | None = None) -> httpx.Timeout:
    return httpx.Timeout(connect=30.0, read=read_timeout or _DEFAULT_READ_TIMEOUT, write=30.0, pool=30.0)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResponse:
    content: str | None
    tool_calls: list[ToolCall] | None
    finish_reason: str  # "stop", "tool_calls", "length"


class ApiClient:
    """Client for OpenAI-compatible chat completions API."""

    def __init__(self, base_url: str, api_key: str, model: str, read_timeout: float | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = httpx.Client(timeout=_make_timeout(read_timeout))

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.4,
    ) -> ChatResponse:
        """Send a chat completion request. Returns message + tool_calls."""
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": t} for t in tools
            ]

        logger.debug("Sending chat request: model=%s, messages=%d", self.model, len(messages))

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        resp.raise_for_status()

        try:
            data = resp.json()
        except json.JSONDecodeError:
            logger.error("API returned non-JSON response (status %d, %d bytes): %s",
                         resp.status_code, len(resp.content),
                         resp.text[:500])
            raise RuntimeError(f"API returned non-JSON response (status {resp.status_code}). First 500 chars: {resp.text[:500]}")

        choice = data["choices"][0]
        msg = choice["message"]
        finish_reason = choice["finish_reason"]

        content = msg.get("content")
        tool_calls = None
        if msg.get("tool_calls"):
            tool_calls = []
            for tc in msg["tool_calls"]:
                fn = tc["function"]
                try:
                    args = json.loads(fn["arguments"])
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_calls.append(ToolCall(id=tc["id"], name=fn["name"], arguments=args))

        return ChatResponse(content=content, tool_calls=tool_calls, finish_reason=finish_reason)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
