"""Tests for the agent loop."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from gxy_tool_bot.agent_loop import (
    AgentResult,
    ToolDefinition,
    _compute_context_size,
    run_agent_loop,
)
from gxy_tool_bot.api_client import ChatResponse, ToolCall


def _make_tool_call(id: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=id, name=name, arguments=args)


def test_agent_loop_terminates_on_no_tool_calls() -> None:
    """Agent should stop when the API returns a normal completion."""
    client = MagicMock()
    client.chat.return_value = ChatResponse(
        content="Here is the plan.",
        tool_calls=None,
        finish_reason="stop",
    )

    result = run_agent_loop(
        client=client,
        system_prompt="You are a planner.",
        user_prompt="Plan a tool.",
        tools=[],
        max_iterations=5,
    )

    assert result.terminated_naturally is True
    assert result.content == "Here is the plan."
    assert result.iterations == 1
    assert result.tool_call_trace == []


def test_agent_loop_executes_tool_then_terminates() -> None:
    """Agent should execute a tool call, then terminate on the next response."""
    client = MagicMock()
    # First call: returns a tool call
    # Second call: returns final content
    client.chat.side_effect = [
        ChatResponse(
            content=None,
            tool_calls=[_make_tool_call("call_1", "search_bioconda", {"query": "samtools"})],
            finish_reason="tool_calls",
        ),
        ChatResponse(
            content="Plan based on bioconda data.",
            tool_calls=None,
            finish_reason="stop",
        ),
    ]

    handler = MagicMock(return_value='{"package_name": "samtools", "version": "1.20"}')

    tools = [
        ToolDefinition(
            name="search_bioconda",
            description="Search bioconda",
            parameters={"type": "object", "properties": {}},
            handler=handler,
        ),
    ]

    result = run_agent_loop(
        client=client,
        system_prompt="sys",
        user_prompt="user",
        tools=tools,
        max_iterations=5,
    )

    assert result.terminated_naturally is True
    assert result.content == "Plan based on bioconda data."
    assert result.iterations == 2
    assert len(result.tool_call_trace) == 1
    assert result.tool_call_trace[0]["tool"] == "search_bioconda"
    handler.assert_called_once_with({"query": "samtools"})


def test_agent_loop_max_iterations_warning() -> None:
    """Agent should add a warning when max iterations is reached."""
    client = MagicMock()
    client.chat.return_value = ChatResponse(
        content=None,
        tool_calls=[_make_tool_call("call_1", "search_bioconda", {"query": "test"})],
        finish_reason="tool_calls",
    )

    handler = MagicMock(return_value="result")

    tools = [
        ToolDefinition(
            name="search_bioconda",
            description="Search bioconda",
            parameters={"type": "object", "properties": {}},
            handler=handler,
        ),
    ]

    result = run_agent_loop(
        client=client,
        system_prompt="sys",
        user_prompt="user",
        tools=tools,
        max_iterations=3,
    )

    assert result.terminated_naturally is False
    assert "did not naturally terminate" in result.content
    assert result.iterations == 3


def test_agent_loop_tool_error_handled() -> None:
    """Tool execution errors should be caught and returned to the agent."""
    client = MagicMock()
    client.chat.side_effect = [
        ChatResponse(
            content=None,
            tool_calls=[_make_tool_call("call_1", "failing_tool", {})],
            finish_reason="tool_calls",
        ),
        ChatResponse(
            content="Plan despite error.",
            tool_calls=None,
            finish_reason="stop",
        ),
    ]

    def _failing_handler(args: dict) -> str:
        raise RuntimeError("Something went wrong")

    tools = [
        ToolDefinition(
            name="failing_tool",
            description="A tool that fails",
            parameters={"type": "object", "properties": {}},
            handler=_failing_handler,
        ),
    ]

    result = run_agent_loop(
        client=client,
        system_prompt="sys",
        user_prompt="user",
        tools=tools,
        max_iterations=5,
    )

    assert result.terminated_naturally is True
    assert result.content == "Plan despite error."
    assert "Error" in result.tool_call_trace[0]["result"]


def test_compute_context_size() -> None:
    """Context size should sum content + tool_call arguments."""
    messages = [
        {"role": "system", "content": "hello"},
        {"role": "user", "content": "world"},
        {"role": "assistant", "content": "ok", "tool_calls": [
            {"function": {"arguments": '{"query": "test"}'}},
        ]},
        {"role": "tool", "content": "result data"},
    ]
    size = _compute_context_size(messages)
    assert size == len("hello") + len("world") + len("ok") + len('{"query": "test"}') + len("result data")


def test_context_size_logged(caplog) -> None:
    """Context size should be logged each iteration."""
    client = MagicMock()
    client.chat.return_value = ChatResponse(
        content="Done.",
        tool_calls=None,
        finish_reason="stop",
    )

    with caplog.at_level(logging.INFO, logger="gxy_tool_bot.agent_loop"):
        run_agent_loop(
            client=client,
            system_prompt="sys",
            user_prompt="user",
            tools=[],
            max_iterations=1,
        )

    assert any("context:" in record.message for record in caplog.records)


def test_context_summarization_triggers(caplog) -> None:
    """When context exceeds max_context_chars, old tool results should be LLM-summarized."""
    big_result = "x" * 2000
    summary_text = "summarized content"

    # Iteration 1: tool call with big result
    # Iteration 2: summarization call (not counted as iteration), then tool call with big result
    # Iteration 3: summarization call, then final answer
    # The client.chat mock needs to handle both agent calls and summarization calls

    agent_responses = [
        ChatResponse(content=None, tool_calls=[_make_tool_call("call_1", "fetch", {})], finish_reason="tool_calls"),
        ChatResponse(content=None, tool_calls=[_make_tool_call("call_2", "fetch", {})], finish_reason="tool_calls"),
        ChatResponse(content=None, tool_calls=[_make_tool_call("call_3", "fetch", {})], finish_reason="tool_calls"),
        ChatResponse(content=None, tool_calls=[_make_tool_call("call_4", "fetch", {})], finish_reason="tool_calls"),
        ChatResponse(content=None, tool_calls=[_make_tool_call("call_5", "fetch", {})], finish_reason="tool_calls"),
        ChatResponse(content=None, tool_calls=[_make_tool_call("call_6", "fetch", {})], finish_reason="tool_calls"),
        ChatResponse(content=None, tool_calls=[_make_tool_call("call_7", "fetch", {})], finish_reason="tool_calls"),
        ChatResponse(content="Done.", tool_calls=None, finish_reason="stop"),
    ]
    summary_response = ChatResponse(content=summary_text, tool_calls=None, finish_reason="stop")

    call_count = [0]

    client = MagicMock()

    def _chat_side_effect(messages, **kwargs):
        # Summarization calls have no tools kwarg or empty tools
        tools = kwargs.get("tools")
        if not tools:
            resp = summary_response
            return resp
        resp = agent_responses[call_count[0]]
        call_count[0] += 1
        return resp

    client.chat.side_effect = _chat_side_effect

    handler = MagicMock(return_value=big_result)
    tools = [ToolDefinition(name="fetch", description="fetch", parameters={"type": "object", "properties": {}}, handler=handler)]

    result = run_agent_loop(
        client=client,
        system_prompt="sys",
        user_prompt="user",
        tools=tools,
        max_iterations=10,
        max_context_chars=5000,
    )

    # Check that at least one tool result was summarized
    tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
    summarized = [m for m in tool_msgs if m.get("content", "").startswith("[summarized]")]
    assert len(summarized) > 0, "Expected at least one summarized tool result"

    # Recent tool results should NOT be summarized
    recent_tool_msgs = tool_msgs[-5:]
    for m in recent_tool_msgs:
        assert not m["content"].startswith("[summarized]"), "Recent tool result should not be summarized"


def test_context_summarization_fallback_truncation() -> None:
    """If LLM summarization fails, old tool results should be naively truncated."""
    big_result = "x" * 2000

    agent_responses = [
        ChatResponse(content=None, tool_calls=[_make_tool_call(f"call_{i}", "fetch", {})], finish_reason="tool_calls")
        for i in range(1, 8)
    ] + [ChatResponse(content="Done.", tool_calls=None, finish_reason="stop")]

    call_count = [0]

    client = MagicMock()

    def _chat_side_effect(messages, **kwargs):
        tools = kwargs.get("tools")
        if not tools:
            raise RuntimeError("LLM unavailable")
        resp = agent_responses[call_count[0]]
        call_count[0] += 1
        return resp

    client.chat.side_effect = _chat_side_effect

    handler = MagicMock(return_value=big_result)
    tools = [ToolDefinition(name="fetch", description="fetch", parameters={"type": "object", "properties": {}}, handler=handler)]

    result = run_agent_loop(
        client=client,
        system_prompt="sys",
        user_prompt="user",
        tools=tools,
        max_iterations=10,
        max_context_chars=5000,
    )

    tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
    truncated = [m for m in tool_msgs if "[...truncated," in m.get("content", "")]
    assert len(truncated) > 0, "Expected at least one truncated tool result"


def test_context_summarization_idempotent() -> None:
    """Already-summarized tool results should not be re-summarized."""
    big_result = "x" * 2000
    summary_text = "summarized"

    agent_responses = [
        ChatResponse(content=None, tool_calls=[_make_tool_call(f"call_{i}", "fetch", {})], finish_reason="tool_calls")
        for i in range(1, 8)
    ] + [ChatResponse(content="Done.", tool_calls=None, finish_reason="stop")]

    call_count = [0]
    summary_call_count = [0]

    client = MagicMock()

    def _chat_side_effect(messages, **kwargs):
        tools = kwargs.get("tools")
        if not tools:
            summary_call_count[0] += 1
            return ChatResponse(content=summary_text, tool_calls=None, finish_reason="stop")
        resp = agent_responses[call_count[0]]
        call_count[0] += 1
        return resp

    client.chat.side_effect = _chat_side_effect

    handler = MagicMock(return_value=big_result)
    tools = [ToolDefinition(name="fetch", description="fetch", parameters={"type": "object", "properties": {}}, handler=handler)]

    result = run_agent_loop(
        client=client,
        system_prompt="sys",
        user_prompt="user",
        tools=tools,
        max_iterations=10,
        max_context_chars=5000,
    )

    # Summarization should have happened
    assert summary_call_count[0] > 0, "Expected summarization calls"

    # On subsequent iterations, context may still exceed threshold,
    # but already-summarized results should not be re-summarized.
    # The summary_call_count should not grow unboundedly.
    # With 7 tool results and keep_recent=5, only 2 get summarized in 1 batch.
    # After that, context should be small enough to not trigger again.
    # If it does trigger again, the already-summarized ones are skipped.
    assert summary_call_count[0] <= 2, f"Expected at most 2 summarization calls, got {summary_call_count[0]}"
