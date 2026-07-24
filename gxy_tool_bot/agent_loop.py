"""Generic tool-use loop — the core agent harness."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

from gxy_tool_bot.api_client import ApiClient, ChatResponse

logger = logging.getLogger(__name__)

_TOOL_TIMEOUT_SECONDS = 120
_SUMMARIZE_BATCH_SIZE = 10
_SUMMARIZE_MIN_CHARS = 500
_SUMMARIZE_KEEP_RECENT = 5


def _run_tool_with_timeout(handler: Callable[[dict], str], args: dict, timeout: int = _TOOL_TIMEOUT_SECONDS) -> str:
    """Run a tool handler with a wall-clock timeout. Returns error string if timed out."""
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(handler, args)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.warning("Tool call timed out after %ds", timeout)
            return f"Error: tool call timed out after {timeout}s"


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


def _compute_context_size(messages: list[dict]) -> int:
    """Estimate total characters across all messages."""
    total = 0
    for msg in messages:
        total += len(msg.get("content") or "")
        for tc in msg.get("tool_calls", []):
            total += len(tc.get("function", {}).get("arguments", ""))
    return total


def _summarize_old_tool_results(
    client: ApiClient,
    messages: list[dict],
    summarized_ids: set[str],
    max_context_chars: int,
) -> int:
    """Summarize old tool results via LLM to reduce context size.

    Modifies messages in place. Returns number of tool results summarized.
    """
    keep_recent = _SUMMARIZE_KEEP_RECENT
    tool_indices = [
        i for i, msg in enumerate(messages)
        if msg.get("role") == "tool"
        and len(msg.get("content", "")) > _SUMMARIZE_MIN_CHARS
        and msg.get("tool_call_id") not in summarized_ids
    ]
    # Don't summarize the most recent tool results
    if len(tool_indices) <= keep_recent:
        return 0
    to_summarize = tool_indices[:-keep_recent]

    before_chars = _compute_context_size(messages)
    count = 0
    call_count = 0

    for i in range(0, len(to_summarize), _SUMMARIZE_BATCH_SIZE):
        batch = to_summarize[i:i + _SUMMARIZE_BATCH_SIZE]
        batch_contents = []
        for idx in batch:
            msg = messages[idx]
            batch_contents.append(f"--- Tool result (id={msg.get('tool_call_id')}) ---\n{msg['content']}")

        prompt_messages = [
            {"role": "system", "content": (
                "Summarize the following tool results into a compact form for an AI agent. "
                "Preserve all key facts: URLs, file paths, error messages, CLI flags, version numbers, and data structures. "
                "Remove prose, repetition, and formatting whitespace. "
                "Prioritize information density over readability — this will be consumed by an agent, not a human. "
                "Target ~20% of original length."
            )},
            {"role": "user", "content": "\n\n".join(batch_contents)},
        ]

        try:
            response = client.chat(messages=prompt_messages, temperature=0.1)
            summary = (response.content or "").strip()
            if not summary:
                raise RuntimeError("Empty summary")
            call_count += 1
            for idx in batch:
                msg = messages[idx]
                old_len = len(msg["content"])
                msg["content"] = f"[summarized] {summary}"
                summarized_ids.add(msg["tool_call_id"])
                count += 1
        except Exception as e:
            logger.warning("LLM summarization failed, falling back to truncation: %s", e)
            for idx in batch:
                msg = messages[idx]
                old_content = msg["content"]
                msg["content"] = old_content[:200] + f"[...truncated, {len(old_content)} chars total...]"
                summarized_ids.add(msg["tool_call_id"])
                count += 1

    after_chars = _compute_context_size(messages)
    if count > 0:
        logger.info(
            "LLM-summarized %d old tool results in %d calls to reduce context from %d to %d chars",
            count, call_count, before_chars, after_chars,
        )
    return count


def run_agent_loop(
    client: ApiClient,
    system_prompt: str,
    user_prompt: str,
    tools: list[ToolDefinition],
    max_iterations: int = 10,
    temperature: float = 0.4,
    messages: list[dict] | None = None,
    max_context_chars: int = 100_000,
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
    summarized_ids: set[str] = set()

    for iteration in range(1, max_iterations + 1):
        iterations = iteration
        total_chars = _compute_context_size(messages)
        logger.info("Agent iteration %d/%d (context: %d chars, ~%d tokens)", iteration, max_iterations, total_chars, total_chars // 4)

        if total_chars > max_context_chars:
            _summarize_old_tool_results(client, messages, summarized_ids, max_context_chars)

        if iteration == max_iterations - 3 and max_iterations >= 10:
            messages.append({
                "role": "user",
                "content": (
                    f"You have {max_iterations - iteration + 1} iterations remaining. "
                    "If you have not already started writing files, stop researching now "
                    "and use write_file to create all remaining tool files. "
                    "You do not need to investigate further — use what you already know."
                ),
            })

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
                        "arguments": json.dumps(tc.arguments),
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
                        result = _run_tool_with_timeout(tool_def.handler, tc.arguments)
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

                # If the agent called give_up, terminate the loop immediately
                if result.startswith("Gave up:"):
                    final_content = result
                    terminated_naturally = True
                    break
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
