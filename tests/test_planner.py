"""Tests for the planner module."""

from __future__ import annotations

from gxy_tool_bot.planner import PLAN_MARKER, ToolRequest, find_plan_comment, parse_issue_body


def test_parse_issue_body_structured() -> None:
    body = """
Tool name: samtools sort

Description: Sort BAM files by coordinates

Links:
- https://github.com/samtools/samtools
- https://doi.org/10.1093/bioinformatics/btp352

Contact: @d-callan
"""
    request = parse_issue_body(body)
    assert request.tool_name == "samtools sort"
    assert request.description == "Sort BAM files by coordinates"
    assert len(request.links) == 2
    assert request.contact == "@d-callan"


def test_parse_issue_body_unstructured() -> None:
    body = "I want a tool that does fastq quality control."
    request = parse_issue_body(body)
    assert request.tool_name == "unknown"
    assert "fastq quality control" in request.description


def test_find_plan_comment() -> None:
    from gxy_tool_bot.github_client import Comment

    comments = [
        Comment(id=1, body="Some random comment", author="alice"),
        Comment(id=2, body=f"{PLAN_MARKER}\n# Tool Plan: samtools sort\n\n## Summary\n...", author="bot"),
        Comment(id=3, body="Looks good!", author="bob"),
    ]

    plan = find_plan_comment(comments)
    assert plan is not None
    assert plan.startswith("# Tool Plan: samtools sort")
    assert PLAN_MARKER not in plan


def test_find_plan_comment_not_found() -> None:
    from gxy_tool_bot.github_client import Comment

    comments = [
        Comment(id=1, body="No plan here", author="alice"),
    ]

    plan = find_plan_comment(comments)
    assert plan is None
