"""Tool generation: agent loop with write_file tool + validation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
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
from gxy_tool_bot.lookups.toolshed import fetch_toolshed_categories
from gxy_tool_bot.lookups.web import search_web
from gxy_tool_bot.planemo_utils import summarize_test_json
from gxy_tool_bot.utils import sanitized_env

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

    def __init__(self, output_dir: Path, mode: str = "generate"):
        self.output_dir = output_dir
        self.mode = mode
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
        """Read a file from the output directory with optional line range and pattern search."""
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
            # File is binary — don't corrupt it by decoding with errors=replace.
            # Return a clear message so the bot knows not to round-trip it through write_file.
            return (
                f"Error: file '{path}' is binary and cannot be read as text. "
                "Use `track_file` to include it in the PR without reading its contents."
            )

        lines = content.splitlines()
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        pattern = args.get("pattern")

        if start_line is not None or end_line is not None:
            start = max(1, int(start_line)) if start_line is not None else 1
            end = min(len(lines), int(end_line)) if end_line is not None else len(lines)
            if start > len(lines):
                return f"Error: start_line {start} exceeds file length ({len(lines)} lines)"
            selected = lines[start - 1:end]
            if pattern:
                try:
                    regex = re.compile(pattern)
                except re.error as e:
                    return f"Error: invalid pattern: {e}"
                matches = [
                    f"{start + i}: {line}"
                    for i, line in enumerate(selected)
                    if regex.search(line)
                ]
                if not matches:
                    return "No matches found."
                return "\n".join(matches)
            return "\n".join(f"{start + i}: {line}" for i, line in enumerate(selected))

        if pattern:
            try:
                regex = re.compile(pattern)
            except re.error as e:
                return f"Error: invalid pattern: {e}"
            matches = [
                f"{i + 1}: {line}"
                for i, line in enumerate(lines)
                if regex.search(line)
            ]
            if not matches:
                return "No matches found."
            return "\n".join(matches)

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

        # Strip XML encoding declarations — planemo/lxml crashes on Unicode
        # strings with encoding declarations (known lxml bug). UTF-8 is the
        # default, so the declaration is unnecessary.
        if path.endswith(".xml"):
            content_bytes = re.sub(
                rb'<\?xml[^>]*encoding="[^"]*"[^>]*\?>\s*',
                b'',
                content_bytes,
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

    def move_file(self, args: dict) -> str:
        """Move/rename a file within the output directory."""
        src_path = args.get("src", "")
        dest_path = args.get("dest", "")
        if not src_path or not dest_path:
            return "Error: both src and dest are required"
        if dest_path == src_path:
            return "Error: src and dest are the same"

        src = (self.output_dir / src_path).resolve()
        dest = (self.output_dir / dest_path).resolve()
        output_resolved = self.output_dir.resolve()
        if not src.is_relative_to(output_resolved):
            return f"Error: src '{src_path}' is outside the output directory"
        if not dest.is_relative_to(output_resolved):
            return f"Error: dest '{dest_path}' is outside the output directory"
        if not src.exists() or not src.is_file():
            return f"Error: source file '{src_path}' does not exist"

        content_bytes = src.read_bytes()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content_bytes)
        src.unlink()

        if src_path in self.files:
            self.files[dest_path] = self.files.pop(src_path)
        else:
            self.files[dest_path] = content_bytes

        logger.info("move_file: %s -> %s", src_path, dest_path)
        return f"File moved: {src_path} -> {dest_path}"

    def delete_file(self, args: dict) -> str:
        """Delete a file from the output directory."""
        path = args.get("path", "")
        if not path:
            return "Error: path is required"

        src = (self.output_dir / path).resolve()
        if not src.is_relative_to(self.output_dir.resolve()):
            return f"Error: path '{path}' is outside the output directory"
        if not src.exists() or not src.is_file():
            return f"Error: file '{path}' does not exist"

        src.unlink()
        self.files.pop(path, None)

        logger.info("delete_file: %s", path)
        return f"File deleted: {path}"

    def planemo_lint(self, args: dict) -> str:
        """Run planemo lint on a file or directory within the output directory."""
        path = args.get("path", "")
        if not path:
            return "Error: path is required"

        target = (self.output_dir / path).resolve()
        if not target.is_relative_to(self.output_dir.resolve()):
            return f"Error: path '{path}' is outside the output directory"
        if not target.exists():
            return f"Error: path '{path}' does not exist"

        try:
            result = subprocess.run(
                ["planemo", "lint", str(target)],
                capture_output=True, text=True, timeout=120,
                env=sanitized_env(),
            )
            output = result.stdout + result.stderr
            if len(output) > 10000:
                output = output[:10000] + "\n... [truncated]\n"
            logger.info("planemo_lint: %s (exit %d)", path, result.returncode)
            return output or "No output from planemo lint."
        except subprocess.TimeoutExpired:
            return "Error: planemo lint timed out after 120s"
        except FileNotFoundError:
            return "Error: planemo is not installed"
        except Exception as e:
            return f"Error: {e}"

    def planemo_test(self, args: dict) -> str:
        """Run planemo test on a tool XML or directory within the output directory."""
        path = args.get("path", "")
        if not path:
            return "Error: path is required"

        target = (self.output_dir / path).resolve()
        if not target.is_relative_to(self.output_dir.resolve()):
            return f"Error: path '{path}' is outside the output directory"
        if not target.exists():
            return f"Error: path '{path}' does not exist"

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            json_path = tmp.name

        try:
            result = subprocess.run(
                ["planemo", "test", "--test_output_json", json_path, str(target)],
                capture_output=True, text=True, timeout=300,
                env=sanitized_env(),
            )
            try:
                with open(json_path) as f:
                    raw = f.read()
                output = summarize_test_json(raw)
            except (FileNotFoundError, OSError):
                output = result.stdout + result.stderr
            if len(output) > 10000:
                output = output[:10000] + "\n... [truncated]\n"
            logger.info("planemo_test: %s (exit %d)", path, result.returncode)
            return output or "No output from planemo test."
        except subprocess.TimeoutExpired:
            return "Error: planemo test timed out after 300s"
        except FileNotFoundError:
            return "Error: planemo is not installed"
        except Exception as e:
            return f"Error: {e}"
        finally:
            try:
                os.unlink(json_path)
            except OSError:
                pass

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

    def run_in_conda(self, args: dict) -> str:
        """Install conda packages from bioconda/conda-forge and run a command.

        Creates a cached conda environment (keyed by sorted package specs) in the
        system temp directory, then runs the command with the env's bin/ on PATH.
        The command runs in the output directory so test data is accessible.

        Only bioconda packages are supported — tools available only as
        Docker/Singularity images (not in bioconda) cannot be test-run this way.
        Use search_bioconda to verify a package exists before calling this.
        """
        packages = args.get("packages", [])
        command = args.get("command", "")
        timeout = min(args.get("timeout", 120), 300)

        if not packages or not isinstance(packages, list):
            return "Error: packages (list of conda specs) is required"
        if not command:
            return "Error: command is required"

        # Pick the fastest available conda frontend
        micromamba = shutil.which("micromamba")
        conda = shutil.which("conda")
        if not micromamba and not conda:
            return "Error: neither micromamba nor conda is installed"

        # Cached env path keyed by sorted package specs
        env_key = hashlib.sha256(
            "|".join(sorted(packages)).encode()
        ).hexdigest()[:16]
        env_path = Path(tempfile.gettempdir()) / f"gxy_conda_{env_key}"

        # Create env if it doesn't exist (or is incomplete)
        if not (env_path / "bin").exists():
            if micromamba:
                create_cmd = [
                    micromamba, "create", "-y", "-p", str(env_path),
                    "-c", "bioconda", "-c", "conda-forge",
                    *packages,
                ]
            else:
                create_cmd = [
                    conda, "create", "-y", "-p", str(env_path),
                    "-c", "bioconda", "-c", "conda-forge",
                    *packages,
                ]
            try:
                result = subprocess.run(
                    create_cmd, capture_output=True, text=True, timeout=300,
                    env=sanitized_env(),
                )
                if result.returncode != 0:
                    err = (result.stderr + result.stdout)[-2000:]
                    return f"Error creating conda env: {err}"
            except subprocess.TimeoutExpired:
                return "Error: conda env creation timed out after 300s"
            except Exception as e:
                return f"Error creating conda env: {e}"

        # Run the command with the env's bin/ prepended to PATH.
        # The command runs in the output directory so test data is accessible.
        # Any files the command creates (e.g. output samples, logs) will remain
        # on disk — the bot must clean them up with delete_file after inspecting
        # them. The validation loop checks for stray files as a safety net.
        env = sanitized_env()
        env["PATH"] = str(env_path / "bin") + os.pathsep + env.get("PATH", "")

        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=str(self.output_dir), env=env,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            output += f"\n[exit code: {result.returncode}]"
            if len(output) > 10000:
                output = output[:10000] + "\n... [truncated]\n"
            logger.info("run_in_conda: %s (exit %d)", command[:80], result.returncode)
            return output
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout}s"
        except Exception as e:
            return f"Error: {e}"

    def track_file(self, args: dict) -> str:
        """Track a file already on disk (e.g. produced by run_in_conda) as a generated file.

        This is the only way to include binary files in the PR — write_file rejects
        binary content, and round-tripping through read_file corrupts non-UTF-8 bytes.
        """
        path = args.get("path", "")
        if not path:
            return "Error: path is required"
        dest = (self.output_dir / path).resolve()
        # Prevent path traversal
        try:
            dest.relative_to(self.output_dir.resolve())
        except ValueError:
            return "Error: path must be within the tool directory"
        if not dest.exists():
            return f"Error: file '{path}' does not exist on disk"
        content = dest.read_bytes()
        self.files[path] = content
        logger.info("Tracked file %s (%d bytes)", path, len(content))
        return f"Tracked '{path}' ({len(content)} bytes)"

    def download_file_handler(self, args: dict) -> str:
        url = args.get("url", "")
        dest_path = args.get("path", "")
        result = download_file(url, dest_path, output_dir=str(self.output_dir))
        if not result.startswith("Error:"):
            # Read the downloaded file into our files dict
            downloaded = (self.output_dir / dest_path).read_bytes()
            self.files[dest_path] = downloaded
        return result

    def add_agent_notes(self, args: dict) -> str:
        """Append concise notes to .agent-notes (never removes existing content).

        In generate mode, notes go into a '## Generation notes' section (created
        on first call, appended on subsequent calls). In feedback mode, each call
        appends a new '## Feedback round N' section, incrementing N.
        """
        notes = args.get("notes", "")
        if not notes.strip():
            return "Error: notes is required"

        notes_path = ".agent-notes"
        dest = (self.output_dir / notes_path).resolve()
        existing = b""
        if dest.exists():
            try:
                existing = dest.read_bytes()
            except OSError:
                existing = b""
        existing_text = existing.decode("utf-8", errors="replace") if existing else ""

        if self.mode == "feedback":
            # Count existing feedback round sections to pick the next number.
            rounds = re.findall(r"^## Feedback round (\d+)", existing_text, re.MULTILINE)
            next_round = max((int(r) for r in rounds), default=0) + 1
            section = f"## Feedback round {next_round}\n\n{notes.strip()}\n"
            if existing_text:
                new_text = existing_text.rstrip() + "\n\n" + section
            else:
                new_text = f"# Agent Notes\n\n{section}"
        else:
            # Generate mode: create or append to the generation section.
            if "## Generation notes" in existing_text:
                new_text = existing_text.rstrip() + f"\n{notes.strip()}\n"
            else:
                new_text = f"# Agent Notes\n\n## Generation notes\n\n{notes.strip()}\n"

        content_bytes = new_text.encode("utf-8")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content_bytes)
        self.files[notes_path] = content_bytes
        logger.info("add_agent_notes: appended %d chars (mode=%s)", len(notes), self.mode)
        return f"Notes appended to {notes_path}"


def _build_tool_definitions(file_writer: FileWriter, config: BotConfig | None = None) -> list[ToolDefinition]:
    """Build tool function definitions for the generator agent."""
    tools = [
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
            description=(
                "Read the contents of a file in the output directory. "
                "Use this to inspect existing files before modifying them. "
                "Supports optional line range (start_line, end_line) and pattern search (regex). "
                "If pattern is given, returns only matching lines with line numbers. "
                "If start_line/end_line are given (without pattern), returns that slice with line numbers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'macros.xml', 'test-data/sample.bam'"},
                    "start_line": {"type": "integer", "description": "Start reading from this line (1-indexed). Optional."},
                    "end_line": {"type": "integer", "description": "Stop reading at this line (inclusive). Optional."},
                    "pattern": {"type": "string", "description": "Regex pattern to search for. Returns matching lines with line numbers. Optional."},
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
            name="move_file",
            description="Move or rename a file within the output directory. Use this to fix incorrectly named files.",
            parameters={
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "Current relative path"},
                    "dest": {"type": "string", "description": "New relative path"},
                },
                "required": ["src", "dest"],
            },
            handler=file_writer.move_file,
        ),
        ToolDefinition(
            name="delete_file",
            description="Delete a file from the output directory. Use this to remove incorrectly named or obsolete files.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path of file to delete"},
                },
                "required": ["path"],
            },
            handler=file_writer.delete_file,
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
        ToolDefinition(
            name="fetch_toolshed_categories",
            description=(
                "Fetch the list of valid Tool Shed category names from the Tool Shed API. "
                "Use this when writing or fixing .shed.yml to ensure the categories field "
                "only contains valid category names. Invalid categories cause shed lint "
                "warnings which fail CI. Call this before writing .shed.yml if you are "
                "unsure which categories are valid."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=lambda args: _format_toolshed_categories(fetch_toolshed_categories()),
        ),
    ]

    if config and config.agent_notes:
        tools.append(ToolDefinition(
            name="add_agent_notes",
            description=(
                "Append concise notes to .agent-notes for human reviewers. "
                "Focus ONLY on things not obvious from the other files: rationale for "
                "non-obvious decisions, unexpected issues that arose, trade-offs made. "
                "Keep it brief and human-readable. This file is append-only — existing "
                "content is never removed. "
                "Write notes incrementally as you discover things worth noting — do not "
                "wait until the end, as you may run out of iterations. Call this each "
                "time you learn something a future reviewer or feedback round would "
                "benefit from knowing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "notes": {"type": "string", "description": "Concise notes for reviewers"},
                },
                "required": ["notes"],
            },
            handler=file_writer.add_agent_notes,
        ))

    if shutil.which("planemo"):
        tools.append(ToolDefinition(
            name="planemo_lint",
            description=(
                "Run planemo lint on a file or directory within the output directory. "
                "Returns lint warnings and errors. Use this to catch issues before CI."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to lint (file or directory), e.g. 'my_tool.xml' or '.'"},
                },
                "required": ["path"],
            },
            handler=file_writer.planemo_lint,
            timeout=180,
        ))
        tools.append(ToolDefinition(
            name="planemo_test",
            description=(
                "Run planemo test on a tool XML or directory within the output directory. "
                "Returns a summary of test failures. Use this to verify tests pass before CI. "
                "Note: tests may take several minutes to run."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to test (tool XML or directory), e.g. 'my_tool.xml' or '.'"},
                },
                "required": ["path"],
            },
            handler=file_writer.planemo_test,
            timeout=300,
        ))

    if shutil.which("micromamba") or shutil.which("conda"):
        tools.append(ToolDefinition(
            name="run_in_conda",
            description=(
                "Install conda packages from bioconda/conda-forge and run a command. "
                "Creates a cached environment (reused across calls with the same packages). "
                "The command runs in the output directory so test data is accessible. "
                "Use this to verify CLI flags, inspect output formats, or debug test failures "
                "when documentation is unclear or planemo test output is hard to interpret. "
                "Only bioconda packages are supported — tools not in bioconda cannot be test-run. "
                "Use search_bioconda first to verify a package exists. "
                "Use sparingly: the plan and exemplars should have most of what you need. "
                "**Important:** If the command creates any files in the output directory "
                "(e.g. output samples, logs), you MUST clean them up with delete_file after "
                "inspecting them. Files left in the output directory will be committed to the PR."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Conda package specs, e.g. [\"samtools=1.21\"] or [\"bcftools\", \"htslib\"]",
                    },
                    "command": {
                        "type": "string",
                        "description": "Shell command to run, e.g. 'samtools --help' or 'samtools view test-data/sample.bam | head'",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Command timeout in seconds (default 120, max 300)",
                    },
                },
                "required": ["packages", "command"],
            },
            handler=file_writer.run_in_conda,
            timeout=360,
        ))
        tools.append(ToolDefinition(
            name="track_file",
            description=(
                "Track a file already on disk as a generated file so it gets included in the PR. "
                "Use this for files produced by run_in_conda that you want in the PR — especially "
                "binary files (HDF5, BAM, bgzipped) that cannot be read with read_file or written "
                "with write_file. The path must be relative to the tool directory and the file "
                "must already exist on disk."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file on disk (e.g. 'test-data/lookup_table.h5')",
                    },
                },
                "required": ["path"],
            },
            handler=file_writer.track_file,
            timeout=30,
        ))

    return tools


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


def _format_toolshed_categories(categories: list[str]) -> str:
    if not categories:
        return "Failed to fetch Tool Shed categories. Try again later or use commonly valid categories like: Sequence Analysis, Variant Analysis, Statistics, Phylogenetics, Proteomics, Transcriptomics, Metagenomics, Assembly, RNA, Genomic Interval Operations, Convert Formats, Text Manipulation, Visualization."
    return "Valid Tool Shed categories:\n" + "\n".join(f"- {c}" for c in categories)


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


def _derive_tool_owner(repo: str) -> str:
    """Derive the Tool Shed owner from the repo string.

    For galaxyproject/tools-iuc, the owner is 'iuc' by convention.
    For all other repos, the owner is the GitHub repo owner (first part of owner/repo).
    """
    if repo == "galaxyproject/tools-iuc":
        return "iuc"
    return repo.split("/")[0]


def generate_tool(
    plan_markdown: str,
    config: BotConfig,
    api_key: str,
    output_dir: Path,
    max_iterations_override: int | None = None,
    max_validation_retries_override: int | None = None,
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
    tool_owner = config.tool_owner or _derive_tool_owner(config.repo or "")
    system_prompt = _load_template("generator_system.txt").render(
        tool_owner=tool_owner,
        repo=config.repo or "",
        default_branch=config.default_branch,
    )
    user_prompt = _load_template("generator_user.txt").render(
        plan=plan_markdown,
        exemplars=_build_exemplar_text(exemplars),
    )

    if config.agent_notes:
        user_prompt += (
            "\n\n---\n\n## Agent Notes\n\n"
            "Write notes incrementally as you work — call `add_agent_notes` each time you "
            "discover something worth noting (e.g. a non-obvious dependency, an upstream "
            "issue, a workaround). Do NOT wait until the end, as you may run out of "
            "iterations before you get there. These notes persist across feedback rounds "
            "and help the next round avoid repeating your dead ends."
        )

    # Set up file writer and tools
    file_writer = FileWriter(output_dir)
    tools = _build_tool_definitions(file_writer, config)

    no_files_nudge = (
        "No files were generated in the previous attempt. The agent spent all iterations"
        " on research instead of writing files.\n\n"
        "You MUST start writing files immediately. Call `set_tool_dir` first, then call"
        " `write_file` to create the tool XML. Do NOT call search_github, search_web, or"
        " fetch_url until you have written at least the tool XML and macros.xml.\n\n"
        "The plan contains everything you need. Start writing now."
    )

    from gxy_tool_bot.validation import run_agent_with_validation, ValidationResult
    from gxy_tool_bot.planner import count_tool_xmls_in_plan

    # Scale validation retry rounds by the number of tool XMLs in the plan.
    # Each round gets the full max_tool_iterations budget, so more tool XMLs
    # get more rounds (e.g. 3 rounds of 30 → 4 rounds of 30) rather than
    # marginally longer rounds (3 rounds of 30 → 3 rounds of 31).
    # validation_retries_per_extra_tool_xml=0 (default) disables scaling.
    # CLI overrides (if provided) take precedence over scaling.
    scaled_retries: int | None = None
    increment = config.api.validation_retries_per_extra_tool_xml
    if increment > 0:
        num_xmls = count_tool_xmls_in_plan(plan_markdown)
        scaled_retries = config.api.max_validation_retries + increment * max(0, num_xmls - 1)
        logger.info(
            "Scaled validation retries: %d baseline + %d * (%d tool XMLs - 1) = %d rounds of %d iterations",
            config.api.max_validation_retries, increment, num_xmls, scaled_retries, config.api.max_tool_iterations,
        )
    effective_retries_override = max_validation_retries_override if max_validation_retries_override is not None else scaled_retries

    with ApiClient(config.api.base_url, api_key, config.api.model, read_timeout=config.api.read_timeout, fallback_models=config.api.fallback_models) as client:
        result, files, validation, _validation_retries = run_agent_with_validation(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            file_writer=file_writer,
            config=config,
            no_files_nudge=no_files_nudge,
            write_tools={"write_file", "compress_file", "download_file", "track_file"},
            max_validation_retries_override=effective_retries_override,
            max_iterations_override=max_iterations_override,
        )

    # Integrated self-review: if enabled, run review on the generated files
    # and give the agent fix rounds to address any findings.
    if config.integrated_review_mode != "never" and config.max_review_fix_rounds > 0:
        from gxy_tool_bot.review import run_integrated_review
        files, result, validation, _review_result = run_integrated_review(
            tool_dir=output_dir,
            config=config,
            api_key=api_key,
            validation_passed=validation.valid,
            file_writer=file_writer,
            original_result=result,
            original_files=files,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            no_files_nudge=no_files_nudge,
            plan_markdown=plan_markdown,
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
            "Do not use '#' followed by a number — GitHub auto-links '#NNN' to issues/PRs and stray numbers create broken links. "
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
            "Do not use '#' followed by a number unless referencing the specific issue number provided — "
            "GitHub auto-links '#NNN' to issues/PRs and stray numbers create broken links. "
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
            if mode == "feedback" and re.search(r'#\d+', commit_message):
                logger.warning("LLM commit message for feedback contains #N reference (attempt %d/%d): %s", attempt + 1, max_attempts, commit_message)
                if attempt < max_attempts - 1:
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": "The commit message contains \"#N\" references (e.g. #8). These get auto-linked to wrong issues by GitHub. Please respond again with a commit message that does NOT contain any \"#\" followed by a number."})
                    continue
                commit_message = re.sub(r'\s*(?:Closes|Fixes|Resolves)\s*#\d+', '', commit_message, flags=re.IGNORECASE).strip()
                commit_message = re.sub(r'#\d+', '', commit_message).strip()
                commit_message = re.sub(r'\s{2,}', ' ', commit_message).strip()
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
