"""Eval harness for generate and feedback loops.

Runs eval cases (YAML + reference files) against real LLM calls, measuring
validation pass rates, planemo lint/test pass rates, iteration counts, and
structural file correctness across difficulty tiers.

Cases live in ``eval/cases/<case_name>/`` directories, each with a ``case.yml``
and supporting files (plan.md for generate cases, broken tool files for
feedback cases).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from gxy_tool_bot.address_feedback import FeedbackContext, _build_feedback_user_prompt
from gxy_tool_bot.api_client import ApiClient
from gxy_tool_bot.config import BotConfig
from gxy_tool_bot.generator import (
    GeneratedFile,
    FileWriter,
    _build_tool_definitions,
    _build_exemplar_text,
    _derive_tool_owner,
    _load_template,
    generate_tool,
)
from gxy_tool_bot.utils import sanitized_env
from gxy_tool_bot.validation import run_agent_with_validation, validate_generated_files

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EvalCase:
    """A single eval case loaded from a case.yml file."""
    name: str
    type: str  # "generate" | "feedback"
    difficulty: str  # "easy" | "medium" | "hard"
    description: str
    case_dir: Path
    raw: dict  # full YAML content


@dataclass
class CaseResult:
    """Result of running a single eval case."""
    name: str
    type: str
    difficulty: str
    description: str
    passed: bool
    validation_passed: bool
    planemo_lint_passed: bool | None  # None if planemo not installed / not run
    planemo_test_passed: bool | None
    assertions_passed: bool
    assertions_failed: list[str]
    agent_iterations: int
    validation_retries: int
    agent_terminated_naturally: bool
    gave_up: bool
    files_generated: int
    error: str | None
    duration_seconds: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "difficulty": self.difficulty,
            "description": self.description,
            "passed": self.passed,
            "validation_passed": self.validation_passed,
            "planemo_lint_passed": self.planemo_lint_passed,
            "planemo_test_passed": self.planemo_test_passed,
            "assertions_passed": self.assertions_passed,
            "assertions_failed": self.assertions_failed,
            "agent_iterations": self.agent_iterations,
            "validation_retries": self.validation_retries,
            "agent_terminated_naturally": self.agent_terminated_naturally,
            "gave_up": self.gave_up,
            "files_generated": self.files_generated,
            "error": self.error,
            "duration_seconds": round(self.duration_seconds, 2),
        }


@dataclass
class EvalReport:
    """Aggregate results of an eval run."""
    results: list[CaseResult]
    summary: dict

    def to_dict(self) -> dict:
        return {
            "results": [r.to_dict() for r in self.results],
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Case discovery and filtering
# ---------------------------------------------------------------------------

def load_cases(case_dir: Path, filters: list[str] | None = None) -> list[EvalCase]:
    """Discover eval cases from case.yml files in case_dir (recursive).

    ``filters`` is a list of ``key=value`` strings. Supported keys:
    ``name`` (glob), ``type`` (exact), ``difficulty`` (exact).
    Multiple filters on the same key are OR'd; different keys are AND'd.
    """
    cases: list[EvalCase] = []
    for yml_path in sorted(case_dir.rglob("case.yml")):
        with open(yml_path) as f:
            raw = yaml.safe_load(f)
        if not raw:
            logger.warning("Empty case.yml: %s", yml_path)
            continue
        case = EvalCase(
            name=raw.get("name", yml_path.parent.name),
            type=raw["type"],
            difficulty=raw.get("difficulty", "medium"),
            description=raw.get("description", ""),
            case_dir=yml_path.parent,
            raw=raw,
        )
        if _matches_filters(case, filters):
            cases.append(case)
    return cases


def _matches_filters(case: EvalCase, filters: list[str] | None) -> bool:
    """Check if a case matches all filter groups."""
    if not filters:
        return True
    groups: dict[str, list[str]] = {}
    for f in filters:
        key, _, value = f.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        groups.setdefault(key, []).append(value)

    for key, values in groups.items():
        attr = getattr(case, key, None)
        if attr is None:
            return False
        if key == "name":
            if not any(fnmatch.fnmatch(attr, v) for v in values):
                return False
        else:
            if attr not in values:
                return False
    return True


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def run_assertion(assertion: dict, files: dict[str, bytes]) -> tuple[bool, str]:
    """Run a single structural assertion against generated files.

    Returns (passed, error_message). On success, error_message is empty.
    """
    atype = assertion.get("type", "")
    fname = assertion.get("file", "")

    if atype == "file_exists":
        if fname not in files:
            return False, f"Expected file '{fname}' was not generated"
        return True, ""

    if atype == "file_contains":
        if fname not in files:
            return False, f"File '{fname}' not found for file_contains check"
        pattern = assertion.get("pattern", "")
        content = files[fname].decode("utf-8", errors="replace")
        if not re.search(pattern, content):
            return False, f"File '{fname}' does not contain pattern '{pattern}'"
        return True, ""

    if atype == "xml_element_exists":
        if fname not in files:
            return False, f"File '{fname}' not found for XML assertion"
        xpath = assertion.get("xpath", "")
        try:
            root = ET.fromstring(files[fname].decode("utf-8"))
        except ET.ParseError as e:
            return False, f"XML parse error in '{fname}': {e}"
        if root.find(xpath) is None:
            return False, f"XPath '{xpath}' not found in '{fname}'"
        return True, ""

    if atype == "xml_element_count":
        if fname not in files:
            return False, f"File '{fname}' not found for XML assertion"
        xpath = assertion.get("xpath", "")
        expected = assertion.get("count", 1)
        try:
            root = ET.fromstring(files[fname].decode("utf-8"))
        except ET.ParseError as e:
            return False, f"XML parse error in '{fname}': {e}"
        actual = len(root.findall(xpath))
        if actual != expected:
            return False, f"XPath '{xpath}' in '{fname}' found {actual} elements, expected {expected}"
        return True, ""

    if atype == "xml_attribute_equals":
        if fname not in files:
            return False, f"File '{fname}' not found for XML assertion"
        xpath = assertion.get("xpath", "")
        attr = assertion.get("attribute", "")
        expected = assertion.get("value", "")
        try:
            root = ET.fromstring(files[fname].decode("utf-8"))
        except ET.ParseError as e:
            return False, f"XML parse error in '{fname}': {e}"
        elem = root.find(xpath)
        if elem is None:
            return False, f"XPath '{xpath}' not found in '{fname}'"
        actual = elem.get(attr, "")
        if actual != expected:
            return False, f"Attribute '{attr}' on '{xpath}' in '{fname}' is '{actual}', expected '{expected}'"
        return True, ""

    if atype == "xml_element_not_exists":
        if fname not in files:
            return False, f"File '{fname}' not found for XML assertion"
        xpath = assertion.get("xpath", "")
        try:
            root = ET.fromstring(files[fname].decode("utf-8"))
        except ET.ParseError as e:
            return False, f"XML parse error in '{fname}': {e}"
        if root.find(xpath) is not None:
            return False, f"XPath '{xpath}' unexpectedly found in '{fname}'"
        return True, ""

    if atype == "file_not_contains":
        if fname not in files:
            return False, f"File '{fname}' not found for file_not_contains check"
        pattern = assertion.get("pattern", "")
        content = files[fname].decode("utf-8", errors="replace")
        if re.search(pattern, content):
            return False, f"File '{fname}' contains pattern '{pattern}' that should not be present"
        return True, ""

    return False, f"Unknown assertion type: '{atype}'"


def run_assertions(assertions: list[dict], files: dict[str, bytes]) -> tuple[bool, list[str]]:
    """Run all assertions. Returns (all_passed, list_of_failure_messages)."""
    failures: list[str] = []
    for a in assertions:
        passed, msg = run_assertion(a, files)
        if not passed:
            failures.append(msg)
    return len(failures) == 0, failures


# ---------------------------------------------------------------------------
# Planemo runner
# ---------------------------------------------------------------------------

def _run_planemo_lint(target_dir: Path, env_scrub_names: set[str] | None = None) -> bool | None:
    """Run planemo lint on a directory. Returns True/False, or None if planemo not installed."""
    if not shutil.which("planemo"):
        return None
    try:
        result = subprocess.run(
            ["planemo", "lint", str(target_dir)],
            capture_output=True, text=True, timeout=180,
            env=sanitized_env(env_scrub_names),
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _run_planemo_test(target_dir: Path, env_scrub_names: set[str] | None = None) -> bool | None:
    """Run planemo test on a directory. Returns True/False, or None if planemo not installed."""
    if not shutil.which("planemo"):
        return None
    try:
        result = subprocess.run(
            ["planemo", "test", str(target_dir)],
            capture_output=True, text=True, timeout=600,
            env=sanitized_env(env_scrub_names),
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Case runners
# ---------------------------------------------------------------------------

def run_generate_case(
    case: EvalCase,
    config: BotConfig,
    api_key: str,
    work_dir: Path,
    run_planemo: bool = True,
) -> CaseResult:
    """Run a generate eval case."""
    start = time.time()
    output_dir = work_dir / case.name / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    plan_path = case.case_dir / case.raw.get("plan", "plan.md")
    plan_md = plan_path.read_text()

    try:
        generated, result, validation = generate_tool(
            plan_markdown=plan_md,
            config=config,
            api_key=api_key,
            output_dir=output_dir,
        )
    except Exception as e:
        logger.exception("Generate case '%s' failed", case.name)
        return CaseResult(
            name=case.name, type=case.type, difficulty=case.difficulty,
            description=case.description, passed=False,
            validation_passed=False, planemo_lint_passed=None,
            planemo_test_passed=None, assertions_passed=False,
            assertions_failed=[f"Exception: {e}"],
            agent_iterations=0, validation_retries=0,
            agent_terminated_naturally=False, gave_up=False,
            files_generated=0, error=str(e),
            duration_seconds=time.time() - start,
        )

    files_dict = {f.path: f.content for f in generated.files}
    assertions = case.raw.get("assertions", [])
    assertions_passed, assertion_failures = run_assertions(assertions, files_dict)

    planemo_lint = None
    planemo_test = None
    if run_planemo and not generated.give_up_reason:
        planemo_lint = _run_planemo_lint(output_dir, {config.api.api_key_env})
        planemo_test = _run_planemo_test(output_dir, {config.api.api_key_env})

    # Overall pass: validation passed, assertions passed, didn't give up.
    # Planemo is informational (may not be installed) — not required for "passed".
    passed = validation.valid and assertions_passed and not generated.give_up_reason

    return CaseResult(
        name=case.name, type=case.type, difficulty=case.difficulty,
        description=case.description, passed=passed,
        validation_passed=validation.valid,
        planemo_lint_passed=planemo_lint, planemo_test_passed=planemo_test,
        assertions_passed=assertions_passed, assertions_failed=assertion_failures,
        agent_iterations=result.iterations, validation_retries=0,
        agent_terminated_naturally=result.terminated_naturally,
        gave_up=generated.give_up_reason is not None,
        files_generated=len(generated.files), error=None,
        duration_seconds=time.time() - start,
    )


def run_feedback_case(
    case: EvalCase,
    config: BotConfig,
    api_key: str,
    work_dir: Path,
    run_planemo: bool = True,
) -> CaseResult:
    """Run a feedback eval case.

    Constructs a FeedbackContext from the case YAML (simulated comments + CI),
    sets up FileWriter with existing files, and runs the agent loop with
    validation — mirroring address_feedback without needing a real GitHub PR.
    """
    from gxy_tool_bot.github_client import Comment

    start = time.time()
    tool_dir = work_dir / case.name / "tool_dir"
    tool_dir.mkdir(parents=True, exist_ok=True)

    # Load existing tool files from the case directory
    existing_files: dict[str, str] = {}
    for fname in case.raw.get("existing_files", []):
        src = case.case_dir / fname
        if src.exists():
            existing_files[fname] = src.read_text()
            dest = tool_dir / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(existing_files[fname])

    if not existing_files:
        return CaseResult(
            name=case.name, type=case.type, difficulty=case.difficulty,
            description=case.description, passed=False,
            validation_passed=False, planemo_lint_passed=None,
            planemo_test_passed=None, assertions_passed=False,
            assertions_failed=["No existing files found in case"],
            agent_iterations=0, validation_retries=0,
            agent_terminated_naturally=False, gave_up=False,
            files_generated=0, error="No existing files",
            duration_seconds=time.time() - start,
        )

    # Build FeedbackContext from YAML
    fb = case.raw.get("feedback", {})
    review_comments = [
        Comment(
            id=i + 1,
            body=c.get("body", ""),
            author=c.get("author", "reviewer"),
            file_path=c.get("file_path"),
            line=c.get("line"),
        )
        for i, c in enumerate(fb.get("review_comments", []))
    ]
    pr_comments = [
        Comment(id=i + 1, body=c.get("body", ""), author=c.get("author", "reviewer"))
        for i, c in enumerate(fb.get("pr_comments", []))
    ]
    ctx = FeedbackContext(
        pr_comments=pr_comments,
        review_comments=review_comments,
        failed_checks=fb.get("failed_checks", []),
        existing_files=existing_files,
        tool_dir_name=tool_dir.name,
        ci_artifacts=fb.get("ci_artifacts", {}),
    )

    system_prompt = _load_template("feedback_system.txt").render()
    user_prompt = _build_feedback_user_prompt(ctx)

    if config.agent_notes:
        user_prompt += (
            "\n\n---\n\n## Agent Notes\n\n"
            "If a `.agent-notes` file exists, use `read_file` to read it for context on "
            "decisions made during generation or previous feedback rounds. "
            "Write notes incrementally as you work — call `add_agent_notes` each time you "
            "discover something worth noting (e.g. an upstream bug, a workaround, a failed "
            "approach). Do NOT wait until the end, as you may run out of iterations before "
            "you get there. Without these notes, the next feedback round starts from scratch."
        )

    # Set up file writer with existing files loaded
    file_writer = FileWriter(tool_dir, mode="feedback", env_scrub_names={config.api.api_key_env})
    for path, content in existing_files.items():
        file_writer.files[path] = content.encode("utf-8")

    tools = _build_tool_definitions(file_writer, config)

    _WRITE_TOOLS = {"write_file", "compress_file", "download_file", "track_file"}
    no_files_nudge = (
        "No files were modified in the previous attempt. The agent spent all iterations"
        " on research instead of fixing the issues.\n\n"
        "You MUST start fixing files immediately. Use `read_file` to inspect the files"
        " you need to modify, then use `write_file` to rewrite them."
    )

    try:
        with ApiClient(
            config.api.base_url, api_key, config.api.model,
            read_timeout=config.api.read_timeout,
            fallback_models=config.api.fallback_models,
        ) as client:
            result, files, validation, validation_retries = run_agent_with_validation(
                client=client,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools=tools,
                file_writer=file_writer,
                config=config,
                no_files_nudge=no_files_nudge,
                write_tools=_WRITE_TOOLS,
            )
    except Exception as e:
        logger.exception("Feedback case '%s' failed", case.name)
        return CaseResult(
            name=case.name, type=case.type, difficulty=case.difficulty,
            description=case.description, passed=False,
            validation_passed=False, planemo_lint_passed=None,
            planemo_test_passed=None, assertions_passed=False,
            assertions_failed=[f"Exception: {e}"],
            agent_iterations=0, validation_retries=0,
            agent_terminated_naturally=False, gave_up=False,
            files_generated=len(file_writer.files), error=str(e),
            duration_seconds=time.time() - start,
        )

    files_dict = {f.path: f.content for f in files}
    assertions = case.raw.get("assertions", [])
    assertions_passed, assertion_failures = run_assertions(assertions, files_dict)

    planemo_lint = None
    planemo_test = None
    if run_planemo and not file_writer.give_up_reason:
        planemo_lint = _run_planemo_lint(tool_dir, {config.api.api_key_env})
        planemo_test = _run_planemo_test(tool_dir, {config.api.api_key_env})

    gave_up = file_writer.give_up_reason is not None
    passed = validation.valid and assertions_passed and not gave_up

    return CaseResult(
        name=case.name, type=case.type, difficulty=case.difficulty,
        description=case.description, passed=passed,
        validation_passed=validation.valid,
        planemo_lint_passed=planemo_lint, planemo_test_passed=planemo_test,
        assertions_passed=assertions_passed, assertions_failed=assertion_failures,
        agent_iterations=result.iterations, validation_retries=validation_retries,
        agent_terminated_naturally=result.terminated_naturally,
        gave_up=gave_up,
        files_generated=len(file_writer.files), error=None,
        duration_seconds=time.time() - start,
    )


# ---------------------------------------------------------------------------
# Eval runner
# ---------------------------------------------------------------------------

def run_eval(
    cases: list[EvalCase],
    config: BotConfig,
    api_key: str,
    work_dir: Path,
    run_planemo: bool = True,
) -> EvalReport:
    """Run all cases sequentially and return an aggregate report."""
    results: list[CaseResult] = []
    for case in cases:
        logger.info("Running eval case: %s (%s, %s)", case.name, case.type, case.difficulty)
        if case.type == "generate":
            result = run_generate_case(case, config, api_key, work_dir, run_planemo)
        elif case.type == "feedback":
            result = run_feedback_case(case, config, api_key, work_dir, run_planemo)
        else:
            logger.warning("Unknown case type '%s' for case '%s'", case.type, case.name)
            continue
        results.append(result)
        _log_case_result(result)

    summary = _build_summary(results)
    return EvalReport(results=results, summary=summary)


def _log_case_result(result: CaseResult) -> None:
    status = "PASS" if result.passed else "FAIL"
    logger.info(
        "[%s] %s — validation=%s, assertions=%s, iters=%d, retries=%d, %.1fs",
        status, result.name,
        result.validation_passed, result.assertions_passed,
        result.agent_iterations, result.validation_retries,
        result.duration_seconds,
    )


def _build_summary(results: list[CaseResult]) -> dict:
    """Aggregate stats by difficulty, type, and overall."""
    if not results:
        return {}

    def _stats(subset: list[CaseResult]) -> dict:
        if not subset:
            return {"count": 0, "pass_rate": 0.0}
        passed = sum(1 for r in subset if r.passed)
        return {
            "count": len(subset),
            "pass_rate": round(passed / len(subset), 3),
            "validation_pass_rate": round(sum(1 for r in subset if r.validation_passed) / len(subset), 3),
            "avg_iterations": round(sum(r.agent_iterations for r in subset) / len(subset), 1),
            "avg_validation_retries": round(sum(r.validation_retries for r in subset) / len(subset), 1),
            "avg_duration_seconds": round(sum(r.duration_seconds for r in subset) / len(subset), 1),
        }

    planemo_results = [r for r in results if r.planemo_lint_passed is not None]
    planemo_stats = {}
    if planemo_results:
        planemo_stats = {
            "lint_pass_rate": round(sum(1 for r in planemo_results if r.planemo_lint_passed) / len(planemo_results), 3),
            "test_pass_rate": round(
                sum(1 for r in planemo_results if r.planemo_test_passed) / len(planemo_results), 3
            ) if any(r.planemo_test_passed is not None for r in planemo_results) else None,
        }

    return {
        "overall": _stats(results),
        "by_difficulty": {
            d: _stats([r for r in results if r.difficulty == d])
            for d in ("easy", "medium", "hard")
        },
        "by_type": {
            t: _stats([r for r in results if r.type == t])
            for t in ("generate", "feedback")
        },
        "planemo": planemo_stats,
    }


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_report_text(report: EvalReport) -> str:
    """Format an eval report as a human-readable text table."""
    lines: list[str] = []
    lines.append("=" * 80)
    lines.append("EVAL REPORT")
    lines.append("=" * 80)
    lines.append("")

    # Per-case table
    header = f"{'Case':<35} {'Type':<10} {'Diff':<7} {'Pass':<5} {'Val':<5} {'Assert':<7} {'Iter':<5} {'Retr':<5} {'Time':>7}"
    lines.append(header)
    lines.append("-" * len(header))
    for r in report.results:
        lines.append(
            f"{r.name:<35} {r.type:<10} {r.difficulty:<7} "
            f"{'✓' if r.passed else '✗':<5} "
            f"{'✓' if r.validation_passed else '✗':<5} "
            f"{'✓' if r.assertions_passed else '✗':<7} "
            f"{r.agent_iterations:<5} {r.validation_retries:<5} "
            f"{r.duration_seconds:>6.1f}s"
        )
    lines.append("")

    # Summary
    s = report.summary
    if s.get("overall"):
        lines.append("SUMMARY")
        lines.append("-" * 40)
        ov = s["overall"]
        lines.append(f"  Total cases:   {ov['count']}")
        lines.append(f"  Pass rate:     {ov['pass_rate'] * 100:.1f}%")
        lines.append(f"  Val pass rate: {ov['validation_pass_rate'] * 100:.1f}%")
        lines.append(f"  Avg iterations: {ov['avg_iterations']}")
        lines.append(f"  Avg retries:   {ov['avg_validation_retries']}")
        lines.append(f"  Avg duration:  {ov['avg_duration_seconds']}s")
        lines.append("")

        for label, key in (("By difficulty", "by_difficulty"), ("By type", "by_type")):
            lines.append(f"  {label}:")
            for k, v in s[key].items():
                if v["count"] > 0:
                    lines.append(f"    {k:<10} {v['pass_rate'] * 100:5.1f}% pass ({v['count']} cases, avg {v['avg_iterations']} iters)")
            lines.append("")

        if s.get("planemo"):
            p = s["planemo"]
            lines.append("  Planemo:")
            if p.get("lint_pass_rate") is not None:
                lines.append(f"    Lint pass rate: {p['lint_pass_rate'] * 100:.1f}%")
            if p.get("test_pass_rate") is not None:
                lines.append(f"    Test pass rate: {p['test_pass_rate'] * 100:.1f}%")
            lines.append("")

    # Failures detail
    failures = [r for r in report.results if not r.passed]
    if failures:
        lines.append("FAILURES DETAIL")
        lines.append("-" * 40)
        for r in failures:
            lines.append(f"  {r.name} ({r.difficulty}):")
            if r.error:
                lines.append(f"    Error: {r.error}")
            for msg in r.assertions_failed:
                lines.append(f"    Assertion: {msg}")
            if r.gave_up:
                lines.append("    Agent gave up")
            if not r.validation_passed:
                lines.append("    Validation failed")
            lines.append("")

    return "\n".join(lines)
