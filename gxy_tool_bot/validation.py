"""Post-generation validation and agent validation loop.

This module contains:
- ValidationResult: dataclass holding validation results
- validate_generated_files: checks generated tool XML/files for IUC convention compliance
- run_agent_with_validation: the agent loop with validation retries, shared by both
  the generation and feedback flows

Where to put new conventions — three tiers:
1. System prompts (templates/generator_system.txt, templates/feedback_system.txt):
   High-level guidance the agent needs to know before writing — things that can't
   be tested by inspecting files (e.g. "use galaxy_slots for threading", "make
   asserts strong and specific"). Keep prompts concise; every line costs tokens
   on every LLM call. Prefer getting the agent to write something and iterate
   rather than bogging it down with exhaustive rules upfront.

2. Validation checks (validate_generated_files in this file):
   Things we can detect by inspecting the generated files (XML structure, attribute
   patterns, missing test data, etc.). Each check should produce a clear error
   message telling the agent exactly what to fix. These run after the agent writes,
   so they don't bloat the prompt but still catch mistakes on retry.

3. Neither (let planemo CI catch it):
   Things planemo already checks explicitly (e.g. XML well-formedness, shed metadata)
   should go in neither place — the CI workflow will report these failures and the
   feedback flow will pick them up. Only add a check here or in the prompt if we see
   the bot consistently making that specific mistake, wasting tokens on retries that
   a simple upfront rule would prevent.

The goal is a concise prompt that gets the agent writing quickly, with validation
as a safety net for things that are easy to get wrong structurally.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from gxy_tool_bot.agent_loop import AgentResult, ToolDefinition, run_agent_loop
from gxy_tool_bot.api_client import ApiClient
from gxy_tool_bot.config import BotConfig
from gxy_tool_bot.generator import GeneratedFile, FileWriter

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]


def validate_generated_files(files: list[GeneratedFile]) -> ValidationResult:
    """
    Lightweight post-generation validation (no planemo):
    - XML well-formedness check on all .xml files
    - Check that test data files referenced in <tests> blocks exist
    - Check that macro tokens referenced in tool XML are defined in macros XML
    - Check that <help> sections use Markdown, not HTML
    - Check detect_errors="aggressive" on <command>
    - Check expect_num_outputs on every <test>
    - Check help format="markdown" attribute
    - Check tool ID is lowercase [a-z0-9_-]
    - Check version uses @TOOL_VERSION@ token
    - Check no Cheetah directives in <xml> macros (should be <token>)
    - Check no optional="true" with a value attribute
    - Check no display="checkboxes" on multi-select params
    - Check for default label pattern on <data> outputs (reviewers request removal)
    - Check for 'cp' in command text (should use 'mv' instead)
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
        # Check <expand> macro references
        for match in re.finditer(r'<expand\s+macro="([^"]+)"', raw):
            token_name = match.group(1)
            if token_name not in macro_tokens:
                errors.append(f"Macro/token '{token_name}' referenced in {path} but not defined in any macros.xml")

    # Check that <help> sections use Markdown, not HTML
    # Galaxy renders help as reStructuredText/Markdown — HTML tags like <p>, <br>, <div>
    # inside <help> indicate the agent used HTML instead of Markdown.
    # When ElementTree parses the tool XML, HTML tags inside <help> become child
    # elements, so we check both the tag names of children and the serialized content.
    html_tag_names = {"p", "br", "div", "span", "ul", "ol", "li", "table", "tr", "td", "th", "strong", "em", "b", "i"}
    html_tag_re = re.compile(r'<(?:p|br|div|span|h[1-6]|ul|ol|li|table|tr|td|th|a\s|strong|em|b>|i>)\b', re.IGNORECASE)
    for path, root in xml_contents.items():
        if "macros.xml" in path:
            continue
        help_elem = root.find(".//help")
        if help_elem is not None:
            # Check for HTML child elements
            has_html_children = any(child.tag.lower() in html_tag_names for child in help_elem.iter() if child is not help_elem)
            # Also check serialized content for HTML tags in text/CDATA
            help_serialized = ET.tostring(help_elem, encoding="unicode")
            if has_html_children or html_tag_re.search(help_serialized):
                errors.append(
                    f"<help> section in {path} contains HTML tags — "
                    "Galaxy help sections must use Markdown, not HTML. "
                    "Replace HTML tags with Markdown syntax (e.g. **bold**, - bullet, # heading)."
                )

    # Check IUC conventions on tool XML files (not macros.xml)
    for path, root in xml_contents.items():
        if "macros.xml" in path:
            continue

        # detect_errors="aggressive" on <command>
        command_elem = root.find(".//command")
        if command_elem is not None:
            detect_errors = command_elem.get("detect_errors", "")
            if detect_errors != "aggressive":
                errors.append(
                    f"<command> in {path} is missing detect_errors=\"aggressive\" — "
                    "required for proper error detection in IUC tools."
                )

        # expect_num_outputs on every <test>
        tests_elem = root.find(".//tests")
        if tests_elem is not None:
            for test in tests_elem.findall("test"):
                if "expect_num_outputs" not in test.attrib:
                    errors.append(
                        f"<test> in {path} is missing expect_num_outputs attribute — "
                        "required by IUC conventions."
                    )

        # help format="markdown"
        help_elem = root.find(".//help")
        if help_elem is not None:
            help_format = help_elem.get("format", "")
            if help_format != "markdown":
                errors.append(
                    f"<help> in {path} is missing format=\"markdown\" — "
                    "IUC tools should use format=\"markdown\" for help sections."
                )

        # Tool ID format: lowercase [a-z0-9_-] only
        tool_id = root.get("id", "")
        if tool_id and not re.match(r'^[a-z0-9_-]+$', tool_id):
            errors.append(
                f"Tool id '{tool_id}' in {path} contains invalid characters — "
                "must be lowercase [a-z0-9_-] only."
            )

        # Version should use @TOOL_VERSION@ token, not hardcoded
        version = root.get("version", "")
        if version and not version.startswith("@"):
            errors.append(
                f"Tool version '{version}' in {path} appears hardcoded — "
                "use @TOOL_VERSION@+galaxy@VERSION_SUFFIX@ token from macros.xml."
            )

    # Check for Cheetah directives inside <xml> macros (should be <token> instead)
    cheetah_re = re.compile(r'#(?:if|for|while|set|return)\b')
    for path, root in xml_contents.items():
        if "macros.xml" not in path:
            continue
        for xml_macro in root.iter("xml"):
            macro_name = xml_macro.get("name", "unnamed")
            macro_serialized = ET.tostring(xml_macro, encoding="unicode")
            if cheetah_re.search(macro_serialized):
                errors.append(
                    f"<xml name=\"{macro_name}\"> in {path} contains Cheetah directives "
                    "(#if/#for/etc.) — use <token> instead of <xml> for Cheetah snippets."
                )

    # Check for optional="true" with a value attribute on params
    for path, root in xml_contents.items():
        if "macros.xml" in path:
            continue
        for param in root.iter("param"):
            if param.get("optional") == "true" and param.get("value") is not None:
                param_name = param.get("name") or param.get("argument") or "unnamed"
                errors.append(
                    f"<param> '{param_name}' in {path} has both optional=\"true\" and a value — "
                    "remove optional and just use the default value."
                )

    # Check for display="checkboxes" on multi-select params
    for path, root in xml_contents.items():
        if "macros.xml" in path:
            continue
        for param in root.iter("param"):
            if param.get("display") == "checkboxes":
                param_name = param.get("name") or param.get("argument") or "unnamed"
                errors.append(
                    f"<param> '{param_name}' in {path} has display=\"checkboxes\" — "
                    "remove the display attribute; let Galaxy pick the widget."
                )

    # Check for <stdio> when detect_errors="aggressive" is present (redundant)
    for path, root in xml_contents.items():
        if "macros.xml" in path:
            continue
        command_elem = root.find(".//command")
        if command_elem is not None and command_elem.get("detect_errors") == "aggressive":
            stdio_elem = root.find(".//stdio")
            if stdio_elem is not None:
                errors.append(
                    f"<stdio> in {path} is redundant when detect_errors=\"aggressive\" is set — "
                    "remove the <stdio> element."
                )

    # Check for boolean params with truevalue="true" (should be the CLI flag)
    for path, root in xml_contents.items():
        if "macros.xml" in path:
            continue
        for param in root.iter("param"):
            if param.get("type") == "boolean" and param.get("truevalue") == "true":
                param_name = param.get("name") or param.get("argument") or "unnamed"
                errors.append(
                    f"<param> '{param_name}' in {path} is a boolean with truevalue=\"true\" — "
                    "truevalue should be the CLI flag (e.g. \"--verbose\" or \"-f\"), not \"true\"."
                )

    # Check for <output> elements in <test> blocks missing ftype
    for path, root in xml_contents.items():
        if "macros.xml" in path:
            continue
        tests_elem = root.find(".//tests")
        if tests_elem is not None:
            for test in tests_elem.findall("test"):
                for output in test.iter("output"):
                    if "ftype" not in output.attrib:
                        output_name = output.get("name", "unnamed")
                        errors.append(
                            f"<output> '{output_name}' in a <test> in {path} is missing ftype — "
                            "test outputs should specify the expected file type (e.g. ftype=\"fasta\")."
                        )

    # Check for <data> outputs that use the bare default label explicitly.
    # Labels like "${tool.name} log on ${on_string}" are fine (descriptive),
    # but the bare "${tool.name} on ${on_string}" is redundant and triggers
    # planemo's OutputsLabelDuplicated warnings.
    for path, root in xml_contents.items():
        if "macros.xml" in path:
            continue
        for data_elem in root.iter("data"):
            label = data_elem.get("label", "")
            if label and label.strip() == "${tool.name} on ${on_string}":
                data_name = data_elem.get("name", "unnamed")
                errors.append(
                    f"<data> '{data_name}' in {path} has label=\"{label}\" — "
                    "this is the Galaxy default and triggers planemo lint warnings. "
                    "Either remove the label (for the first output) or use a descriptive "
                    "label like '${{tool.name}} log on ${{on_string}}'."
                )

    # Check for 'cp' in command text — should use 'mv' instead to avoid stale copies
    for path, root in xml_contents.items():
        if "macros.xml" in path:
            continue
        command_elem = root.find(".//command")
        if command_elem is not None and command_elem.text:
            for match in re.finditer(r'\bcp\b', command_elem.text):
                errors.append(
                    f"Command in {path} uses 'cp' — use 'mv' instead when moving or renaming "
                    "output files."
                )

    return ValidationResult(valid=len(errors) == 0, errors=errors)


def run_agent_with_validation(
    client: ApiClient,
    system_prompt: str,
    user_prompt: str,
    tools: list[ToolDefinition],
    file_writer: FileWriter,
    config: BotConfig,
    no_files_nudge: str | None = None,
    write_tools: set[str] | None = None,
) -> tuple[AgentResult, list[GeneratedFile], ValidationResult]:
    """
    Run the agent loop with validation retries. Shared by generate_tool and address_feedback.

    - Runs the agent, collects files from file_writer, validates them.
    - On validation failure, feeds errors back to the agent and retries up to max_validation_retries.
    - If no write tool calls were made and no_files_nudge is provided, uses it to nudge the agent.
    - write_tools: set of tool names that count as "writing" (default: {"write_file"}).
    - Returns (final AgentResult, files, ValidationResult).
    """
    temperature = config.api.temperature_generate
    max_iterations = config.api.max_tool_iterations
    max_validation_retries = config.api.max_validation_retries
    max_context_chars = config.api.max_context_chars

    result = run_agent_loop(
        client=client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        tools=tools,
        max_iterations=max_iterations,
        temperature=temperature,
        max_context_chars=max_context_chars,
    )

    files = [
        GeneratedFile(path=p, content=c)
        for p, c in sorted(file_writer.files.items())
    ]
    validation = validate_generated_files(files)

    for retry in range(max_validation_retries):
        if validation.valid:
            break

        # If the agent explicitly gave up, don't retry — report the reason
        if file_writer.give_up_reason:
            logger.warning("Agent gave up: %s", file_writer.give_up_reason)
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

        # If no write tool calls were made, the agent wasted all iterations
        # researching. Keep the conversation history but add a strong nudge
        # to start writing. On the last retry, start fresh to avoid a
        # bloated context that keeps triggering research.
        #
        # We check the tool call trace for write_file/compress_file/download_file
        # calls rather than checking file sets, since feedback mode overwrites
        # existing files (same keys, new content).
        _wt = write_tools or {"write_file"}
        made_writes = any(tc["tool"] in _wt for tc in result.tool_call_trace)
        if not made_writes and no_files_nudge:
            if retry < max_validation_retries - 1:
                result = run_agent_loop(
                    client=client,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tools=tools,
                    max_iterations=max_iterations,
                    temperature=temperature,
                    max_context_chars=max_context_chars,
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
                    max_context_chars=max_context_chars,
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
                max_context_chars=max_context_chars,
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
