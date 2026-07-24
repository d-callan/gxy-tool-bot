"""Tests for the address_feedback module."""

from __future__ import annotations

from gxy_tool_bot.address_feedback import FeedbackContext, _build_feedback_user_prompt, _collect_feedback, _summarize_test_json
from gxy_tool_bot.github_client import Comment


def _make_comment(body: str, author: str = "maintainer", cid: int = 1,
                   file_path: str | None = None, line: int | None = None) -> Comment:
    return Comment(id=cid, body=body, author=author, file_path=file_path, line=line)


def test_prompt_includes_existing_files() -> None:
    ctx = FeedbackContext(
        pr_comments=[],
        review_comments=[],
        failed_checks=[],
        existing_files={"my_tool.xml": "<tool/>", "macros.xml": "<macros/>"},
        tool_dir_name="my_tool",
        ci_artifacts={},
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "my_tool.xml" in prompt
    assert "macros.xml" in prompt
    assert "Current Tool Files" in prompt
    assert "read_file" in prompt


def test_prompt_includes_maintainer_comments() -> None:
    ctx = FeedbackContext(
        pr_comments=[_make_comment("Please fix the help section", "alice")],
        review_comments=[],
        failed_checks=[],
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="my_tool",
        ci_artifacts={},
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "Maintainer Comments" in prompt
    assert "alice" in prompt
    assert "Please fix the help section" in prompt


def test_prompt_filters_bot_comments() -> None:
    ctx = FeedbackContext(
        pr_comments=[
            _make_comment("Tool generated successfully!", "github-actions[bot]", 1),
            _make_comment("Please fix the help section", "alice", 2),
        ],
        review_comments=[],
        failed_checks=[],
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="my_tool",
        ci_artifacts={},
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "alice" in prompt
    assert "github-actions" not in prompt
    assert "Tool generated successfully" not in prompt


def test_prompt_includes_review_comments() -> None:
    ctx = FeedbackContext(
        pr_comments=[],
        review_comments=[_make_comment("This macro is undefined", "bob")],
        failed_checks=[],
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="my_tool",
        ci_artifacts={},
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "Review Comments" in prompt
    assert "bob" in prompt
    assert "This macro is undefined" in prompt


def test_prompt_includes_ci_failures() -> None:
    ctx = FeedbackContext(
        pr_comments=[],
        review_comments=[],
        failed_checks=[
            {"name": "planemo-test", "status": "completed", "conclusion": "failure", "output": "Error: test failed"},
        ],
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="my_tool",
        ci_artifacts={},
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "CI Check Failures" in prompt
    assert "planemo-test" in prompt
    assert "Error: test failed" in prompt


def test_prompt_no_feedback_sections_when_empty() -> None:
    ctx = FeedbackContext(
        pr_comments=[],
        review_comments=[],
        failed_checks=[],
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="my_tool",
        ci_artifacts={},
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "Maintainer Comments" not in prompt
    assert "Review Comments" not in prompt
    assert "CI Check Failures" not in prompt
    assert "Fix the issues" in prompt


def test_prompt_review_comment_includes_file_and_line() -> None:
    """Review comments should include file path and line number in the prompt."""
    ctx = FeedbackContext(
        pr_comments=[],
        review_comments=[_make_comment("Fix this param", "bob", file_path="tools/my_tool/my_tool.xml", line=42)],
        failed_checks=[],
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="my_tool",
        ci_artifacts={},
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "tools/my_tool/my_tool.xml" in prompt
    assert "line 42" in prompt


def test_prompt_ci_output_filters_to_tool() -> None:
    """CI output should be filtered to lines relevant to the tool, not the full log."""
    ci_output = (
        "Linting tool tools/pureclip/pureclip.xml\n"
        "Failed linting\n"
        ".. WARNING (XMLOrder): [stdio] elements should come before [version_command]\n"
        "Linting tool tools/my_tool/my_tool.xml\n"
        "Failed linting\n"
        ".. WARNING (XMLOrder): [xrefs] elements should come before [help]\n"
    )
    ctx = FeedbackContext(
        pr_comments=[],
        review_comments=[],
        failed_checks=[
            {"name": "lint", "status": "completed", "conclusion": "failure", "output": ci_output},
        ],
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="my_tool",
        ci_artifacts={},
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "my_tool" in prompt
    assert "xrefs" in prompt
    # pureclip's issue should not appear
    assert "stdio" not in prompt


def test_prompt_includes_tool_scoping() -> None:
    """Prompt should clearly state which tool directory to work in."""
    ctx = FeedbackContext(
        pr_comments=[],
        review_comments=[],
        failed_checks=[],
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="sdust",
        ci_artifacts={},
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "tools/sdust/" in prompt
    assert "Do NOT" in prompt


def test_prompt_includes_ci_artifacts() -> None:
    """CI artifact reports should be included in the prompt."""
    ctx = FeedbackContext(
        pr_comments=[],
        review_comments=[],
        failed_checks=[],
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="my_tool",
        ci_artifacts={
            "Tool linting output/lint_report.txt": (
                "Linting tool tools/my_tool/my_tool.xml\n"
                "Failed linting\n"
                ".. WARNING (HelpInvalidRST): Invalid reStructuredText found in help\n"
            ),
        },
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "CI Artifact Reports" in prompt
    assert "Tool linting output" in prompt
    assert "HelpInvalidRST" in prompt


def test_prompt_no_artifact_section_when_empty() -> None:
    """Artifact section should not appear when no artifacts are present."""
    ctx = FeedbackContext(
        pr_comments=[],
        review_comments=[],
        failed_checks=[],
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="my_tool",
        ci_artifacts={},
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "CI Artifact Reports" not in prompt


def test_summarize_test_json_extracts_failures() -> None:
    """_summarize_test_json should extract only failed tests with useful fields."""
    raw = (
        '{"tests": ['
        '  {"id": "my_tool.test_toolbox.1", "data": {"status": "success"}},'
        '  {"id": "my_tool.test_toolbox.2", "data": {'
        '    "status": "error",'
        '    "execution_problem": "Command not found",'
        '    "output_problems": ["Output file missing"],'
        '    "job": {"command_line": "my_tool --input x", "stdout": "ok", "stderr": "err"}'
        '  }}'
        '], "summary": {"num_tests": 2, "num_failures": 0, "num_errors": 1, "num_skips": 0}}'
    )
    result = _summarize_test_json(raw)
    assert "my_tool.test_toolbox.2" in result
    assert "error" in result
    assert "Command not found" in result
    assert "Output file missing" in result
    assert "my_tool --input x" in result
    # Success test should not appear
    assert "test_toolbox.1" not in result


def test_summarize_test_json_all_passed() -> None:
    """_summarize_test_json should report all tests passed when no failures."""
    raw = (
        '{"tests": ['
        '  {"id": "my_tool.test_toolbox.1", "data": {"status": "success"}}'
        '], "summary": {"num_tests": 1, "num_failures": 0, "num_errors": 0, "num_skips": 0}}'
    )
    result = _summarize_test_json(raw)
    assert "All 1 tests passed." in result


def test_summarize_test_json_invalid_json() -> None:
    """_summarize_test_json should return truncated raw on invalid JSON."""
    result = _summarize_test_json("not json at all")
    assert result == "not json at all"


def test_collect_feedback_filters_resolved_comments(tmp_path) -> None:
    """_collect_feedback should filter out resolved review comments."""
    from unittest.mock import MagicMock

    gh = MagicMock()
    gh.get_pr_comments.return_value = []
    gh.get_pr_review_comments.return_value = [
        _make_comment("Fix this", "alice", cid=101),
        _make_comment("Also fix that", "bob", cid=102),
        _make_comment("Already addressed", "carol", cid=103),
    ]
    gh.get_resolved_review_comment_ids.return_value = {103}
    gh.get_pr_check_runs.return_value = []
    gh.get_pr_artifacts.return_value = []

    ctx = _collect_feedback(gh, 1, tmp_path)
    comment_ids = [c.id for c in ctx.review_comments]
    assert 101 in comment_ids
    assert 102 in comment_ids
    assert 103 not in comment_ids


def test_collect_feedback_includes_all_on_graphql_failure(tmp_path) -> None:
    """_collect_feedback should include all comments if GraphQL call fails."""
    from unittest.mock import MagicMock

    gh = MagicMock()
    gh.get_pr_comments.return_value = []
    gh.get_pr_review_comments.return_value = [
        _make_comment("Fix this", "alice", cid=101),
        _make_comment("Already addressed", "carol", cid=103),
    ]
    gh.get_resolved_review_comment_ids.side_effect = RuntimeError("GraphQL error")
    gh.get_pr_check_runs.return_value = []
    gh.get_pr_artifacts.return_value = []

    ctx = _collect_feedback(gh, 1, tmp_path)
    assert len(ctx.review_comments) == 2
