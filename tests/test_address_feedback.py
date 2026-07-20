"""Tests for the address_feedback module."""

from __future__ import annotations

from gxy_tool_bot.address_feedback import FeedbackContext, _build_feedback_user_prompt
from gxy_tool_bot.github_client import Comment


def _make_comment(body: str, author: str = "maintainer", cid: int = 1) -> Comment:
    return Comment(id=cid, body=body, author=author)


def test_prompt_includes_existing_files() -> None:
    ctx = FeedbackContext(
        pr_comments=[],
        review_comments=[],
        failed_checks=[],
        existing_files={"my_tool.xml": "<tool/>", "macros.xml": "<macros/>"},
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
    )
    prompt = _build_feedback_user_prompt(ctx)
    assert "Maintainer Comments" not in prompt
    assert "Review Comments" not in prompt
    assert "CI Check Failures" not in prompt
    assert "Fix the issues" in prompt
