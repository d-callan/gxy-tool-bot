"""Address feedback on an existing PR: read comments + CI failures, fix tool files."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from gxy_tool_bot.agent_loop import AgentResult
from gxy_tool_bot.api_client import ApiClient
from gxy_tool_bot.config import BotConfig
from gxy_tool_bot.generator import (
    GeneratedFile,
    GeneratedTool,
    FileWriter,
    ValidationResult,
    _build_tool_definitions,
    _load_template,
    _run_agent_with_validation,
)
from gxy_tool_bot.github_client import Comment, GitHubClient

logger = logging.getLogger(__name__)


@dataclass
class FeedbackContext:
    """Collected feedback from PR comments and CI checks."""
    pr_comments: list[Comment]
    review_comments: list[Comment]
    failed_checks: list[dict]
    existing_files: dict[str, str]  # relative path -> file content
    tool_dir_name: str  # the tool directory name (e.g. "sdust")


def _collect_feedback(gh: GitHubClient, pr_number: int, tool_dir: Path) -> FeedbackContext:
    """Gather all feedback: PR comments, review comments, CI failures, and existing files."""
    pr_comments = gh.get_pr_comments(pr_number)
    review_comments = gh.get_pr_review_comments(pr_number)

    all_checks = gh.get_pr_check_runs(pr_number)
    failed_checks = [c for c in all_checks if c.get("conclusion") not in ("success", None, "")]

    # Read existing files from the tool directory
    existing_files: dict[str, str] = {}
    if tool_dir.exists():
        for f in tool_dir.rglob("*"):
            if f.is_file():
                rel = f.relative_to(tool_dir)
                # Skip internal files
                if rel.name == ".tool-name":
                    continue
                try:
                    existing_files[str(rel)] = f.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    existing_files[str(rel)] = f.read_bytes().decode("utf-8", errors="replace")

    return FeedbackContext(
        pr_comments=pr_comments,
        review_comments=review_comments,
        failed_checks=failed_checks,
        existing_files=existing_files,
        tool_dir_name=tool_dir.name,
    )


def _filter_ci_output(output: str, tool_dir_name: str) -> str:
    """Extract only the failure-relevant lines from CI output.

    Planemo lint output includes info about all tools in the repo.
    We filter to blocks that mention the tool, then within those
    blocks extract warning/error/failure lines with context.
    """
    if not output:
        return ""
    lines = output.splitlines()
    relevant: list[str] = []

    # Find blocks: a "Linting tool" line starts a block for that tool.
    # We collect lines until the next "Linting tool" or "Linting repository" line.
    in_tool_block = False
    block_lines: list[str] = []
    for line in lines:
        if "Linting tool" in line:
            # Flush previous block if it was for our tool
            if in_tool_block and block_lines:
                relevant.extend(block_lines)
                relevant.append("")
            in_tool_block = tool_dir_name in line
            block_lines = []
        elif "Linting repository" in line:
            # Flush previous block
            if in_tool_block and block_lines:
                relevant.extend(block_lines)
                relevant.append("")
            in_tool_block = False
            block_lines = []
        elif in_tool_block:
            # Only keep failure-related lines within our tool's block
            is_failure = any(kw in line.upper() for kw in ("WARNING", "ERROR", "FAIL", "Failed"))
            if is_failure:
                block_lines.append(line)

    # Flush last block
    if in_tool_block and block_lines:
        relevant.extend(block_lines)
        relevant.append("")

    return "\n".join(relevant) if relevant else output[:5000]


def _build_feedback_user_prompt(ctx: FeedbackContext) -> str:
    """Build the user prompt containing existing files + feedback."""
    parts: list[str] = []

    parts.append(
        f"You are addressing feedback on the tool in `tools/{ctx.tool_dir_name}/`. "
        f"Only modify files in this directory. Do NOT touch any other tool's files.\n"
    )
    parts.append("---\n")

    # Existing files
    parts.append("## Current Tool Files\n")
    for path, content in sorted(ctx.existing_files.items()):
        parts.append(f"### {path}\n```\n{content}\n```\n")
    parts.append("---\n")

    # PR comments (general) — filter out bot's own comments
    human_comments = [
        c for c in ctx.pr_comments
        if "github-actions" not in c.author.lower() and "gxy-tool-bot" not in c.author.lower()
    ]

    if human_comments:
        parts.append("## Maintainer Comments\n")
        for c in human_comments:
            parts.append(f"**{c.author}:**\n{c.body}\n")
        parts.append("---\n")

    # Review comments (inline) — include file path and line number
    if ctx.review_comments:
        parts.append("## Review Comments (inline code)\n")
        for c in ctx.review_comments:
            location = ""
            if c.file_path:
                location = f" on `{c.file_path}`"
                if c.line:
                    location += f" at line {c.line}"
            parts.append(f"**{c.author}:**{location}\n{c.body}\n")
        parts.append("---\n")

    # CI failures — filter to relevant lines only
    if ctx.failed_checks:
        parts.append("## CI Check Failures\n")
        for check in ctx.failed_checks:
            parts.append(f"### {check['name']} — {check['conclusion']}\n")
            filtered = _filter_ci_output(check.get("output", ""), ctx.tool_dir_name)
            if filtered:
                parts.append(f"```\n{filtered}\n```\n")
        parts.append("---\n")

    parts.append(
        f"Fix the issues identified above for the tool in `tools/{ctx.tool_dir_name}/`. "
        "Use `write_file` to rewrite any files that need changes. "
        "Only rewrite files that need fixing. Do NOT modify files for any other tool."
    )

    return "\n".join(parts)


def address_feedback(
    pr_number: int,
    config: BotConfig,
    api_key: str,
    tool_dir: Path,
    gh: GitHubClient,
) -> tuple[GeneratedTool, AgentResult, ValidationResult]:
    """
    Address feedback on an existing PR:
    1. Collect PR comments, review comments, CI failures, and existing files.
    2. Run agent loop with feedback context to fix issues (with validation retries).
    3. Validate the updated files.
    """
    ctx = _collect_feedback(gh, pr_number, tool_dir)

    if not ctx.existing_files:
        raise ValueError(f"No tool files found in {tool_dir}")

    system_prompt = _load_template("feedback_system.txt").render()
    user_prompt = _build_feedback_user_prompt(ctx)

    # Load existing files into FileWriter so they're tracked
    file_writer = FileWriter(tool_dir)
    for path, content in ctx.existing_files.items():
        file_writer.files[path] = content.encode("utf-8")
        # Also write to disk so the agent can see them
        dest = tool_dir / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    tools = _build_tool_definitions(file_writer)

    # No no-files nudge for feedback — files already exist, the agent
    # should be fixing them, not generating from scratch.
    with ApiClient(config.api.base_url, api_key, config.api.model, read_timeout=config.api.read_timeout) as client:
        result, files, validation = _run_agent_with_validation(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            file_writer=file_writer,
            config=config,
        )

    generated = GeneratedTool(
        files=files,
        summary=result.content if result.terminated_naturally else f"⚠️ Incomplete: {result.content}",
        tool_dir=file_writer.tool_dir,
        give_up_reason=file_writer.give_up_reason,
    )

    return generated, result, validation
