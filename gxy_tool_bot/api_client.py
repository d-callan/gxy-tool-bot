"""Thin wrapper around the OpenAI-compatible chat completions endpoint."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Timeout defaults: 30s connect, 600s read (large completions can be slow on some endpoints).
_DEFAULT_READ_TIMEOUT = 600.0


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

    def __init__(self, base_url: str, api_key: str, model: str, read_timeout: float | None = None,
                 fallback_models: list[str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.fallback_models = fallback_models or []
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

        # Build model retry sequence: primary model first, then fallbacks.
        # If no fallbacks, duplicate primary so it gets at least 2 attempts (preserving prior behavior).
        models_to_try = [self.model] + self.fallback_models
        if not self.fallback_models:
            models_to_try.append(self.model)
        max_retries = len(models_to_try)

        data = None
        for attempt, model_used in enumerate(models_to_try):
            payload["model"] = model_used
            logger.debug("Sending chat request: model=%s, messages=%d", model_used, len(messages))
            try:
                resp = self._client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                resp.raise_for_status()
            except (httpx.ReadTimeout, httpx.ConnectError) as e:
                logger.warning("API error with model %s (attempt %d/%d): %s", model_used, attempt + 1, max_retries, e)
                if attempt < max_retries - 1:
                    time.sleep(5)
                    continue
                raise
            except httpx.HTTPStatusError as e:
                if resp.status_code in (502, 503, 504):
                    logger.warning("API server error %d with model %s (attempt %d/%d): %s",
                                   resp.status_code, model_used, attempt + 1, max_retries, e)
                    if attempt < max_retries - 1:
                        time.sleep(5)
                        continue
                    raise
                raise

            try:
                data = resp.json()
                # Validate that the response has the expected structure
                if "choices" not in data or not data["choices"]:
                    logger.warning("API returned JSON without choices (attempt %d/%d, status %d): %s",
                                   attempt + 1, max_retries, resp.status_code, str(data)[:200])
                    if attempt < max_retries - 1:
                        time.sleep(5)
                        continue
                    logger.error("API returned invalid response after %d attempts: %s", max_retries, str(data)[:500])
                    raise RuntimeError(f"API returned invalid response (no choices field). Response: {str(data)[:500]}")
                break
            except json.JSONDecodeError:
                logger.warning("API returned non-JSON response (attempt %d/%d, status %d, %d bytes)",
                               attempt + 1, max_retries, resp.status_code, len(resp.content))
                if attempt < max_retries - 1:
                    time.sleep(5)
                    continue
                logger.error("API returned non-JSON response after %d attempts: %s", max_retries, resp.text[:500])
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
