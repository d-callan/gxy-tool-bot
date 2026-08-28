"""Tests for the review module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from gxy_tool_bot.review import (
    ReviewContext,
    ReviewFinding,
    ReviewResult,
    _build_review_tool_definitions,
    format_findings_as_feedback,
    format_review_comment,
    parse_findings,
)
from gxy_tool_bot.utils import read_tool_files

# ---------------------------------------------------------------------------
# parse_findings tests
# ---------------------------------------------------------------------------

def test_parse_findings_well_formatted() -> None:
    raw = """## Findings

### [critical] conventions: my_tool.xml:15
Tool version is hardcoded instead of using @TOOL_VERSION@ token.
Suggestion: Replace version="1.2.3" with version="@TOOL_VERSION@+galaxy@VERSION_SUFFIX@"

### [warning] test_coverage: my_tool.xml:22
Only one test case — add a test for edge case with empty input.
Suggestion: Add a second <test> with empty input file.

### [suggestion] completeness: macros.xml
Missing @VERSION_SUFFIX@ token definition.
Suggestion: Add <token name="@VERSION_SUFFIX@">0</token> to macros.xml
"""
    findings = parse_findings(raw)
    assert len(findings) == 3

    assert findings[0].severity == "critical"
    assert findings[0].category == "conventions"
    assert findings[0].file == "my_tool.xml"
    assert findings[0].line == 15
    assert "hardcoded" in findings[0].description
    assert findings[0].suggestion is not None
    assert "@TOOL_VERSION@" in findings[0].suggestion

    assert findings[1].severity == "warning"
    assert findings[1].category == "test_coverage"
    assert findings[1].file == "my_tool.xml"
    assert findings[1].line == 22

    assert findings[2].severity == "suggestion"
    assert findings[2].category == "completeness"
    assert findings[2].file == "macros.xml"
    assert findings[2].line is None


def test_parse_findings_no_line_number() -> None:
    raw = """## Findings

### [warning] completeness: macros.xml
Missing @VERSION_SUFFIX@ token.
"""
    findings = parse_findings(raw)
    assert len(findings) == 1
    assert findings[0].file == "macros.xml"
    assert findings[0].line is None


def test_parse_findings_no_suggestion() -> None:
    raw = """## Findings

### [critical] validation: tool.xml:5
XML is not well-formed.
"""
    findings = parse_findings(raw)
    assert len(findings) == 1
    assert findings[0].suggestion is None
    assert "well-formed" in findings[0].description


def test_parse_findings_empty() -> None:
    findings = parse_findings("")
    assert findings == []


def test_parse_findings_no_issues_found() -> None:
    raw = "## Findings\n\nNo issues found. The tool files look good."
    findings = parse_findings(raw)
    assert findings == []


def test_parse_findings_malformed_fallback() -> None:
    """If output doesn't match the structured format, fall back to raw."""
    raw = "This is just some random text without structured findings."
    findings = parse_findings(raw)
    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert findings[0].description == raw


def test_parse_findings_unknown_severity_normalizes() -> None:
    raw = """## Findings

### [blooper] conventions: tool.xml:1
Something is wrong.
"""
    findings = parse_findings(raw)
    assert len(findings) == 1
    assert findings[0].severity == "warning"  # unknown → warning


def test_parse_findings_multiple_findings() -> None:
    raw = """## Findings

### [critical] validation: a.xml:1
Issue A.
Suggestion: Fix A.

### [critical] validation: b.xml:2
Issue B.

### [warning] conventions: c.xml:3
Issue C.
Suggestion: Fix C.
"""
    findings = parse_findings(raw)
    assert len(findings) == 3
    assert findings[0].file == "a.xml"
    assert findings[1].file == "b.xml"
    assert findings[2].file == "c.xml"


# ---------------------------------------------------------------------------
# ReviewResult tests
# ---------------------------------------------------------------------------

def test_review_result_has_critical() -> None:
    findings = [
        ReviewFinding("conventions", "critical", "tool.xml", 1, "desc", None),
        ReviewFinding("conventions", "warning", "tool.xml", 2, "desc", None),
    ]
    result = ReviewResult(findings=findings, raw_output="", agent_iterations=5, terminated_naturally=True)
    assert result.has_critical is True
    assert result.has_warnings is True


def test_review_result_no_critical() -> None:
    findings = [
        ReviewFinding("conventions", "warning", "tool.xml", 2, "desc", None),
        ReviewFinding("conventions", "suggestion", "tool.xml", 3, "desc", None),
    ]
    result = ReviewResult(findings=findings, raw_output="", agent_iterations=5, terminated_naturally=True)
    assert result.has_critical is False
    assert result.has_warnings is True


def test_review_result_empty() -> None:
    result = ReviewResult(findings=[], raw_output="", agent_iterations=0, terminated_naturally=True)
    assert result.has_critical is False
    assert result.has_warnings is False


# ---------------------------------------------------------------------------
# format_review_comment tests
# ---------------------------------------------------------------------------

def test_format_review_comment_with_findings() -> None:
    findings = [
        ReviewFinding("conventions", "critical", "tool.xml", 15, "Hardcoded version.", "Use @TOOL_VERSION@."),
        ReviewFinding("test_coverage", "warning", "tool.xml", 22, "Only one test.", "Add edge case test."),
    ]
    result = ReviewResult(findings=findings, raw_output="", agent_iterations=10, terminated_naturally=True)
    comment = format_review_comment(result, "my_tool")
    assert "Tool Review" in comment
    assert "my_tool" in comment
    assert "critical" in comment.lower()
    assert "warning" in comment.lower()
    assert "Hardcoded version" in comment
    assert "@TOOL_VERSION@" in comment


def test_format_review_comment_no_findings() -> None:
    result = ReviewResult(findings=[], raw_output="", agent_iterations=5, terminated_naturally=True)
    comment = format_review_comment(result, "my_tool")
    assert "No issues found" in comment


def test_format_review_comment_not_terminated() -> None:
    findings = [ReviewFinding("conventions", "critical", "tool.xml", 1, "desc", None)]
    result = ReviewResult(findings=findings, raw_output="", agent_iterations=10, terminated_naturally=False)
    comment = format_review_comment(result, "my_tool")
    assert "did not terminate naturally" in comment


def test_format_review_comment_suggestions_only() -> None:
    findings = [ReviewFinding("completeness", "suggestion", "macros.xml", None, "Minor issue.", "Optional fix.")]
    result = ReviewResult(findings=findings, raw_output="", agent_iterations=5, terminated_naturally=True)
    comment = format_review_comment(result, "my_tool")
    assert "suggestion" in comment.lower()
    assert "critical" not in comment.lower()


# ---------------------------------------------------------------------------
# format_findings_as_feedback tests
# ---------------------------------------------------------------------------

def test_format_findings_as_feedback_with_actionable() -> None:
    findings = [
        ReviewFinding("conventions", "critical", "tool.xml", 15, "Hardcoded version.", "Use @TOOL_VERSION@."),
        ReviewFinding("test_coverage", "warning", "tool.xml", 22, "Only one test.", "Add edge case."),
        ReviewFinding("completeness", "suggestion", "macros.xml", None, "Minor.", "Optional."),
    ]
    result = ReviewResult(findings=findings, raw_output="", agent_iterations=10, terminated_naturally=True)
    feedback = format_findings_as_feedback(result)
    assert "review" in feedback.lower()
    assert "Hardcoded version" in feedback
    assert "Use @TOOL_VERSION@" in feedback
    assert "Only one test" in feedback
    # Suggestions should not be included in actionable feedback
    assert "Minor" not in feedback
    assert "write_file" in feedback


def test_format_findings_as_feedback_no_findings() -> None:
    result = ReviewResult(findings=[], raw_output="", agent_iterations=5, terminated_naturally=True)
    feedback = format_findings_as_feedback(result)
    assert "no issues" in feedback.lower()


def test_format_findings_as_feedback_suggestions_only() -> None:
    findings = [ReviewFinding("completeness", "suggestion", "macros.xml", None, "Minor.", "Optional.")]
    result = ReviewResult(findings=findings, raw_output="", agent_iterations=5, terminated_naturally=True)
    feedback = format_findings_as_feedback(result)
    assert "only minor suggestions" in feedback.lower()


def test_format_findings_as_feedback_no_suggestion() -> None:
    findings = [ReviewFinding("validation", "critical", "tool.xml", 1, "Broken XML.", None)]
    result = ReviewResult(findings=findings, raw_output="", agent_iterations=5, terminated_naturally=True)
    feedback = format_findings_as_feedback(result)
    assert "Broken XML" in feedback
    assert "Fix:" not in feedback


# ---------------------------------------------------------------------------
# _build_review_tool_definitions tests
# ---------------------------------------------------------------------------

def test_review_tools_are_read_only() -> None:
    from gxy_tool_bot.generator import FileWriter
    fw = FileWriter(Path("/tmp"), mode="review")
    tools = _build_review_tool_definitions(fw)
    tool_names = {t.name for t in tools}
    # Read-only tools should be present
    assert "read_file" in tool_names
    assert "search_github" in tool_names
    assert "search_web" in tool_names
    assert "search_bio_tools" in tool_names
    # Write tools must NOT be present
    assert "write_file" not in tool_names
    assert "compress_file" not in tool_names
    assert "download_file" not in tool_names
    assert "set_tool_dir" not in tool_names
    assert "move_file" not in tool_names
    assert "delete_file" not in tool_names
    assert "give_up" not in tool_names


# ---------------------------------------------------------------------------
# read_tool_files tests (shared helper)
# ---------------------------------------------------------------------------

def test_read_tool_files(tmp_path: Path) -> None:
    (tmp_path / "tool.xml").write_text("<tool/>")
    (tmp_path / "macros.xml").write_text("<macros/>")
    (tmp_path / "test-data").mkdir()
    (tmp_path / "test-data" / "sample.fasta").write_text(">seq\nACGT\n")
    (tmp_path / ".tool-name").write_text("my_tool")

    files = read_tool_files(tmp_path)
    assert "tool.xml" in files
    assert "macros.xml" in files
    assert "test-data/sample.fasta" in files
    # .tool-name should be skipped
    assert ".tool-name" not in files
    assert all(f != ".tool-name" for f in files)


def test_read_tool_files_nonexistent_dir(tmp_path: Path) -> None:
    files = read_tool_files(tmp_path / "nonexistent")
    assert files == {}


def test_read_tool_files_binary_fallback(tmp_path: Path) -> None:
    (tmp_path / "binary.dat").write_bytes(b"\x00\x01\x02\xff")
    files = read_tool_files(tmp_path)
    assert "binary.dat" in files
    # Should have decoded with errors="replace"
    assert "\x00" not in files["binary.dat"] or isinstance(files["binary.dat"], str)


# ---------------------------------------------------------------------------
# Config tests for review fields
# ---------------------------------------------------------------------------

def test_config_review_defaults(tmp_path: Path) -> None:
    from gxy_tool_bot.config import load_config
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "api:\n"
        "  base_url: http://localhost\n"
        "  model: test-model\n"
        "exemplars:\n"
        "  - url: http://example.com/tool.xml\n"
        "repo: owner/repo\n"
    )
    config = load_config(config_path)
    assert config.integrated_review_mode == "never"
    assert config.max_review_fix_rounds == 1
    assert config.labels.review == "review"


def test_config_review_custom(tmp_path: Path) -> None:
    from gxy_tool_bot.config import load_config
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "api:\n"
        "  base_url: http://localhost\n"
        "  model: test-model\n"
        "exemplars:\n"
        "  - url: http://example.com/tool.xml\n"
        "repo: owner/repo\n"
        "integrated_review_mode: always\n"
        "max_review_fix_rounds: 3\n"
        "labels:\n"
        "  review: custom-review\n"
    )
    config = load_config(config_path)
    assert config.integrated_review_mode == "always"
    assert config.max_review_fix_rounds == 3
    assert config.labels.review == "custom-review"


# ---------------------------------------------------------------------------
# _fetch_plan_from_pr tests
# ---------------------------------------------------------------------------

def test_fetch_plan_from_pr_extracts_issue_number() -> None:
    """_fetch_plan_from_pr should parse 'Closes #N' from PR body and fetch the plan."""
    from gxy_tool_bot.review import _fetch_plan_from_pr
    from gxy_tool_bot.planner import PLAN_MARKER

    gh = MagicMock()
    gh.get_pr.return_value = {"body": "Generated by gxy-tool-bot for issue #42\n\nCloses #42"}
    gh.get_issue_comments.return_value = [
        MagicMock(body=f"{PLAN_MARKER}\n## Tool Plan\n\nInputs: foo, bar"),
    ]

    plan = _fetch_plan_from_pr(gh, 99)
    assert plan is not None
    assert "Tool Plan" in plan
    gh.get_pr.assert_called_once_with(99)
    gh.get_issue_comments.assert_called_once_with(42)


def test_fetch_plan_from_pr_no_closes_reference() -> None:
    """If PR body has no 'Closes #N', return None."""
    from gxy_tool_bot.review import _fetch_plan_from_pr

    gh = MagicMock()
    gh.get_pr.return_value = {"body": "A regular PR description without issue reference"}

    plan = _fetch_plan_from_pr(gh, 99)
    assert plan is None


def test_fetch_plan_from_pr_no_plan_comment() -> None:
    """If the linked issue has no plan comment, return None."""
    from gxy_tool_bot.review import _fetch_plan_from_pr

    gh = MagicMock()
    gh.get_pr.return_value = {"body": "Closes #42"}
    gh.get_issue_comments.return_value = [MagicMock(body="just a regular comment")]

    plan = _fetch_plan_from_pr(gh, 99)
    assert plan is None


def test_fetch_plan_from_pr_handles_errors() -> None:
    """If GitHub API calls fail, return None gracefully."""
    from gxy_tool_bot.review import _fetch_plan_from_pr

    gh = MagicMock()
    gh.get_pr.side_effect = Exception("API error")

    plan = _fetch_plan_from_pr(gh, 99)
    assert plan is None


# ---------------------------------------------------------------------------
# build_review_prompt with plan
# ---------------------------------------------------------------------------

def test_build_review_prompt_includes_plan() -> None:
    from gxy_tool_bot.review import build_review_prompt

    ctx = ReviewContext(
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="my_tool",
        ci_failures=[],
        ci_artifacts={},
        exemplars_text="",
        plan_markdown="## Tool Plan\n\nInputs: foo, bar\nOutputs: baz",
    )
    system, user = build_review_prompt(ctx)
    assert "Tool Plan / Spec" in user
    assert "Inputs: foo, bar" in user
    assert "completeness" in user


def test_build_review_prompt_without_plan() -> None:
    from gxy_tool_bot.review import build_review_prompt

    ctx = ReviewContext(
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="my_tool",
        ci_failures=[],
        ci_artifacts={},
        exemplars_text="",
        plan_markdown=None,
    )
    system, user = build_review_prompt(ctx)
    assert "Tool Plan / Spec" not in user


def test_build_review_prompt_mentions_agent_notes() -> None:
    from gxy_tool_bot.review import build_review_prompt

    ctx = ReviewContext(
        existing_files={"tool.xml": "<tool/>", ".agent-notes": "## notes"},
        tool_dir_name="my_tool",
        ci_failures=[],
        ci_artifacts={},
        exemplars_text="",
    )
    system, user = build_review_prompt(ctx)
    assert ".agent-notes" in user
