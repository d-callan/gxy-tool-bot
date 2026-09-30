"""Tool review module — standalone PR review + integrated self-review.

The review agent reads tool files, compares them against IUC conventions and
exemplars, runs planemo checks, and produces structured findings (category +
severity). It has read-only tools only — it never writes files.

Two modes:
1. **Standalone**: triggered by a ``review`` label on a PR. Collects context
   (files, CI failures, exemplars), runs review, posts findings as a PR comment.
2. **Integrated**: runs automatically after generate/feedback flows. The review
   findings are fed back to the original agent as a user message for a fix round,
   continuing from the original conversation history (same pattern as validation
   retries). Up to ``max_review_fix_rounds`` rounds of review → fix.

Context management:
- Review agent: always fresh context (no inherited conversation).
- Post-review fix agent: continues from original conversation history.
- Re-review (round 2+): fresh context each time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from gxy_tool_bot.agent_loop import AgentResult, ToolDefinition, run_agent_loop
from gxy_tool_bot.api_client import ApiClient
from gxy_tool_bot.config import BotConfig
from gxy_tool_bot.exemplars import fetch_exemplars
from gxy_tool_bot.generator import (
    FileWriter,
    GeneratedFile,
    _build_exemplar_text,
    _load_template,
)
from gxy_tool_bot.github_client import GitHubClient
from gxy_tool_bot.lookups.biotools import search_bio_tools
from gxy_tool_bot.lookups.github import search_github
from gxy_tool_bot.lookups.toolshed import fetch_toolshed_categories
from gxy_tool_bot.lookups.web import search_web
from gxy_tool_bot.utils import is_report_artifact, read_tool_files
from gxy_tool_bot.validation import ValidationResult, run_agent_with_validation

logger = logging.getLogger(__name__)

_SEVERITY_EMOJI = {"critical": "🔴", "warning": "🟡", "suggestion": "🔵"}
_VALID_SEVERITIES = {"critical", "warning", "suggestion"}
_VALID_CATEGORIES = {
    "validation", "conventions", "completeness",
    "test_coverage", "security_bugs", "brittleness",
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ReviewFinding:
    """A single finding from the review agent."""
    category: str
    severity: str
    file: str | None
    line: int | None
    description: str
    suggestion: str | None


@dataclass
class ReviewResult:
    """Aggregated results from a review run."""
    findings: list[ReviewFinding]
    raw_output: str
    agent_iterations: int
    terminated_naturally: bool
    has_critical: bool = False
    has_warnings: bool = False

    def __post_init__(self) -> None:
        self.has_critical = any(f.severity == "critical" for f in self.findings)
        self.has_warnings = any(f.severity == "warning" for f in self.findings)


@dataclass
class ReviewContext:
    """Collected context for a review run."""
    existing_files: dict[str, str]  # relative path -> content
    tool_dir_name: str
    ci_failures: list[dict]  # check runs with failures
    ci_artifacts: dict[str, str]  # artifact name -> content
    exemplars_text: str  # formatted exemplar XMLs for the prompt
    plan_markdown: str | None = None  # the spec/plan to check completeness against


# ---------------------------------------------------------------------------
# Context collection
# ---------------------------------------------------------------------------

def collect_review_context(
    tool_dir: Path,
    config: BotConfig,
    gh: GitHubClient | None = None,
    pr_number: int | None = None,
    plan_markdown: str | None = None,
) -> ReviewContext:
    """Gather files, CI failures, exemplars, and plan for a review run.

    If ``gh`` and ``pr_number`` are provided, also fetches CI check runs and
    artifacts (same as the feedback flow). If ``plan_markdown`` is not provided
    but ``gh`` and ``pr_number`` are, attempts to fetch the plan from the issue
    linked to the PR (via "Closes #N" in the PR body). Always fetches exemplars
    for comparison against IUC patterns.
    """
    existing_files = read_tool_files(tool_dir)

    ci_failures: list[dict] = []
    ci_artifacts: dict[str, str] = {}

    if gh and pr_number is not None:
        from gxy_tool_bot.planemo_utils import summarize_test_json

        all_checks = gh.get_pr_check_runs(pr_number)
        ci_failures = [c for c in all_checks if c.get("conclusion") not in ("success", None, "")]

        try:
            artifacts = gh.get_pr_artifacts(pr_number)
            has_combined = any(a["name"] == "All tool test results" for a in artifacts)

            for artifact in artifacts:
                name = artifact["name"]
                if not is_report_artifact(name):
                    continue
                if has_combined and name.startswith("Tool test output "):
                    continue
                files = gh.download_artifact(artifact["id"])
                for fname, content in files.items():
                    if fname.endswith(".html"):
                        continue
                    try:
                        text = content.decode("utf-8")
                    except UnicodeDecodeError:
                        text = content.decode("utf-8", errors="replace")
                    if fname.endswith(".json") and "test" in name.lower():
                        text = summarize_test_json(text)
                    ci_artifacts[f"{name}/{fname}"] = text
        except Exception:
            logger.warning("Failed to fetch CI artifacts for review", exc_info=True)

        # If no plan was passed directly, try to fetch it from the linked issue.
        if plan_markdown is None:
            plan_markdown = _fetch_plan_from_pr(gh, pr_number)

    # Fetch exemplars for comparison
    exemplars = fetch_exemplars(config.exemplars)
    exemplars_text = _build_exemplar_text(exemplars)

    return ReviewContext(
        existing_files=existing_files,
        tool_dir_name=tool_dir.name,
        ci_failures=ci_failures,
        ci_artifacts=ci_artifacts,
        exemplars_text=exemplars_text,
        plan_markdown=plan_markdown,
    )


def _fetch_plan_from_pr(gh: GitHubClient, pr_number: int) -> str | None:
    """Fetch the plan from the issue linked to a PR.

    PRs created by the bot contain "Closes #N" in the body, referencing the
    issue where the plan was posted as a comment. This extracts the issue
    number and fetches the plan comment.
    """
    import re

    from gxy_tool_bot.planner import find_plan_comment

    try:
        pr = gh.get_pr(pr_number)
        body = pr.get("body", "") or ""
        # Look for "Closes #N", "Fixes #N", "Resolves #N" in the PR body
        match = re.search(r"(?:closes|fixes|resolves)\s+#(\d+)", body, re.IGNORECASE)
        if not match:
            return None
        issue_number = int(match.group(1))
        comments = gh.get_issue_comments(issue_number)
        return find_plan_comment(comments)
    except Exception:
        logger.warning("Failed to fetch plan from linked issue for PR #%d", pr_number, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_review_prompt(ctx: ReviewContext) -> tuple[str, str]:
    """Build system + user prompts for the review agent.

    Returns (system_prompt, user_prompt).
    """
    system_prompt = _load_template("review_system.txt").render()

    parts: list[str] = []
    parts.append(
        f"You are reviewing the tool in `tools/{ctx.tool_dir_name}/`. "
        f"Only review files in this directory.\n"
    )
    parts.append("---\n")

    # Plan/spec — check completeness against this
    if ctx.plan_markdown:
        parts.append("## Tool Plan / Spec\n")
        parts.append(
            "This is the plan the tool was generated from. Check that the tool files "
            "implement everything described here — all inputs, outputs, parameters, "
            "and test cases. Report missing or incomplete implementations as "
            "`completeness` findings.\n\n"
        )
        parts.append(ctx.plan_markdown)
        parts.append("\n---\n")

    # Tool files — list names only, agent uses read_file to inspect
    parts.append("## Tool Files\n")
    parts.append(
        "The following files exist in the tool directory. Use `read_file` to read "
        "any file you need to inspect. The `.agent-notes` file (if present) contains "
        "the writer's rationale for non-obvious decisions — read it for context.\n"
    )
    for path in sorted(ctx.existing_files.keys()):
        parts.append(f"- `{path}`")
    parts.append("")
    parts.append("---\n")

    # Exemplars
    if ctx.exemplars_text:
        parts.append("## Exemplar Galaxy Tool XMLs\n")
        parts.append("These are examples of well-written IUC tools. Compare the tool files against these patterns.\n")
        parts.append(ctx.exemplars_text)
        parts.append("\n---\n")

    # CI failures
    if ctx.ci_failures:
        parts.append("## CI Check Failures\n")
        for check in ctx.ci_failures:
            parts.append(f"### {check['name']} — {check['conclusion']}\n")
            output = check.get("output", "")
            if output:
                # Filter to relevant lines (reuse the feedback filter)
                from gxy_tool_bot.address_feedback import _filter_ci_output
                filtered = _filter_ci_output(output, ctx.tool_dir_name)
                if filtered:
                    parts.append(f"```\n{filtered}\n```\n")
        parts.append("---\n")

    # CI artifacts
    if ctx.ci_artifacts:
        parts.append("## CI Artifact Reports\n")
        for name, content in sorted(ctx.ci_artifacts.items()):
            parts.append(f"### {name}\n")
            if content:
                parts.append(f"```\n{content}\n```\n")
        parts.append("---\n")

    parts.append(
        "Review the tool files listed above. Use `read_file` to inspect each file, "
        "run `planemo_lint` and `planemo_test` if available, compare against the exemplars, "
        "and report your findings in the structured format specified in your instructions."
    )

    return system_prompt, "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool definitions (read-only)
# ---------------------------------------------------------------------------

def _build_review_tool_definitions(file_writer: FileWriter) -> list[ToolDefinition]:
    """Build read-only tool definitions for the review agent.

    The review agent can read files, run planemo, and search — but never write.
    """
    import shutil

    tools = [
        ToolDefinition(
            name="read_file",
            description=(
                "Read the contents of a file in the tool directory. "
                "Use this to inspect files for review. "
                "Supports optional line range (start_line, end_line) and pattern search (regex). "
                "If pattern is given, returns only matching lines with line numbers. "
                "If start_line/end_line are given (without pattern), returns that slice with line numbers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'macros.xml', 'test-data/sample.bam'"},
                    "start_line": {"type": "integer", "description": "Start reading from this line (1-indexed). Optional."},
                    "end_line": {"type": "integer", "description": "Stop reading at this line (inclusive). Optional."},
                    "pattern": {"type": "string", "description": "Regex pattern to search for. Returns matching lines with line numbers. Optional."},
                },
                "required": ["path"],
            },
            handler=file_writer.read_file,
        ),
        ToolDefinition(
            name="search_github",
            description="Search GitHub repos — useful for verifying CLI flags, checking upstream examples.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
            handler=lambda args: _format_github(search_github(args["query"])),
        ),
        ToolDefinition(
            name="search_web",
            description="General web search fallback (DuckDuckGo). Returns titles, URLs, and snippets.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
            handler=lambda args: _format_web_results(search_web(args["query"])),
        ),
        ToolDefinition(
            name="search_bio_tools",
            description=(
                "Search the bio.tools registry for a tool by name. "
                "Use this to verify bio.tools IDs referenced in the tool XML."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Tool name to search for"}},
                "required": ["query"],
            },
            handler=lambda args: _format_bio_tools_results(search_bio_tools(args["query"])),
        ),
        ToolDefinition(
            name="fetch_toolshed_categories",
            description=(
                "Fetch the list of valid Tool Shed category names from the Tool Shed API. "
                "Use this to verify that .shed.yml categories are valid."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=lambda args: _format_toolshed_categories(fetch_toolshed_categories()),
        ),
    ]

    if shutil.which("planemo"):
        tools.append(ToolDefinition(
            name="planemo_lint",
            description=(
                "Run planemo lint on a file or directory within the tool directory. "
                "Returns lint warnings and errors. Use this to catch issues."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to lint (file or directory), e.g. 'my_tool.xml' or '.'"},
                },
                "required": ["path"],
            },
            handler=file_writer.planemo_lint,
            timeout=180,
        ))
        tools.append(ToolDefinition(
            name="planemo_test",
            description=(
                "Run planemo test on a tool XML or directory within the tool directory. "
                "Returns a summary of test failures. Note: tests may take several minutes to run."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to test (tool XML or directory), e.g. 'my_tool.xml' or '.'"},
                },
                "required": ["path"],
            },
            handler=file_writer.planemo_test,
            timeout=300,
        ))

    if shutil.which("micromamba") or shutil.which("conda"):
        tools.append(ToolDefinition(
            name="run_in_conda",
            description=(
                "Install conda packages from bioconda/conda-forge and run a command. "
                "Creates a cached environment (reused across calls with the same packages). "
                "The command runs in the tool directory so test data is accessible. "
                "Use this to verify CLI flags, inspect output formats, or check command behavior. "
                "Only bioconda packages are supported — tools not in bioconda cannot be test-run. "
                "Use search_bioconda first to verify a package exists."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Conda package specs, e.g. [\"samtools=1.21\"] or [\"bcftools\", \"htslib\"]",
                    },
                    "command": {
                        "type": "string",
                        "description": "Shell command to run, e.g. 'samtools --help' or 'samtools view test-data/sample.bam | head'",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Command timeout in seconds (default 120, max 300)",
                    },
                },
                "required": ["packages", "command"],
            },
            handler=file_writer.run_in_conda,
            timeout=360,
        ))
        tools.append(ToolDefinition(
            name="track_file",
            description=(
                "Track a file already on disk as a generated file so it gets included in the PR. "
                "Use this for files produced by run_in_conda that you want in the PR — especially "
                "binary files (HDF5, BAM, bgzipped) that cannot be read with read_file or written "
                "with write_file. The path must be relative to the tool directory and the file "
                "must already exist on disk."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file on disk (e.g. 'test-data/lookup_table.h5')",
                    },
                },
                "required": ["path"],
            },
            handler=file_writer.track_file,
            timeout=30,
        ))

    return tools


def _format_github(info) -> str:
    if not info:
        return "No GitHub repo found."
    import json
    return json.dumps({
        "full_name": info.full_name,
        "url": info.url,
        "description": info.description,
        "stars": info.stars,
        "language": info.language,
        "license": info.license,
    })


def _format_web_results(results: list) -> str:
    if not results:
        return "No web search results found."
    import json
    return json.dumps([{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results])


def _format_bio_tools_results(result) -> str:
    if not result or result.total_results == 0:
        return "No bio.tools entries found."
    import json
    return json.dumps([
        {
            "biotools_id": e.biotools_id,
            "name": e.name,
            "description": e.description,
            "homepage": e.homepage,
            "tooltype": e.tooltype,
        }
        for e in result.entries
    ])


def _format_toolshed_categories(categories: list[str]) -> str:
    if not categories:
        return "Failed to fetch Tool Shed categories."
    return "Valid Tool Shed categories:\n" + "\n".join(f"- {c}" for c in categories)


# ---------------------------------------------------------------------------
# Review runner
# ---------------------------------------------------------------------------

def run_review(
    ctx: ReviewContext,
    config: BotConfig,
    api_key: str,
    tool_dir: Path,
) -> ReviewResult:
    """Run the review agent on the tool files.

    The review agent gets fresh context (no inherited conversation). It uses
    read-only tools to inspect files, run planemo, and search.
    """
    system_prompt, user_prompt = build_review_prompt(ctx)

    # FileWriter is used only for read_file/planemo handlers — never for writing.
    # We set mode="review" to signal it's a review context (no write tools offered).
    file_writer = FileWriter(tool_dir, mode="review", env_scrub_names={config.api.api_key_env})
    tools = _build_review_tool_definitions(file_writer)

    with ApiClient(
        config.api.base_url, api_key, config.api.model,
        read_timeout=config.api.read_timeout,
        fallback_models=config.api.fallback_models,
    ) as client:
        result = run_agent_loop(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            max_iterations=config.api.max_tool_iterations,
            temperature=config.api.temperature_generate,
            max_context_chars=config.api.max_context_chars,
        )

    findings = parse_findings(result.content or "")
    return ReviewResult(
        findings=findings,
        raw_output=result.content or "",
        agent_iterations=result.iterations,
        terminated_naturally=result.terminated_naturally,
    )


# ---------------------------------------------------------------------------
# Findings parser
# ---------------------------------------------------------------------------

# Matches: ### [severity] category: file:line
_FINDING_RE = re.compile(
    r"^###\s*\[(\w+)\]\s*(\w+):\s*(\S+?)(?::(\d+))?\s*$",
    re.MULTILINE,
)


def parse_findings(raw_output: str) -> list[ReviewFinding]:
    """Parse structured findings from the review agent's text output.

    Expected format (one finding per block):
    ```
    ### [severity] category: file:line
    Description of the issue.
    Suggestion: How to fix it (if applicable).
    ```

    Falls back gracefully: if no findings are parsed, returns a single finding
    with the raw output as the description so nothing is lost.
    """
    if not raw_output.strip():
        return []

    findings: list[ReviewFinding] = []
    matches = list(_FINDING_RE.finditer(raw_output))

    for i, match in enumerate(matches):
        severity = match.group(1).lower()
        category = match.group(2).lower()
        file_path = match.group(3)
        line_str = match.group(4)
        line = int(line_str) if line_str else None

        # Content between this header and the next one (or end of text)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_output)
        body = raw_output[start:end].strip()

        # Split body into description and suggestion
        suggestion = None
        # Look for "Suggestion:" prefix
        suggestion_match = re.search(r"^Suggestion:\s*(.+)$", body, re.MULTILINE | re.DOTALL)
        if suggestion_match:
            suggestion = suggestion_match.group(1).strip()
            description = body[:suggestion_match.start()].strip()
        else:
            description = body.strip()

        if not description:
            description = "(no description provided)"

        # Normalize severity — if unrecognized, default to "warning"
        if severity not in _VALID_SEVERITIES:
            severity = "warning"

        # Normalize category — if unrecognized, keep as-is (don't lose info)
        findings.append(ReviewFinding(
            category=category,
            severity=severity,
            file=file_path if file_path and file_path != "None" else None,
            line=line,
            description=description,
            suggestion=suggestion,
        ))

    if not findings:
        # Check for the "no issues" case
        if re.search(r"no issues found|looks good|no problems", raw_output, re.IGNORECASE):
            return []
        # Fallback: treat the whole output as a single finding
        logger.warning("Could not parse structured findings from review output — using raw fallback")
        return [ReviewFinding(
            category="validation",
            severity="warning",
            file=None,
            line=None,
            description=raw_output[:2000],
            suggestion=None,
        )]

    return findings


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def format_review_comment(result: ReviewResult, tool_dir_name: str) -> str:
    """Format findings as a GitHub PR comment for standalone mode."""
    if not result.findings:
        return f"## Tool Review: `tools/{tool_dir_name}/`\n\n✅ No issues found. The tool files look good."

    parts: list[str] = [f"## Tool Review: `tools/{tool_dir_name}/`\n"]

    # Summary line
    counts = {"critical": 0, "warning": 0, "suggestion": 0}
    for f in result.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    summary_parts = []
    if counts["critical"]:
        summary_parts.append(f"{_SEVERITY_EMOJI['critical']} {counts['critical']} critical")
    if counts["warning"]:
        summary_parts.append(f"{_SEVERITY_EMOJI['warning']} {counts['warning']} warning(s)")
    if counts["suggestion"]:
        summary_parts.append(f"{_SEVERITY_EMOJI['suggestion']} {counts['suggestion']} suggestion(s)")
    parts.append("**Summary:** " + " · ".join(summary_parts) + "\n")
    parts.append("---\n")

    # Group by severity
    for severity in ("critical", "warning", "suggestion"):
        severity_findings = [f for f in result.findings if f.severity == severity]
        if not severity_findings:
            continue
        parts.append(f"### {_SEVERITY_EMOJI[severity]} {severity.title()}\n")
        for f in severity_findings:
            location = ""
            if f.file:
                location = f"`{f.file}`"
                if f.line:
                    location += f":{f.line}"
            parts.append(f"**[{f.category}]**{f' {location}' if location else ''}\n")
            parts.append(f"{f.description}\n")
            if f.suggestion:
                parts.append(f"*Suggestion: {f.suggestion}*\n")
            parts.append("")

    if not result.terminated_naturally:
        parts.append("---\n")
        parts.append("⚠️ Review agent did not terminate naturally — findings may be incomplete.\n")

    return "\n".join(parts)


def format_findings_as_feedback(result: ReviewResult) -> str:
    """Format findings as a user message for the integrated fix loop.

    This is fed back to the original agent (continuing from its conversation
    history) so it can fix the identified issues — same pattern as validation
    retries in run_agent_with_validation.
    """
    if not result.findings:
        return "A review of your files found no issues. No changes needed."

    parts: list[str] = [
        "A review of your generated files found the following issues:",
        "",
    ]

    # Only feed back critical and warning findings — suggestions are optional
    actionable = [f for f in result.findings if f.severity in ("critical", "warning")]
    if not actionable:
        return "A review of your files found only minor suggestions. No changes required."

    for f in actionable:
        location = ""
        if f.file:
            location = f" ({f.file}"
            if f.line:
                location += f":{f.line}"
            location += ")"
        parts.append(f"**[{f.severity}] {f.category}**{location}:")
        parts.append(f"{f.description}")
        if f.suggestion:
            parts.append(f"Fix: {f.suggestion}")
        parts.append("")

    parts.append(
        "Please fix the critical and warning issues listed above by rewriting "
        "the affected files with write_file. Only rewrite files that need fixing."
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Integrated review (review + fix loop)
# ---------------------------------------------------------------------------

def run_integrated_review(
    tool_dir: Path,
    config: BotConfig,
    api_key: str,
    validation_passed: bool,
    file_writer: FileWriter,
    original_result: AgentResult,
    original_files: list[GeneratedFile],
    system_prompt: str,
    user_prompt: str,
    tools: list[ToolDefinition],
    no_files_nudge: str | None = None,
    write_tools: set[str] | None = None,
    plan_markdown: str | None = None,
) -> tuple[list[GeneratedFile], AgentResult, ValidationResult, ReviewResult | None]:
    """Run integrated review + fix rounds after generate/feedback.

    Returns (final_files, final_result, final_validation, review_result).
    If review is disabled or no actionable findings, returns original values
    and the review result (or None if review didn't run).
    """
    mode = config.integrated_review_mode
    if mode == "never" or config.max_review_fix_rounds <= 0:
        return original_files, original_result, ValidationResult(valid=True, errors=[]), None

    if mode == "on-validation-pass" and not validation_passed:
        logger.info("Skipping integrated review (validation did not pass)")
        return original_files, original_result, ValidationResult(valid=True, errors=[]), None

    logger.info("Running integrated review (mode=%s, max_rounds=%d)", mode, config.max_review_fix_rounds)

    # Initial review — fresh context
    ctx = collect_review_context(tool_dir, config, plan_markdown=plan_markdown)
    if not ctx.existing_files:
        logger.warning("No files found for review in %s", tool_dir)
        return original_files, original_result, ValidationResult(valid=True, errors=[]), None

    review_result = run_review(ctx, config, api_key, tool_dir)
    logger.info(
        "Review complete: %d findings (%d critical, %d warning, %d suggestion)",
        len(review_result.findings),
        sum(1 for f in review_result.findings if f.severity == "critical"),
        sum(1 for f in review_result.findings if f.severity == "warning"),
        sum(1 for f in review_result.findings if f.severity == "suggestion"),
    )

    # If no actionable findings, no fix rounds needed
    if not review_result.has_critical and not review_result.has_warnings:
        return original_files, original_result, ValidationResult(valid=True, errors=[]), review_result

    # Fix rounds — continue from original conversation history
    result = original_result
    files = original_files
    validation = ValidationResult(valid=True, errors=[])

    for round_num in range(1, config.max_review_fix_rounds + 1):
        logger.info("Review fix round %d/%d", round_num, config.max_review_fix_rounds)

        feedback_msg = format_findings_as_feedback(review_result)

        # Run the original agent with the review findings appended to its
        # conversation history — same pattern as validation retries.
        result, files, validation, _retries = run_agent_with_validation(
            client=ApiClient(
                config.api.base_url, api_key, config.api.model,
                read_timeout=config.api.read_timeout,
                fallback_models=config.api.fallback_models,
            ),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            file_writer=file_writer,
            config=config,
            no_files_nudge=no_files_nudge,
            write_tools=write_tools,
            max_iterations_override=None,
            initial_messages=result.messages + [{"role": "user", "content": feedback_msg}],
        )

        # Re-review the updated files — fresh context
        ctx = collect_review_context(tool_dir, config, plan_markdown=plan_markdown)
        review_result = run_review(ctx, config, api_key, tool_dir)
        logger.info(
            "Re-review (round %d): %d findings (%d critical, %d warning)",
            round_num, len(review_result.findings),
            sum(1 for f in review_result.findings if f.severity == "critical"),
            sum(1 for f in review_result.findings if f.severity == "warning"),
        )

        # If no more actionable findings, we're done
        if not review_result.has_critical and not review_result.has_warnings:
            logger.info("Review fix round %d resolved all actionable findings", round_num)
            break

    return files, result, validation, review_result
