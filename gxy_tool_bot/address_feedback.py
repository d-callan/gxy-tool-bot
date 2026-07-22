"""Address feedback on an existing PR: read comments + CI failures, fix tool files."""

from __future__ import annotations

import json
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
    _build_tool_definitions,
    _load_template,
)
from gxy_tool_bot.validation import ValidationResult, run_agent_with_validation
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
    ci_artifacts: dict[str, str]  # artifact name -> report content


def _summarize_test_json(raw: str) -> str:
    """Parse planemo test JSON and extract a compact summary of failures only.

    The JSON has structure: {"tests": [{"id": "...", "data": {"status": "...", ...}}], "summary": {...}}
    We extract only failed/error tests with their output_problems, execution_problem, and job info.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:5000]

    tests = data.get("tests", [])
    failures = []
    for test in tests:
        test_data = test.get("data", {})
        status = test_data.get("status", "")
        if status in ("success", "skip"):
            continue
        entry = {"id": test.get("id", ""), "status": status}
        if test_data.get("output_problems"):
            entry["output_problems"] = test_data["output_problems"]
        if test_data.get("execution_problem"):
            entry["execution_problem"] = test_data["execution_problem"]
        if test_data.get("problem_log"):
            entry["problem_log"] = test_data["problem_log"][:2000]
        job = test_data.get("job")
        if job:
            entry["job"] = {
                k: v for k, v in job.items()
                if k in ("command_line", "stdout", "stderr")
            }
        failures.append(entry)

    if not failures:
        summary = data.get("summary", {})
        n = summary.get("num_tests", 0)
        return f"All {n} tests passed." if n else "No test results found."

    lines = []
    for f in failures:
        lines.append(f"### {f['id']} — {f['status']}")
        if "output_problems" in f:
            lines.append("Output problems:")
            for p in f["output_problems"]:
                lines.append(f"  - {p}")
        if "execution_problem" in f:
            lines.append(f"Execution problem: {f['execution_problem']}")
        if "problem_log" in f:
            lines.append(f"Problem log (truncated):\n{f['problem_log']}")
        if "job" in f:
            job = f["job"]
            if job.get("command_line"):
                lines.append(f"Command: {job['command_line']}")
            if job.get("stderr"):
                lines.append(f"Stderr: {job['stderr'][:2000]}")
            if job.get("stdout"):
                lines.append(f"Stdout: {job['stdout'][:2000]}")
        lines.append("")
    return "\n".join(lines)


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

    # Fetch CI artifacts (lint reports, test outputs, etc.)
    #
    # This assumes the CI workflow uploads failure artifacts in the same style as
    # the IUC tools-iuc repo (e.g. 'Tool linting output', 'Python linting output',
    # 'R linting output', 'All tool test results', 'Tool test output N').
    # If the CI workflow behavior changes or a different repo uses different
    # artifact naming conventions, this may not pick up CI failure info.
    ci_artifacts: dict[str, str] = {}
    try:
        artifacts = gh.get_pr_artifacts(pr_number)

        # If the combined test results artifact exists, skip per-chunk artifacts
        # since the combined one already contains all test results.
        has_combined = any(a["name"] == "All tool test results" for a in artifacts)

        for artifact in artifacts:
            name = artifact["name"]
            # Only download artifacts that look like CI reports
            if not any(kw in name.lower() for kw in ("lint", "test", "python", "r lint", "file size")):
                continue
            # Skip per-chunk test artifacts if combined results are available
            if has_combined and name.startswith("Tool test output "):
                continue
            files = gh.download_artifact(artifact["id"])
            for fname, content in files.items():
                # Skip HTML reports — too verbose for LLM context
                if fname.endswith(".html"):
                    continue
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    text = content.decode("utf-8", errors="replace")
                # Parse planemo test JSON into a compact summary of failures
                if fname.endswith(".json") and "test" in name.lower():
                    text = _summarize_test_json(text)
                ci_artifacts[f"{name}/{fname}"] = text
    except Exception:
        logger.warning("Failed to fetch CI artifacts", exc_info=True)

    return FeedbackContext(
        pr_comments=pr_comments,
        review_comments=review_comments,
        failed_checks=failed_checks,
        existing_files=existing_files,
        tool_dir_name=tool_dir.name,
        ci_artifacts=ci_artifacts,
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
        f"When using `write_file`, paths must be relative to the tool directory "
        f"(e.g. `my_tool.xml`, not `tools/{ctx.tool_dir_name}/my_tool.xml`).\n"
    )
    parts.append("---\n")

    # Existing files — list names only, agent can read_file for contents
    parts.append("## Current Tool Files\n")
    parts.append("The following files exist in the tool directory. Use `read_file` to read any file you need to inspect before modifying it.\n")
    for path in sorted(ctx.existing_files.keys()):
        parts.append(f"- `{path}`")
    parts.append("")
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

    # CI artifact reports (lint output, test results, etc.)
    # Artifacts are already scoped to changed tools by the CI workflow,
    # so no additional filtering is needed.
    if ctx.ci_artifacts:
        parts.append("## CI Artifact Reports\n")
        for name, content in sorted(ctx.ci_artifacts.items()):
            parts.append(f"### {name}\n")
            if content:
                parts.append(f"```\n{content}\n```\n")
        parts.append("---\n")

    parts.append(
        f"Fix the issues identified above for the tool in `tools/{ctx.tool_dir_name}/`. "
        "Use `write_file` to rewrite any files that need changes — paths are relative to the tool directory. "
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

    # Track which files existed before the agent runs, so we can detect
    # if the agent only researched without modifying anything.
    # We check the tool call trace for write_file/compress_file/download_file
    # calls rather than comparing file sets, since feedback mode overwrites
    # existing files (same keys, new content).
    _WRITE_TOOLS = {"write_file", "compress_file", "download_file"}

    no_files_nudge = (
        "No files were modified in the previous attempt. The agent spent all iterations"
        " on research instead of fixing the issues.\n\n"
        "You MUST start fixing files immediately. Use `read_file` to inspect the files"
        " you need to modify, then use `write_file` to rewrite them. Do NOT call"
        " search_github, search_web, or fetch_url until you have fixed the identified issues.\n\n"
        "The existing files and feedback contain everything you need. Start fixing now."
    )

    with ApiClient(config.api.base_url, api_key, config.api.model, read_timeout=config.api.read_timeout) as client:
        result, files, validation = run_agent_with_validation(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            file_writer=file_writer,
            config=config,
            no_files_nudge=no_files_nudge,
            write_tools=_WRITE_TOOLS,
        )

    generated = GeneratedTool(
        files=files,
        summary=result.content if result.terminated_naturally else f"⚠️ Incomplete: {result.content}",
        tool_dir=file_writer.tool_dir,
        give_up_reason=file_writer.give_up_reason,
    )

    return generated, result, validation
