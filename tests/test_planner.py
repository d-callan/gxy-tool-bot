"""Tests for the planner module."""

from __future__ import annotations

from gxy_tool_bot.planner import PLAN_MARKER, ToolRequest, count_tool_xmls_in_plan, find_plan_comment, parse_issue_body


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


def test_parse_issue_body_issue_form() -> None:
    """Issue-form bodies render fields as `### Label` headings."""
    body = """
Fill out this form to request a new Galaxy tool wrapper.

### Tool name

samtools sort

### Description

Sort BAM files by coordinate or read name.

### Links

https://github.com/samtools/samtools
https://doi.org/10.1093/bioinformatics/btp352

### Contact

@d-callan
"""
    request = parse_issue_body(body)
    assert request.tool_name == "samtools sort"
    assert request.description == "Sort BAM files by coordinate or read name."
    assert len(request.links) == 2
    assert request.contact == "@d-callan"


def test_parse_issue_body_issue_form_empty_optional() -> None:
    """Unfilled optional fields render as `_No response._` and parse as absent."""
    body = """
### Tool name

fastp

### Description

FASTQ preprocessor.

### Links

_No response._

### Contact

_No response._
"""
    request = parse_issue_body(body)
    assert request.tool_name == "fastp"
    assert request.description == "FASTQ preprocessor."
    assert request.links == []
    assert request.contact is None


def test_parse_issue_body_issue_form_multiline_description() -> None:
    """A description containing its own markdown heading is kept whole."""
    body = """
### Tool name

hyphy

### Description

Hypothesis testing framework.

### Input details

Takes alignments and trees.
"""
    request = parse_issue_body(body)
    assert request.tool_name == "hyphy"
    assert "### Input details" in request.description


def test_parse_issue_body_mixed_formats() -> None:
    """Issue-form fields override legacy fields without hiding other legacy fields."""
    body = """
Tool name: fastp
Contact: @d-callan

### Description

FASTQ preprocessor.
"""
    request = parse_issue_body(body)
    assert request.tool_name == "fastp"
    assert request.description == "FASTQ preprocessor."
    assert request.contact == "@d-callan"


def test_parse_issue_body_non_h3_field_heading() -> None:
    """Non-H3 field-like headings inside descriptions are preserved."""
    body = """
### Tool name

hyphy

### Description

Hypothesis testing framework.

## Links

The documentation is not published yet.
"""
    request = parse_issue_body(body)
    assert request.tool_name == "hyphy"
    assert "## Links" in request.description
    assert "The documentation is not published yet." in request.description


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


def test_count_tool_xmls_structured_line() -> None:
    """The structured 'Number of tool XML files: N' line is used when present."""
    plan = (
        "# Tool Plan: hyphy\n\n"
        "### Tool Structure\n"
        "Tool family with meme.xml, busted.xml, fel.xml sharing macros.xml.\n\n"
        "**Number of tool XML files:** 3\n"
    )
    assert count_tool_xmls_in_plan(plan) == 3


def test_count_tool_xmls_structured_line_bold() -> None:
    """Structured line tolerates surrounding markdown bold and case variants."""
    plan = "## Tool Structure\n\n**Number of tool XML files:** 4\n"
    assert count_tool_xmls_in_plan(plan) == 4


def test_count_tool_xmls_structured_line_clamps_to_one() -> None:
    """A structured count of 0 clamps to the minimum of 1."""
    plan = "**Number of tool XML files:** 0\n"
    assert count_tool_xmls_in_plan(plan) == 1


def test_count_tool_xmls_fallback_distinct_files() -> None:
    """Without the structured line, distinct .xml filenames are counted (macros excluded)."""
    plan = (
        "# Tool Plan: samtools\n\n"
        "### Tool Structure\n"
        "- samtools_view.xml\n"
        "- samtools_sort.xml\n"
        "- samtools_index.xml\n"
        "- macros.xml (shared)\n"
    )
    assert count_tool_xmls_in_plan(plan) == 3


def test_count_tool_xmls_fallback_dedupes_repeats() -> None:
    """Repeated mentions of the same .xml filename count once."""
    plan = (
        "We will write foo.xml. The foo.xml tool uses macros.xml.\n"
        "Tests for foo.xml go in test-data.\n"
    )
    assert count_tool_xmls_in_plan(plan) == 1


def test_count_tool_xmls_no_xml_mentions() -> None:
    """A plan with no .xml mentions returns the minimum of 1."""
    plan = "# Tool Plan: mystery\n\n## Summary\nA vague plan with no file names.\n"
    assert count_tool_xmls_in_plan(plan) == 1


def test_count_tool_xmls_structured_overrides_fallback() -> None:
    """The structured line wins even if the prose mentions a different number of XMLs."""
    plan = (
        "### Tool Structure\n"
        "foo.xml and bar.xml\n\n"
        "**Number of tool XML files:** 5\n"
    )
    assert count_tool_xmls_in_plan(plan) == 5
