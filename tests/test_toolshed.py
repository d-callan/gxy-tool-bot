"""Tests for Tool Shed lookup logic (no HTTP, pure formatting/dataclass tests)."""

from __future__ import annotations

from gxy_tool_bot.lookups.toolshed import ToolShedRepo, ToolShedResult
from gxy_tool_bot.planner import (
    LookupContext,
    _build_lookup_context_text,
    _format_tool_shed,
)
from gxy_tool_bot.lookups import BiocondaInfo, GitHubRepoInfo


def _make_tool_shed_result() -> ToolShedResult:
    return ToolShedResult(
        query="samtools",
        total_results=2,
        repos=[
            ToolShedRepo(
                name="samtools_sort",
                owner="devteam",
                description="Sort alignments by coordinates",
                remote_repository_url="https://github.com/galaxyproject/tools-iuc/tree/main/tool_collections/samtools/samtools_sort",
                homepage_url="http://www.htslib.org/",
                times_downloaded=11500,
                deprecated=False,
            ),
            ToolShedRepo(
                name="samtools_view",
                owner="iuc",
                description="Convert between SAM, BAM, and CRAM",
                remote_repository_url="https://github.com/galaxyproject/tools-iuc/tree/main/tool_collections/samtools/samtools_view",
                homepage_url="http://www.htslib.org/",
                times_downloaded=7995,
                deprecated=False,
            ),
        ],
    )


def test_format_tool_shed_with_results() -> None:
    result = _make_tool_shed_result()
    formatted = _format_tool_shed(result)
    assert "samtools_sort" in formatted
    assert "devteam" in formatted
    assert "samtools_view" in formatted
    assert "iuc" in formatted


def test_format_tool_shed_empty() -> None:
    result = ToolShedResult(query="nonexistent", total_results=0, repos=[])
    formatted = _format_tool_shed(result)
    assert "No existing Tool Shed repositories found" in formatted


def test_format_tool_shed_none() -> None:
    formatted = _format_tool_shed(None)
    assert "No existing Tool Shed repositories found" in formatted


def test_lookup_context_includes_tool_shed() -> None:
    ctx = LookupContext(
        bioconda=None,
        github=None,
        publications=[],
        readme=None,
        raw_urls=[],
        tool_shed=_make_tool_shed_result(),
    )
    text = _build_lookup_context_text(ctx)
    assert "Existing Tool Shed wrappers" in text
    assert "samtools_sort" in text
    assert "devteam" in text
    assert "samtools_view" in text


def test_lookup_context_no_tool_shed() -> None:
    ctx = LookupContext(
        bioconda=None,
        github=None,
        publications=[],
        readme=None,
        raw_urls=[],
        tool_shed=None,
    )
    text = _build_lookup_context_text(ctx)
    assert "none found" in text


def test_lookup_context_tool_shed_truncated_to_5() -> None:
    repos = [
        ToolShedRepo(
            name=f"tool_{i}",
            owner="devteam",
            description=f"Tool number {i}",
            remote_repository_url=None,
            homepage_url=None,
            times_downloaded=i,
            deprecated=False,
        )
        for i in range(10)
    ]
    ctx = LookupContext(
        bioconda=None,
        github=None,
        publications=[],
        readme=None,
        raw_urls=[],
        tool_shed=ToolShedResult(query="test", total_results=10, repos=repos),
    )
    text = _build_lookup_context_text(ctx)
    assert "tool_0" in text
    assert "tool_4" in text
    assert "tool_5" not in text
