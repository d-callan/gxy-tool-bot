"""Tests for the address_feedback module."""

from __future__ import annotations

from gxy_tool_bot.address_feedback import FeedbackContext, _build_feedback_user_prompt
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
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "my_tool.xml" in prompt
    assert "<tool/>" in prompt
    assert "macros.xml" in prompt
    assert "Current Tool Files" in prompt


def test_prompt_includes_maintainer_comments() -> None:
    ctx = FeedbackContext(
        pr_comments=[_make_comment("Please fix the help section", "alice")],
        review_comments=[],
        failed_checks=[],
        existing_files={"tool.xml": "<tool/>"},
        tool_dir_name="my_tool",
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
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "tools/sdust/" in prompt
    assert "Do NOT" in prompt
