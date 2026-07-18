"""Generic tool-use loop — the core agent harness."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from gxy_tool_bot.api_client import ApiClient, ChatResponse

logger = logging.getLogger(__name__)


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict  # JSON schema for parameters
    handler: Callable[[dict], str]  # function that executes the tool call


@dataclass
class AgentResult:
    content: str  # final text output from the agent
    tool_call_trace: list[dict]  # log of all tool calls + results
    iterations: int  # how many loop iterations occurred
    terminated_naturally: bool  # True if the agent stopped on its own
    messages: list[dict] = None  # full conversation history for continuation


def run_agent_loop(
    client: ApiClient,
    system_prompt: str,
    user_prompt: str,
    tools: list[ToolDefinition],
    max_iterations: int = 10,
    temperature: float = 0.4,
    messages: list[dict] | None = None,
) -> AgentResult:
    """
    Run a tool-use loop:
    1. Send messages + tool definitions.
    2. If response has tool_calls, execute them and append results.
    3. Repeat until response has no tool_calls or max_iterations reached.
    4. Return final content + trace of all tool calls made.

    If `messages` is provided, continues from that conversation history
    (ignores system_prompt and user_prompt). Used for validation retry loops.
    """
    tool_map = {t.name: t for t in tools}
    tool_schemas = [
        {"name": t.name, "description": t.description, "parameters": t.parameters}
        for t in tools
    ]

    if messages is not None:
        messages = list(messages)  # shallow copy to avoid mutating caller's list
    else:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    trace: list[dict] = []
    iterations = 0
    final_content = ""
    terminated_naturally = False

    for iteration in range(1, max_iterations + 1):
        iterations = iteration
        logger.info("Agent iteration %d/%d", iteration, max_iterations)

        response: ChatResponse = client.chat(
            messages=messages,
            tools=tool_schemas if tool_schemas else None,
            temperature=temperature,
        )

        if response.tool_calls:
            # Append the assistant message with tool_calls to history
            assistant_msg: dict = {"role": "assistant", "content": response.content or ""}
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": __import__("json").dumps(tc.arguments),
                    },
                }
                for tc in response.tool_calls
            ]
            messages.append(assistant_msg)

            for tc in response.tool_calls:
                logger.debug("Tool call: %s(%s)", tc.name, tc.arguments)
                tool_def = tool_map.get(tc.name)
                if tool_def is None:
                    result = f"Error: unknown tool '{tc.name}'"
                    logger.warning(result)
                else:
                    try:
                        result = tool_def.handler(tc.arguments)
                    except Exception as e:
                        result = f"Error: {e}"
                        logger.warning("Tool %s raised: %s", tc.name, e)

                trace.append({
                    "tool": tc.name,
                    "arguments": tc.arguments,
                    "result": result[:2000],  # cap trace entry size
                })

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
        else:
            # No tool calls — this is the final answer
            final_content = response.content or ""
            terminated_naturally = True
            break

    if not terminated_naturally:
        final_content = (
            final_content
            + "\n\n⚠️ Agent did not naturally terminate after "
            + f"{max_iterations} iterations. Output may be incomplete."
        )
        logger.warning("Agent hit max iterations (%d)", max_iterations)

    return AgentResult(
        content=final_content,
        tool_call_trace=trace,
        iterations=iterations,
        terminated_naturally=terminated_naturally,
        messages=messages,
    )
