"""Tool generation: agent loop with write_file tool + validation."""

from __future__ import annotations

import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from gxy_tool_bot.agent_loop import AgentResult, ToolDefinition, run_agent_loop
from gxy_tool_bot.api_client import ApiClient
from gxy_tool_bot.config import BotConfig
from gxy_tool_bot.exemplars import fetch_exemplars
from gxy_tool_bot.lookups.biotools import search_bio_tools
from gxy_tool_bot.lookups.fetch import download_file, fetch_url
from gxy_tool_bot.lookups.github import search_github
from gxy_tool_bot.lookups.web import search_web

logger = logging.getLogger(__name__)


@dataclass
class GeneratedFile:
    path: str
    content: bytes


@dataclass
class GeneratedTool:
    files: list[GeneratedFile]
    summary: str
    tool_dir: str | None = None
    give_up_reason: str | None = None


class FileWriter:
    """Handles write_file tool calls, collecting files into a dict."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.files: dict[str, bytes] = {}
        self.tool_dir: str | None = None
        self.give_up_reason: str | None = None

    def give_up(self, args: dict) -> str:
        reason = args.get("reason", "")
        if not reason:
            return "Error: reason is required"
        self.give_up_reason = reason
        logger.info("Agent gave up: %s", reason)
        return f"Gave up: {reason}"

    def set_tool_dir(self, args: dict) -> str:
        name = args.get("name", "")
        if not name:
            return "Error: name is required"
        cleaned = re.sub(r'[^a-z0-9_-]+', '_', name.lower()).strip('_')
        if not cleaned:
            return f"Error: '{name}' is not a valid directory name"
        self.tool_dir = cleaned
        logger.info("set_tool_dir: %s", cleaned)
        return f"Tool directory set to: {cleaned}"

    def read_file(self, args: dict) -> str:
        """Read a file from the output directory and return its contents."""
        path = args.get("path", "")
        if not path:
            return "Error: path is required"

        src = (self.output_dir / path).resolve()
        if not src.is_relative_to(self.output_dir.resolve()):
            return f"Error: path '{path}' is outside the output directory"
        if not src.exists() or not src.is_file():
            return f"Error: file '{path}' does not exist"

        try:
            content = src.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            content = src.read_bytes().decode("utf-8", errors="replace")

        if len(content) > 50000:
            content = content[:50000] + "\n... [truncated]\n"
        return content

    def write_file(self, args: dict) -> str:
        path = args.get("path", "")
        content = args.get("content", "")

        if not path or "/" == path or path.endswith("/"):
            return f"Error: path '{path}' is invalid — must include a filename"

        # Validate path is within output_dir
        dest = (self.output_dir / path).resolve()
        if not dest.is_relative_to(self.output_dir.resolve()):
            return f"Error: path '{path}' is outside the output directory"

        if dest == self.output_dir.resolve():
            return f"Error: path '{path}' resolves to the output directory itself — must include a filename"

        # Convert content to bytes
        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
        elif isinstance(content, bytes):
            content_bytes = content
        else:
            content_bytes = str(content).encode("utf-8")

        # Reject binary content — write_file is for text files only.
        # Binary test data should be downloaded with download_file or
        # created with compress_file (for .gz). We detect binary by
        # checking for null bytes and by attempting UTF-8 decode.
        is_binary = False
        if b"\x00" in content_bytes:
            is_binary = True
        else:
            try:
                content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                is_binary = True
        if is_binary:
            return (
                f"Error: file '{path}' appears to be binary. "
                "write_file only accepts text. For binary test data, use download_file "
                "to fetch from a URL, or use compress_file for .gz files."
            )

        # Reject files larger than 1MB — use download_file for binary test data
        max_write_bytes = 1_000_000
        if len(content_bytes) > max_write_bytes:
            return (
                f"Error: file content is {len(content_bytes)} bytes (max {max_write_bytes}). "
                "For large binary test data, use download_file instead. "
                "For large text files, create a small synthetic sample instead."
            )

        self.files[path] = content_bytes
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content_bytes)
        logger.info("write_file: %s (%d bytes)", path, len(content_bytes))
        return f"File written: {path}"

    def compress_file(self, args: dict) -> str:
        """Gzip-compress an existing file in the output directory.

        Reads the source file, writes a .gz version, and tracks both in files dict.
        The source file must already exist (written via write_file or download_file).
        """
        path = args.get("path", "")
        if not path:
            return "Error: path is required"

        src = (self.output_dir / path).resolve()
        if not src.is_relative_to(self.output_dir.resolve()):
            return f"Error: path '{path}' is outside the output directory"
        if not src.exists() or not src.is_file():
            return f"Error: source file '{path}' does not exist — write it first with write_file"

        # Read the source content
        content_bytes = src.read_bytes()
        max_compress_bytes = 1_000_000
        if len(content_bytes) > max_compress_bytes:
            return (
                f"Error: source file is {len(content_bytes)} bytes (max {max_compress_bytes}). "
                "Create smaller test data."
            )

        import gzip
        compressed = gzip.compress(content_bytes)

        gz_path = path + ".gz"
        gz_dest = (self.output_dir / gz_path).resolve()
        gz_dest.parent.mkdir(parents=True, exist_ok=True)
        gz_dest.write_bytes(compressed)

        self.files[gz_path] = compressed
        logger.info("compress_file: %s -> %s (%d -> %d bytes)", path, gz_path, len(content_bytes), len(compressed))
        return f"File compressed: {path} -> {gz_path} ({len(content_bytes)} -> {len(compressed)} bytes)"

    def download_file_handler(self, args: dict) -> str:
        url = args.get("url", "")
        dest_path = args.get("path", "")
        result = download_file(url, dest_path, output_dir=str(self.output_dir))
        if not result.startswith("Error:"):
            # Read the downloaded file into our files dict
            downloaded = (self.output_dir / dest_path).read_bytes()
            self.files[dest_path] = downloaded
        return result


def _build_tool_definitions(file_writer: FileWriter) -> list[ToolDefinition]:
    """Build tool function definitions for the generator agent."""
    return [
        ToolDefinition(
            name="set_tool_dir",
            description=(
                "Set the directory name for this tool or tool family. "
                "For a single tool, use the tool name (e.g. 'sdust'). "
                "For a tool family, use the family name (e.g. 'hyphy' for meme/busted/fel). "
                "Call this once before writing files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Directory name (lowercase, no spaces)"},
                },
                "required": ["name"],
            },
            handler=file_writer.set_tool_dir,
        ),
        ToolDefinition(
            name="read_file",
            description="Read the contents of a file in the output directory. Use this to inspect existing files before modifying them.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'macros.xml', 'test-data/sample.bam'"},
                },
                "required": ["path"],
            },
            handler=file_writer.read_file,
        ),
        ToolDefinition(
            name="write_file",
            description="Write a file to the output directory. Path must be relative (no path traversal). Content is the file text.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'macros.xml', 'test-data/sample.bam'"},
                    "content": {"type": "string", "description": "File content as text"},
                },
                "required": ["path", "content"],
            },
            handler=file_writer.write_file,
        ),
        ToolDefinition(
            name="compress_file",
            description=(
                "Gzip-compress an existing file in the output directory. "
                "Use this to create .gz test data files (e.g. sample.fasta.gz) from text files you've already written. "
                "The source file must already exist (written via write_file). "
                "Both the original and compressed versions will be included in the PR."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path of the file to compress, e.g. 'test-data/sample.fasta'"},
                },
                "required": ["path"],
            },
            handler=file_writer.compress_file,
        ),
        ToolDefinition(
            name="fetch_url",
            description="Fetch text content of a URL (for docs, reference). Truncates at 500K chars. Only text/* and application/json.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to fetch"}},
                "required": ["url"],
            },
            handler=lambda args: fetch_url(args["url"]),
        ),
        ToolDefinition(
            name="download_file",
            description="Download a binary file directly to the output directory (for test data like BAM, FASTQ). Max 10MB.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to download from"},
                    "path": {"type": "string", "description": "Relative path within output directory, e.g. 'test-data/sample.bam'"},
                },
                "required": ["url", "path"],
            },
            handler=file_writer.download_file_handler,
        ),
        ToolDefinition(
            name="give_up",
            description=(
                "Explicitly give up if you cannot complete the task (e.g. required test data "
                "is too large to download, a dependency is missing, or validation cannot be satisfied). "
                "Provide a clear reason explaining what you tried and what blocked you. "
                "This will stop the generation process and report your reason to the maintainers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Explanation of why you cannot complete the task"},
                },
                "required": ["reason"],
            },
            handler=file_writer.give_up,
        ),
        ToolDefinition(
            name="search_github",
            description="Search GitHub repos — useful for verifying CLI flags, checking upstream examples.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
            handler=lambda args: _format_github(search_github(args["query"])),
        ),
        ToolDefinition(
            name="search_web",
            description="General web search fallback (DuckDuckGo). Returns titles, URLs, and snippets.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
            handler=lambda args: _format_web_results(search_web(args["query"])),
        ),
        ToolDefinition(
            name="search_bio_tools",
            description=(
                "Search the bio.tools registry for a tool by name. "
                "Returns matching entries with their bio.tools ID, name, description, and tool type. "
                "Use this to find the correct bio.tools ID before adding a <xref type=\"bio.tools\"> element. "
                "If no match is found, do not add a bio.tools xref."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Tool name to search for"}},
                "required": ["query"],
            },
            handler=lambda args: _format_bio_tools_results(search_bio_tools(args["query"])),
        ),
    ]


def _format_github(info) -> str:
    if not info:
        return "No GitHub repo found."
    import json
    return json.dumps({
        "full_name": info.full_name,
        "url": info.url,
        "description": info.description,
        "stars": info.stars,
        "language": info.language,
        "license": info.license,
    })


def _format_web_results(results: list) -> str:
    if not results:
        return "No web search results found."
    import json
    return json.dumps([{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results])


def _format_bio_tools_results(result) -> str:
    if not result or result.total_results == 0:
        return "No bio.tools entries found."
    import json
    return json.dumps([
        {
            "biotools_id": e.biotools_id,
            "name": e.name,
            "description": e.description,
            "homepage": e.homepage,
            "tooltype": e.tooltype,
        }
        for e in result.entries
    ])


def _build_exemplar_text(exemplars: list) -> str:
    """Format exemplar XMLs as text for the prompt."""
    parts: list[str] = []
    for ex in exemplars:
        parts.append(f"### Exemplar: {ex.name}\n```xml\n{ex.tool_xml}\n```")
        if ex.macros_xml:
            parts.append(f"### Macros: {ex.name}\n```xml\n{ex.macros_xml}\n```")
        if ex.shed_yml:
            parts.append(f"### .shed.yml: {ex.name}\n```yaml\n{ex.shed_yml}\n```")
    return "\n\n".join(parts)


def _load_template(name: str) -> str:
    from jinja2 import Environment, FileSystemLoader

    templates_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(templates_dir)))
    return env.get_template(name)


def generate_tool(
    plan_markdown: str,
    config: BotConfig,
    api_key: str,
    output_dir: Path,
) -> tuple[GeneratedTool, AgentResult, ValidationResult]:
    """
    Full generation pipeline:
    1. Fetch exemplar tool XMLs.
    2. Build system + user prompts (from templates).
    3. Run agent loop with write_file, fetch_url, download_file, search_github, search_web tools.
    4. Validate generated files (XML well-formedness, test data refs).
    5. Return GeneratedTool with all files.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Fetch exemplars
    exemplars = fetch_exemplars(config.exemplars)

    # Build prompts
    system_prompt = _load_template("generator_system.txt").render()
    user_prompt = _load_template("generator_user.txt").render(
        plan=plan_markdown,
        exemplars=_build_exemplar_text(exemplars),
    )

    # Set up file writer and tools
    file_writer = FileWriter(output_dir)
    tools = _build_tool_definitions(file_writer)

    no_files_nudge = (
        "No files were generated in the previous attempt. The agent spent all iterations"
        " on research instead of writing files.\n\n"
        "You MUST start writing files immediately. Call `set_tool_dir` first, then call"
        " `write_file` to create the tool XML. Do NOT call search_github, search_web, or"
        " fetch_url until you have written at least the tool XML and macros.xml.\n\n"
        "The plan contains everything you need. Start writing now."
    )

    from gxy_tool_bot.validation import run_agent_with_validation, ValidationResult

    with ApiClient(config.api.base_url, api_key, config.api.model, read_timeout=config.api.read_timeout, fallback_models=config.api.fallback_models) as client:
        result, files, validation = run_agent_with_validation(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            file_writer=file_writer,
            config=config,
            no_files_nudge=no_files_nudge,
        )

    generated = GeneratedTool(
        files=files,
        summary=result.content if result.terminated_naturally else f"⚠️ Incomplete: {result.content}",
        tool_dir=file_writer.tool_dir,
        give_up_reason=file_writer.give_up_reason,
    )

    return generated, result, validation


def generate_commit_message(
    client: ApiClient,
    config: BotConfig,
    context: dict,
) -> tuple[str, str]:
    """Generate a commit message and PR description via a lightweight LLM call.

    Args:
        context: dict with keys:
            - mode: "generate" or "feedback"
            - tool_name: name of the tool
            - issue_or_pr_number: int
            - summary: brief summary of what was done (agent's final output)

    Returns:
        (commit_message, pr_body) — commit_message is a single line,
        pr_body is markdown for the PR description.
    """
    mode = context.get("mode", "generate")
    tool_name = context.get("tool_name", "unknown")
    number = context.get("issue_or_pr_number", "")
    summary = context.get("summary", "")

    if mode == "feedback":
        system_prompt = (
            "You write concise git commit messages for a Galaxy tool wrapper bot. "
            "The bot has just addressed review feedback and CI failures on an existing PR. "
            "Respond with a JSON object containing one key: "
            "\"commit_message\" (a single-line commit message, max 72 chars, summarizing what was fixed). "
            "Do not include 'Closes #N' or any issue number references — the PR is already linked to its issue. "
            "Do not include any text outside the JSON object."
        )
        user_prompt = (
            f"Tool: {tool_name}\n"
            f"PR #{number}\n"
            f"Agent summary:\n{summary}\n\n"
            "Generate a commit message."
        )
    else:
        system_prompt = (
            "You write concise git commit messages and PR descriptions for a Galaxy tool wrapper bot. "
            "The bot has just generated a new Galaxy tool wrapper (XML, macros, test data) from a user request. "
            "Respond with a JSON object containing two keys: "
            "\"commit_message\" (a single-line commit message, max 72 chars) and "
            "\"pr_body\" (a brief markdown description of the tool and what was generated, 3-6 sentences). "
            "Do not include any text outside the JSON object."
        )
        user_prompt = (
            f"Tool: {tool_name}\n"
            f"Issue #{number}\n"
            f"Agent summary:\n{summary}\n\n"
            "Generate a commit message and PR description."
        )

    max_attempts = 3
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    for attempt in range(max_attempts):
        try:
            response = client.chat(messages=messages, temperature=0.3)
            content = (response.content or "").strip()
            if not content:
                logger.warning("LLM returned empty content for commit message (attempt %d/%d)", attempt + 1, max_attempts)
                if attempt < max_attempts - 1:
                    messages.append({"role": "assistant", "content": ""})
                    messages.append({"role": "user", "content": "Your response was empty. Please respond with the JSON object as instructed."})
                    continue
                break
            parsed = json.loads(content)
            commit_message = parsed.get("commit_message", "").strip()
            if mode == "feedback":
                commit_message = re.sub(r'\s*(?:Closes|Fixes|Resolves)\s*#\d+', '', commit_message, flags=re.IGNORECASE).strip()
            pr_body = parsed.get("pr_body", "").strip()
            if not commit_message:
                logger.warning("LLM returned JSON without commit_message (attempt %d/%d)", attempt + 1, max_attempts)
                if attempt < max_attempts - 1:
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": "The \"commit_message\" field was missing or empty. Please respond again with a valid JSON object containing a non-empty \"commit_message\"."})
                    continue
                commit_message = _fallback_commit_message(mode, tool_name, number)
            if not pr_body:
                if mode != "feedback":
                    logger.warning("LLM returned JSON without pr_body (attempt %d/%d)", attempt + 1, max_attempts)
                    if attempt < max_attempts - 1:
                        messages.append({"role": "assistant", "content": content})
                        messages.append({"role": "user", "content": "The \"pr_body\" field was missing or empty. Please respond again with a valid JSON object containing both \"commit_message\" and a non-empty \"pr_body\"."})
                        continue
                    pr_body = _fallback_pr_body(mode, tool_name, number)
                else:
                    pr_body = _fallback_pr_body(mode, tool_name, number)
            return commit_message, pr_body
        except json.JSONDecodeError as e:
            logger.warning("LLM returned non-JSON for commit message (attempt %d/%d): %s", attempt + 1, max_attempts, e)
            if attempt < max_attempts - 1:
                messages.append({"role": "assistant", "content": content if 'content' in locals() else ""})
                messages.append({"role": "user", "content": f"Your response was not valid JSON: {e}. Please respond with ONLY a JSON object, no other text."})
                continue
            break
        except Exception as e:
            logger.warning("Failed to generate commit message via LLM (attempt %d/%d): %s", attempt + 1, max_attempts, e)
            break

    return _fallback_commit_message(mode, tool_name, number), _fallback_pr_body(mode, tool_name, number)


def _fallback_commit_message(mode: str, tool_name: str, number: str | int) -> str:
    if mode == "feedback":
        return f"Address feedback on PR #{number}"
    return f"Generate {tool_name} tool wrapper (issue #{number})\n\nCloses #{number}"


def _fallback_pr_body(mode: str, tool_name: str, number: str | int) -> str:
    if mode == "feedback":
        return f"Addressed review feedback and CI failures for the {tool_name} tool wrapper."
    return f"Generated by gxy-tool-bot for issue #{number}"
