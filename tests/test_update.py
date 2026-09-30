"""Tests for the tool-update flow: update-issue parsing, update prompts,
XML counting, and update-mode FileWriter/agent notes."""

from __future__ import annotations

import pytest

from gxy_tool_bot.address_feedback import _build_update_user_prompt, update_tool
from gxy_tool_bot.config import ApiConfig, BotConfig, ExemplarConfig
from gxy_tool_bot.generator import (
    FileWriter,
    _fallback_commit_message,
    _fallback_pr_body,
)
from gxy_tool_bot.planner import (
    count_tool_xmls_in_dir,
    generate_update_plan,
    parse_update_issue_body,
)

# ---------------------------------------------------------------------------
# parse_update_issue_body
# ---------------------------------------------------------------------------

ISSUE_FORM_BODY = """
Some preamble

### Tool directory

seqtk

### What to change

Bump to 1.5 and add the -q flag.

### Links

https://github.com/lh3/seqtk/releases/tag/v1.5

### Contact

@d-callan
"""


def test_parse_update_issue_form() -> None:
    req = parse_update_issue_body(ISSUE_FORM_BODY)
    assert req.tool_dir == "seqtk"
    assert "Bump to 1.5" in req.description
    assert req.links == ["https://github.com/lh3/seqtk/releases/tag/v1.5"]
    assert req.contact == "@d-callan"


def test_parse_update_issue_form_no_response() -> None:
    body = (
        "### Tool directory\n\nseqtk\n\n"
        "### What to change\n\n_No response._\n\n"
        "### Links\n\n_No response._\n"
    )
    req = parse_update_issue_body(body)
    assert req.tool_dir == "seqtk"
    assert req.description == ""
    assert req.links == []


def test_parse_update_tool_dir_cleaned() -> None:
    for raw in ("`tools/seqtk`", "tools/seqtk/", "`seqtk`"):
        body = f"### Tool directory\n\n{raw}\n\n### What to change\n\nfix\n"
        req = parse_update_issue_body(body)
        assert req.tool_dir == "seqtk", raw


def test_parse_update_legacy_key_value() -> None:
    body = (
        "Tool directory: seqtk\n"
        "What to change: bump the version\n"
        "Links: https://example.com/notes\n"
    )
    req = parse_update_issue_body(body)
    assert req.tool_dir == "seqtk"
    assert "bump the version" in req.description
    assert req.links == ["https://example.com/notes"]


def test_parse_update_fallback_whole_body() -> None:
    body = "Please update the seqtk wrapper to the latest upstream release."
    req = parse_update_issue_body(body)
    assert req.tool_dir == ""
    assert "seqtk" in req.description


# ---------------------------------------------------------------------------
# count_tool_xmls_in_dir
# ---------------------------------------------------------------------------

def test_count_tool_xmls_in_dir() -> None:
    assert count_tool_xmls_in_dir({"seqtk.xml": "<tool/>"}) == 1
    assert count_tool_xmls_in_dir({"a.xml": "", "b.xml": "", "macros.xml": ""}) == 2
    assert count_tool_xmls_in_dir({"data.bin": "", "readme.md": ""}) == 1  # min 1


# ---------------------------------------------------------------------------
# _build_update_user_prompt
# ---------------------------------------------------------------------------

def test_update_user_prompt_sections() -> None:
    prompt = _build_update_user_prompt(
        description="Bump to 1.5",
        links=["https://example.com"],
        plan_markdown="# Update Plan: seqtk\n\nDo the thing.",
        existing_files={"seqtk.xml": "<tool/>", "x.png": "[binary file x.png]"},
        tool_dir_name="seqtk",
    )
    assert "Bump to 1.5" in prompt
    assert "# Update Plan: seqtk" in prompt
    assert "https://example.com" in prompt
    # existing files are listed by name only (binary placeholder never inlined)
    assert "`seqtk.xml`" in prompt
    assert "`x.png`" in prompt
    assert "[binary file x.png]" not in prompt
    assert "tools/seqtk" in prompt


# ---------------------------------------------------------------------------
# update_tool guards (no API calls needed)
# ---------------------------------------------------------------------------

def _config() -> BotConfig:
    return BotConfig(
        api=ApiConfig(base_url="https://example.com", model="m"),
        exemplars=[ExemplarConfig(url="https://example.com/x.xml")],
        repo="o/r",
    )


def test_update_tool_missing_dir(tmp_path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        update_tool(
            description="x", links=[], plan_markdown="plan",
            config=_config(), api_key="k",
            src_tool_dir=tmp_path / "nope", output_dir=tmp_path / "out",
            tool_dir_name="nope",
        )


def test_update_tool_stages_files(tmp_path) -> None:
    """Staging copies the tool dir verbatim; the API call itself is stubbed."""
    src = tmp_path / "tools" / "seqtk"
    (src / "test-data").mkdir(parents=True)
    (src / "seqtk.xml").write_text("<tool/>")
    (src / "test-data" / "x.bin").write_bytes(b"\x00\x01\xff")

    out = tmp_path / "generated"
    config = _config()
    captured: dict = {}

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import gxy_tool_bot.address_feedback as af
    orig_client, orig_loop = af.ApiClient, af.run_agent_with_validation

    from gxy_tool_bot.agent_loop import AgentResult
    from gxy_tool_bot.validation import ValidationResult

    def fake_loop(**kw):
        captured["file_writer"] = kw["file_writer"]
        return (
            AgentResult(
                content="done", tool_call_trace=[], iterations=1,
                terminated_naturally=True,
            ),
            [],
            ValidationResult(valid=True, errors=[]),
            0,
        )

    af.ApiClient = _FakeClient
    af.run_agent_with_validation = fake_loop
    try:
        generated, result, validation = update_tool(
            description="bump", links=[], plan_markdown="plan",
            config=config, api_key="k",
            src_tool_dir=src, output_dir=out, tool_dir_name="seqtk",
        )
    finally:
        af.ApiClient = orig_client
        af.run_agent_with_validation = orig_loop

    # Files were copied verbatim to the output dir, bytes intact.
    assert (out / "seqtk.xml").read_text() == "<tool/>"
    assert (out / "test-data" / "x.bin").read_bytes() == b"\x00\x01\xff"
    # And tracked in the writer so they land in the PR.
    assert captured["file_writer"].files["test-data/x.bin"] == b"\x00\x01\xff"


def test_generate_update_plan_empty_dir(tmp_path) -> None:
    from gxy_tool_bot.planner import UpdateRequest
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="No tool files found"):
        generate_update_plan(
            UpdateRequest(tool_dir="empty", description="x", links=[]),
            _config(), "k", empty,
        )


# ---------------------------------------------------------------------------
# FileWriter update mode — agent notes rounds
# ---------------------------------------------------------------------------

def test_file_writer_update_mode_notes(tmp_path) -> None:
    writer = FileWriter(tmp_path, mode="update")
    assert writer.add_agent_notes({"notes": "first note"}).startswith("Notes appended")
    assert writer.add_agent_notes({"notes": "second note"}).startswith("Notes appended")
    text = (tmp_path / ".agent-notes").read_text()
    assert "## Update round 1" in text
    assert "## Update round 2" in text


# ---------------------------------------------------------------------------
# commit-message fallbacks for update mode
# ---------------------------------------------------------------------------

def test_update_commit_message_fallbacks() -> None:
    msg = _fallback_commit_message("update", "seqtk", 7)
    assert "seqtk" in msg and "#7" in msg
    body = _fallback_pr_body("update", "seqtk", 7)
    assert "seqtk" in body and "#7" in body
