"""Tests for the agent loop."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from gxy_tool_bot.agent_loop import (
    AgentResult,
    ToolDefinition,
    _compute_context_size,
    _prune_previous_writes,
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
    big_result = "x" * 3000
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
    big_result = "x" * 3000

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
    big_result = "x" * 3000
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


def test_prune_previous_writes_replaces_old_content() -> None:
    """_prune_previous_writes should replace old write_file arguments with a placeholder."""
    import json
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "tool.xml", "content": "<tool>" + "x" * 5000 + "</tool>"}),
            }},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Wrote file tool.xml (5009 bytes)"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_2", "type": "function", "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "tool.xml", "content": "<tool>" + "y" * 3000 + "</tool>"}),
            }},
        ]},
        {"role": "tool", "tool_call_id": "call_2", "content": "Wrote file tool.xml (3009 bytes)"},
    ]

    _prune_previous_writes(messages, "tool.xml", "call_2")

    # First write_file call should be pruned
    old_args = json.loads(messages[0]["tool_calls"][0]["function"]["arguments"])
    assert "content" not in old_args
    assert old_args["path"] == "tool.xml"
    assert "_note" in old_args

    # First tool result should be replaced
    assert "overwritten" in messages[1]["content"]

    # Second write_file call should be untouched
    new_args = json.loads(messages[2]["tool_calls"][0]["function"]["arguments"])
    assert "content" in new_args
    assert "y" * 100 in new_args["content"]


def test_prune_previous_writes_different_paths_untouched() -> None:
    """_prune_previous_writes should not touch writes to different paths."""
    import json
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "other.xml", "content": "x" * 5000}),
            }},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Wrote file other.xml"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_2", "type": "function", "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "tool.xml", "content": "y" * 3000}),
            }},
        ]},
        {"role": "tool", "tool_call_id": "call_2", "content": "Wrote file tool.xml"},
    ]

    _prune_previous_writes(messages, "tool.xml", "call_2")

    # First write to other.xml should be untouched
    old_args = json.loads(messages[0]["tool_calls"][0]["function"]["arguments"])
    assert "content" in old_args
    assert old_args["content"] == "x" * 5000


def test_prune_previous_writes_compress_file() -> None:
    """_prune_previous_writes should also handle compress_file calls."""
    import json
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {
                "name": "compress_file",
                "arguments": json.dumps({"path": "test-data/sample.fa", "dest": "test-data/sample.fa.gz"}),
            }},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Compressed test-data/sample.fa"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_2", "type": "function", "function": {
                "name": "compress_file",
                "arguments": json.dumps({"path": "test-data/sample.fa", "dest": "test-data/sample.fa.gz"}),
            }},
        ]},
        {"role": "tool", "tool_call_id": "call_2", "content": "Compressed test-data/sample.fa"},
    ]

    _prune_previous_writes(messages, "test-data/sample.fa", "call_2")

    old_args = json.loads(messages[0]["tool_calls"][0]["function"]["arguments"])
    assert "dest" not in old_args
    assert old_args["path"] == "test-data/sample.fa"


def test_prune_fires_during_agent_loop() -> None:
    """Writing the same file twice in run_agent_loop should prune the first write from context."""
    import json
    big_content_v1 = "x" * 8000
    big_content_v2 = "y" * 6000

    client = MagicMock()
    client.chat.side_effect = [
        ChatResponse(
            content=None,
            tool_calls=[_make_tool_call("call_1", "write_file", {"path": "tool.xml", "content": big_content_v1})],
            finish_reason="tool_calls",
        ),
        ChatResponse(
            content=None,
            tool_calls=[_make_tool_call("call_2", "write_file", {"path": "tool.xml", "content": big_content_v2})],
            finish_reason="tool_calls",
        ),
        ChatResponse(content="Done.", tool_calls=None, finish_reason="stop"),
    ]

    handler = MagicMock(side_effect=["Wrote file tool.xml", "Wrote file tool.xml"])
    tools = [
        ToolDefinition(name="write_file", description="write", parameters={"type": "object", "properties": {}}, handler=handler),
    ]

    result = run_agent_loop(
        client=client,
        system_prompt="sys",
        user_prompt="user",
        tools=tools,
        max_iterations=5,
    )

    # Both calls should have executed with real content
    assert handler.call_count == 2
    assert handler.call_args_list[0].args[0]["content"] == big_content_v1
    assert handler.call_args_list[1].args[0]["content"] == big_content_v2

    # First write's args in message history should be pruned
    assistant_msgs = [m for m in result.messages if m.get("role") == "assistant" and m.get("tool_calls")]
    first_call_args = json.loads(assistant_msgs[0]["tool_calls"][0]["function"]["arguments"])
    assert "content" not in first_call_args
    assert first_call_args.get("_note") == "previous version overwritten"

    # First write's tool result should note it was overwritten
    tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
    assert "overwritten" in tool_msgs[0]["content"]

    # Second write's args should still have full content
    second_call_args = json.loads(assistant_msgs[1]["tool_calls"][0]["function"]["arguments"])
    assert second_call_args["content"] == big_content_v2


def test_summarization_hysteresis(monkeypatch) -> None:
    """Summarization should not re-trigger every iteration once context is above threshold.

    After summarization reduces context, subsequent iterations that add small amounts
    of content should NOT re-trigger summarization until context grows 25% beyond the
    post-summarization size.
    """
    summarize_calls = 0

    def fake_summarize(client, messages, summarized_ids, max_context_chars):
        nonlocal summarize_calls
        summarize_calls += 1
        # Simulate summarization: truncate all tool results to 50 chars
        for msg in messages:
            if msg.get("role") == "tool" and len(msg.get("content", "")) > 50:
                msg["content"] = msg["content"][:50] + "[summarized]"
                summarized_ids.add(msg.get("tool_call_id"))
        return 1  # pretend we summarized something

    monkeypatch.setattr("gxy_tool_bot.agent_loop._summarize_old_tool_results", fake_summarize)

    # Tool returns small content each time so context grows slowly after summarization
    handler = MagicMock(return_value="small result")

    tools = [
        ToolDefinition(
            name="search",
            description="search",
            parameters={"type": "object", "properties": {}},
            handler=handler,
        ),
    ]

    # Each iteration: tool call then eventually a final response
    responses = []
    for i in range(6):
        responses.append(ChatResponse(
            content=None,
            tool_calls=[_make_tool_call(f"call_{i}", "search", {"q": f"q{i}"})],
            finish_reason="tool_calls",
        ))
    responses.append(ChatResponse(content="done", tool_calls=None, finish_reason="stop"))

    client = MagicMock()
    client.chat.side_effect = responses

    # Set a large initial user prompt so context starts above threshold
    big_prompt = "x" * 600
    result = run_agent_loop(
        client=client,
        system_prompt="sys",
        user_prompt=big_prompt,
        tools=tools,
        max_iterations=7,
        max_context_chars=500,
    )

    # Summarization should have been called at most once or twice, not 6 times
    assert summarize_calls <= 2, f"Expected at most 2 summarization calls, got {summarize_calls}"


def test_consecutive_write_nudge() -> None:
    """After 3 consecutive identical tool calls with no other tool calls, a nudge should be injected."""
    agent_responses = [
        ChatResponse(content=None, tool_calls=[_make_tool_call(f"call_{i}", "write_file", {"path": "test.xml", "content": "same"})], finish_reason="tool_calls")
        for i in range(1, 5)
    ] + [ChatResponse(content="Done.", tool_calls=None, finish_reason="stop")]

    client = MagicMock()
    client.chat.side_effect = agent_responses

    handler = MagicMock(return_value="File written: test.xml")
    tools = [
        ToolDefinition(
            name="write_file",
            description="write a file",
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

    # A user nudge message should have been injected after the 3rd consecutive write
    user_msgs = [m for m in result.messages if m.get("role") == "user"]
    nudge_msgs = [m for m in user_msgs if "times in a row" in m.get("content", "")]
    assert len(nudge_msgs) == 1, f"Expected 1 nudge message, got {len(nudge_msgs)}"


def test_consecutive_write_reset_by_other_tool() -> None:
    """A different tool call between identical writes should reset the consecutive count."""
    agent_responses = [
        # write 1 (identical content)
        ChatResponse(content=None, tool_calls=[_make_tool_call("call_1", "write_file", {"path": "test.xml", "content": "same"})], finish_reason="tool_calls"),
        # write 2 (identical content — count=2)
        ChatResponse(content=None, tool_calls=[_make_tool_call("call_2", "write_file", {"path": "test.xml", "content": "same"})], finish_reason="tool_calls"),
        # lint (resets counter)
        ChatResponse(content=None, tool_calls=[_make_tool_call("call_3", "planemo_lint", {"path": "."})], finish_reason="tool_calls"),
        # write 3 (should NOT trigger nudge — counter reset)
        ChatResponse(content=None, tool_calls=[_make_tool_call("call_4", "write_file", {"path": "test.xml", "content": "same"})], finish_reason="tool_calls"),
        # write 4 (now 2 consecutive — still no nudge)
        ChatResponse(content=None, tool_calls=[_make_tool_call("call_5", "write_file", {"path": "test.xml", "content": "same"})], finish_reason="tool_calls"),
        ChatResponse(content="Done.", tool_calls=None, finish_reason="stop"),
    ]

    client = MagicMock()
    client.chat.side_effect = agent_responses

    write_handler = MagicMock(return_value="File written: test.xml")
    lint_handler = MagicMock(return_value="Lint OK")
    tools = [
        ToolDefinition(name="write_file", description="write", parameters={"type": "object", "properties": {}}, handler=write_handler),
        ToolDefinition(name="planemo_lint", description="lint", parameters={"type": "object", "properties": {}}, handler=lint_handler),
    ]

    result = run_agent_loop(
        client=client,
        system_prompt="sys",
        user_prompt="user",
        tools=tools,
        max_iterations=6,
    )

    user_msgs = [m for m in result.messages if m.get("role") == "user"]
    nudge_msgs = [m for m in user_msgs if "times in a row" in m.get("content", "")]
    assert len(nudge_msgs) == 0, f"Expected no nudge messages, got {len(nudge_msgs)}"


def test_repeated_fetch_url_nudge() -> None:
    """3 consecutive identical fetch_url calls (e.g. hitting a 404) should trigger a nudge."""
    agent_responses = [
        ChatResponse(content=None, tool_calls=[_make_tool_call(f"call_{i}", "fetch_url", {"url": "https://example.com/missing.py"})], finish_reason="tool_calls")
        for i in range(1, 5)
    ] + [ChatResponse(content="Done.", tool_calls=None, finish_reason="stop")]

    client = MagicMock()
    client.chat.side_effect = agent_responses

    fetch_handler = MagicMock(return_value="Error: 404 Not Found")
    tools = [
        ToolDefinition(name="fetch_url", description="fetch", parameters={"type": "object", "properties": {}}, handler=fetch_handler),
    ]

    result = run_agent_loop(
        client=client,
        system_prompt="sys",
        user_prompt="user",
        tools=tools,
        max_iterations=5,
    )

    user_msgs = [m for m in result.messages if m.get("role") == "user"]
    nudge_msgs = [m for m in user_msgs if "times in a row" in m.get("content", "")]
    assert len(nudge_msgs) == 1, f"Expected 1 nudge message, got {len(nudge_msgs)}"
