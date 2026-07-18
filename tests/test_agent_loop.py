"""Tests for the agent loop."""

from __future__ import annotations

from unittest.mock import MagicMock

from gxy_tool_bot.agent_loop import AgentResult, ToolDefinition, run_agent_loop
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
