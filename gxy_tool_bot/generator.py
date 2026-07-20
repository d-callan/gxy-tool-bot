"""Tool generation: agent loop with write_file tool + validation."""

from __future__ import annotations

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


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]


class FileWriter:
    """Handles write_file tool calls, collecting files into a dict."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.files: dict[str, bytes] = {}
        self.tool_dir: str | None = None

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


def validate_generated_files(files: list[GeneratedFile]) -> ValidationResult:
    """
    Lightweight post-generation validation (no planemo):
    - XML well-formedness check on all .xml files
    - Check that test data files referenced in <tests> blocks exist
    - Check that macro tokens referenced in tool XML are defined in macros XML
    """
    errors: list[str] = []
    file_paths = {f.path for f in files}

    # Must have at least one XML file
    xml_files = [f for f in files if f.path.endswith(".xml")]
    if not xml_files:
        errors.append("No XML files were generated — the agent must produce at least a tool XML file")
        return ValidationResult(valid=False, errors=errors)

    # Parse XML files
    xml_contents: dict[str, ET.Element] = {}
    for f in files:
        if f.path.endswith(".xml"):
            try:
                root = ET.fromstring(f.content.decode("utf-8"))
                xml_contents[f.path] = root
            except ET.ParseError as e:
                errors.append(f"XML parse error in {f.path}: {e}")

    # Check test data references
    for path, root in xml_contents.items():
        tests_elem = root.find(".//tests")
        if tests_elem is None:
            continue
        for test in tests_elem.findall("test"):
            for param in test.findall("param"):
                fname = param.get("value", "")
                if fname and not fname.startswith("${"):
                    # Check if it looks like a file reference
                    if "." in fname and "/" not in fname:
                        expected = f"test-data/{fname}"
                        if expected not in file_paths:
                            errors.append(f"Test data file '{expected}' referenced in {path} but not generated")

    # Check macro token references
    macro_tokens: set[str] = set()
    for path, root in xml_contents.items():
        if "macros.xml" in path:
            for token in root.iter("token"):
                name = token.get("name")
                if name:
                    macro_tokens.add(name)
            for macro in root.iter("macro"):
                name = macro.get("name")
                if name:
                    macro_tokens.add(name)
            for xml_elem in root.iter("xml"):
                name = xml_elem.get("name")
                if name:
                    macro_tokens.add(name)

    for path, root in xml_contents.items():
        if "macros.xml" in path:
            continue
        text = root.tag
        # Search for macro imports and token usage
        raw = ET.tostring(root, encoding="unicode")
        import re
        # Check <expand> macro references
        for match in re.finditer(r'<expand\s+macro="([^"]+)"', raw):
            token_name = match.group(1)
            if token_name not in macro_tokens:
                errors.append(f"Macro/token '{token_name}' referenced in {path} but not defined in any macros.xml")

    return ValidationResult(valid=len(errors) == 0, errors=errors)


def _run_agent_with_validation(
    client: ApiClient,
    system_prompt: str,
    user_prompt: str,
    tools: list[ToolDefinition],
    file_writer: FileWriter,
    config: BotConfig,
    no_files_nudge: str | None = None,
) -> tuple[AgentResult, list[GeneratedFile], ValidationResult]:
    """
    Run the agent loop with validation retries. Shared by generate_tool and address_feedback.

    - Runs the agent, collects files from file_writer, validates them.
    - On validation failure, feeds errors back to the agent and retries up to max_validation_retries.
    - If no files were generated and no_files_nudge is provided, uses it to nudge the agent.
    - Returns (final AgentResult, files, ValidationResult).
    """
    temperature = config.api.temperature_generate
    max_iterations = config.api.max_tool_iterations
    max_validation_retries = config.api.max_validation_retries

    result = run_agent_loop(
        client=client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        tools=tools,
        max_iterations=max_iterations,
        temperature=temperature,
    )

    files = [
        GeneratedFile(path=p, content=c)
        for p, c in sorted(file_writer.files.items())
    ]
    validation = validate_generated_files(files)

    for retry in range(max_validation_retries):
        if validation.valid:
            break

        logger.warning(
            "Validation errors (attempt %d/%d): %s",
            retry + 1, max_validation_retries, validation.errors,
        )

        error_msg = (
            "The following validation errors were found in the generated files:\n\n"
            + "\n".join(f"- {e}" for e in validation.errors)
            + "\n\nTo fix macro/token errors: add the missing <xml name=\"NAME\"> or <token name=\"NAME\">"
            " definitions to macros.xml. Every <expand macro=\"NAME\"> in the tool XML must have a"
            " matching definition in macros.xml.\n\n"
            "To fix test data errors: write the missing test data files with write_file.\n\n"
            "To fix XML parse errors: rewrite the file with valid XML.\n\n"
            "Please fix these errors by rewriting the affected files with write_file."
        )

        # If no files were generated at all, the agent wasted all iterations
        # researching. Keep the conversation history but add a strong nudge
        # to start writing. On the last retry, start fresh to avoid a
        # bloated context that keeps triggering research.
        if not files and no_files_nudge:
            if retry < max_validation_retries - 1:
                result = run_agent_loop(
                    client=client,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tools=tools,
                    max_iterations=max_iterations,
                    temperature=temperature,
                    messages=result.messages + [{"role": "user", "content": no_files_nudge}],
                )
            else:
                logger.info("Starting fresh agent loop (no files after %d retries)", retry + 1)
                result = run_agent_loop(
                    client=client,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tools=tools,
                    max_iterations=max_iterations,
                    temperature=temperature,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                        {"role": "user", "content": no_files_nudge},
                    ],
                )
        else:
            result = run_agent_loop(
                client=client,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools=tools,
                max_iterations=max_iterations,
                temperature=temperature,
                messages=result.messages + [{"role": "user", "content": error_msg}],
            )

        files = [
            GeneratedFile(path=p, content=c)
            for p, c in sorted(file_writer.files.items())
        ]
        validation = validate_generated_files(files)

    if not validation.valid:
        logger.warning("Validation errors after %d retries: %s", max_validation_retries, validation.errors)

    return result, files, validation


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

    with ApiClient(config.api.base_url, api_key, config.api.model, read_timeout=config.api.read_timeout) as client:
        result, files, validation = _run_agent_with_validation(
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
    )

    return generated, result, validation
