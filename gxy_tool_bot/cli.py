"""CLI entry points for gxy-tool-bot."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

import click

from gxy_tool_bot.config import load_config
from gxy_tool_bot.generator import GeneratedTool, ValidationResult, generate_tool
from gxy_tool_bot.github_client import GitHubClient
from gxy_tool_bot.planner import PLAN_MARKER, find_plan_comment, generate_plan, parse_issue_body

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.INFO
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _format_trace_block(result) -> str:
    """Format the tool call trace as a collapsible HTML details block."""
    if not result.tool_call_trace:
        return ""
    lines = ["<details><summary>Agent tool call trace</summary>", ""]
    for entry in result.tool_call_trace:
        lines.append(f"**{entry['tool']}**({json.dumps(entry['arguments'])})")
        result_str = entry["result"][:500]
        lines.append(f"```\n{result_str}\n```")
        lines.append("")
    lines.append("</details>")
    return "\n".join(lines)


@click.group()
@click.option("--verbose", is_flag=True, help="Enable debug logging")
@click.option("--quiet", is_flag=True, help="Only show warnings and errors")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, quiet: bool) -> None:
    """gxy-tool-bot: Generate Galaxy tool wrappers from user requests."""
    _setup_logging(verbose, quiet)
    ctx.ensure_object(dict)


@cli.command()
@click.option("--issue", type=int, required=True, help="GitHub issue number")
@click.option("--config", "config_path", type=click.Path(exists=True), default=".gxy-tool-bot.yml")
def plan(issue: int, config_path: str) -> None:
    """Generate a tool plan from a GitHub issue."""
    config = load_config(Path(config_path))
    api_key = os.environ.get("GXY_TOOL_BOT_API_KEY")
    if not api_key:
        click.echo("Error: GXY_TOOL_BOT_API_KEY environment variable not set", err=True)
        sys.exit(1)

    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not gh_token:
        click.echo("Error: GH_TOKEN environment variable not set", err=True)
        sys.exit(1)

    with GitHubClient(gh_token, config.repo) as gh:
        issue_data = gh.get_issue(issue)
        request = parse_issue_body(issue_data.body)

        logger.info("Planning tool: %s", request.tool_name)
        try:
            plan_md, result = generate_plan(request, config, api_key)
        except Exception as exc:
            logger.exception("Plan generation failed")
            gh.add_comment(issue, f"⚠️ Plan generation failed: {exc}\n\nAdd the `retry-plan` label to try again.")
            gh.add_label(issue, config.labels.generation_failed)
            sys.exit(2)

        # Post plan as comment with hidden marker
        comment_body = f"{PLAN_MARKER}\n{plan_md}"
        if result.tool_call_trace:
            comment_body += f"\n\n{_format_trace_block(result)}"
        gh.add_comment(issue, comment_body)

        # Add plan-ready label
        gh.add_label(issue, config.labels.plan_ready)
        logger.info("Plan posted to issue #%d", issue)

    click.echo(f"Plan posted to issue #{issue}")


@cli.command()
@click.option("--issue", type=int, required=True, help="GitHub issue number")
@click.option("--config", "config_path", type=click.Path(exists=True), default=".gxy-tool-bot.yml")
@click.option("--output", "output_dir", type=click.Path(), default="generated/")
def generate(issue: int, config_path: str, output_dir: str) -> None:
    """Generate tool files from a plan in a GitHub issue."""
    config = load_config(Path(config_path))
    api_key = os.environ.get("GXY_TOOL_BOT_API_KEY")
    if not api_key:
        click.echo("Error: GXY_TOOL_BOT_API_KEY environment variable not set", err=True)
        sys.exit(1)

    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not gh_token:
        click.echo("Error: GH_TOKEN environment variable not set", err=True)
        sys.exit(1)

    # Check allowed_maintainers if configured
    if config.allowed_maintainers:
        with GitHubClient(gh_token, config.repo) as gh:
            issue_data = gh.get_issue(issue)
            actor = issue_data.author
            if actor not in config.allowed_maintainers:
                click.echo(f"Error: user '{actor}' is not in allowed_maintainers list", err=True)
                gh.add_comment(issue, f"⚠️ Generation blocked: user '{actor}' is not in the allowed maintainers list.")
                sys.exit(1)

    with GitHubClient(gh_token, config.repo) as gh:
        issue_data = gh.get_issue(issue)
        request = parse_issue_body(issue_data.body)

        comments = gh.get_issue_comments(issue)
        plan_md = find_plan_comment(comments)
        if not plan_md:
            click.echo(f"Error: no plan comment found on issue #{issue} (looking for {PLAN_MARKER} marker)", err=True)
            sys.exit(1)

        logger.info("Generating tool from plan on issue #%d", issue)
        try:
            generated, result, validation = generate_tool(plan_md, config, api_key, Path(output_dir))
        except Exception as exc:
            logger.exception("Tool generation failed")
            gh.add_comment(issue, f"⚠️ Tool generation failed: {exc}\n\nAdd the `retry-generate` label to try again.")
            gh.add_label(issue, config.labels.generation_failed)
            sys.exit(2)

        # Write tool name to a file so the workflow knows where to place files
        tool_dir = re.sub(r'[^a-z0-9]+', '_', request.tool_name.lower()).strip('_')
        (Path(output_dir) / ".tool-name").write_text(tool_dir)

        if not validation.valid:
            error_msg = "⚠️ Generation completed but validation found errors:\n\n"
            for err in validation.errors:
                error_msg += f"- {err}\n"
            error_msg += f"\nFiles generated: {len(generated.files)}"
            gh.add_comment(issue, error_msg)
            gh.add_label(issue, config.labels.generation_failed)
            click.echo(f"Generation failed validation: {len(validation.errors)} errors", err=True)
            sys.exit(3)

        # Post summary comment
        summary = f"✅ Tool generated successfully!\n\n{generated.summary}\n\n"
        summary += f"Files generated ({len(generated.files)}):\n"
        for f in generated.files:
            summary += f"- `{f.path}` ({len(f.content)} bytes)\n"
        if result.tool_call_trace:
            summary += f"\n{_format_trace_block(result)}"
        gh.add_comment(issue, summary)

    click.echo(f"Generated {len(generated.files)} files in {output_dir}")


if __name__ == "__main__":
    cli()
