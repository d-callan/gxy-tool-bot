"""Tests for CLI options that change where comments go."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from gxy_tool_bot.cli import cli


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "bot.yml"
    cfg.write_text(
        "repo: owner/repo\n"
        "api:\n  base_url: http://llm.invalid/v1\n  model: m\n"
        "exemplars:\n  - url: https://example.invalid/tool.xml\n"
    )
    return cfg


def _env() -> dict[str, str]:
    return {"GXY_TOOL_BOT_API_KEY": "k", "GH_TOKEN": "t"}


def test_review_comment_dir_writes_instead_of_posting(tmp_path) -> None:
    """With --comment-dir the review is written to a file for a separate job to post."""
    tool_dir = tmp_path / "tools" / "demo"
    tool_dir.mkdir(parents=True)
    out = tmp_path / "comments"
    gh = MagicMock()
    ctx = MagicMock(existing_files={"demo.xml": "<tool/>"})
    with patch("gxy_tool_bot.cli.GitHubClient") as client, \
         patch("gxy_tool_bot.review.collect_review_context", return_value=ctx), \
         patch("gxy_tool_bot.review.run_review", return_value=MagicMock(findings=[])), \
         patch("gxy_tool_bot.review.format_review_comment", return_value="## Tool Review: demo"):
        client.return_value.__enter__.return_value = gh
        result = CliRunner().invoke(cli, [
            "review", "--pr", "1", "--config", str(_config(tmp_path)),
            "--tool-dir", str(tool_dir), "--comment-dir", str(out),
        ], env=_env())

    assert result.exit_code == 0, result.output
    gh.add_comment.assert_not_called()
    assert [p.read_text() for p in sorted(out.iterdir())] == ["## Tool Review: demo"]


def test_review_comment_dir_keeps_error_comment(tmp_path) -> None:
    """Error comments go to the directory too, so the poster job can report them."""
    tool_dir = tmp_path / "tools" / "demo"
    tool_dir.mkdir(parents=True)
    out = tmp_path / "comments"
    gh = MagicMock()
    with patch("gxy_tool_bot.cli.GitHubClient") as client, \
         patch("gxy_tool_bot.review.collect_review_context", side_effect=RuntimeError("boom")):
        client.return_value.__enter__.return_value = gh
        result = CliRunner().invoke(cli, [
            "review", "--pr", "1", "--config", str(_config(tmp_path)),
            "--tool-dir", str(tool_dir), "--comment-dir", str(out),
        ], env=_env())

    assert result.exit_code == 2
    gh.add_comment.assert_not_called()
    assert [p.read_text() for p in sorted(out.iterdir())] == ["⚠️ Review failed: boom"]


def test_review_without_comment_dir_still_posts(tmp_path) -> None:
    tool_dir = tmp_path / "tools" / "demo"
    tool_dir.mkdir(parents=True)
    gh = MagicMock()
    ctx = MagicMock(existing_files={"demo.xml": "<tool/>"})
    with patch("gxy_tool_bot.cli.GitHubClient") as client, \
         patch("gxy_tool_bot.review.collect_review_context", return_value=ctx), \
         patch("gxy_tool_bot.review.run_review", return_value=MagicMock(findings=[])), \
         patch("gxy_tool_bot.review.format_review_comment", return_value="## Tool Review: demo"):
        client.return_value.__enter__.return_value = gh
        result = CliRunner().invoke(cli, [
            "review", "--pr", "1", "--config", str(_config(tmp_path)), "--tool-dir", str(tool_dir),
        ], env=_env())

    assert result.exit_code == 0, result.output
    gh.add_comment.assert_called_once_with(1, "## Tool Review: demo")


def test_address_feedback_comment_dir_keeps_comment_order(tmp_path) -> None:
    """Validation errors and the summary are written in the order they'd be posted."""
    tool_dir = tmp_path / "tools" / "demo"
    tool_dir.mkdir(parents=True)
    out = tmp_path / "comments"
    gh = MagicMock()
    generated = MagicMock(files={"demo.xml": "<tool/>"}, summary="Fixed help.", give_up_reason=None)
    validation = MagicMock(valid=False, errors=["missing help"])
    result_obj = MagicMock(tool_call_trace=[])
    with patch("gxy_tool_bot.cli.GitHubClient") as client, \
         patch("gxy_tool_bot.cli.address_feedback",
               return_value=(generated, result_obj, validation, 0)):
        client.return_value.__enter__.return_value = gh
        result = CliRunner().invoke(cli, [
            "address-feedback", "--pr", "1", "--config", str(_config(tmp_path)),
            "--tool-dir", str(tool_dir), "--comment-dir", str(out),
        ], env={**_env(), "GITHUB_WORKSPACE": str(tmp_path)})

    assert result.exit_code == 0, result.output
    gh.add_comment.assert_not_called()
    assert (tmp_path / ".validation-failed").read_text() == "missing help"
    bodies = [p.read_text() for p in sorted(out.iterdir())]
    assert len(bodies) == 2
    assert "missing help" in bodies[0]
    assert bodies[1].startswith("🔧 Addressed feedback")
