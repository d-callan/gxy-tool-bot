"""Plan generation: pre-fetch lookups + agent loop + plan formatting."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from gxy_tool_bot.agent_loop import AgentResult, ToolDefinition, run_agent_loop
from gxy_tool_bot.api_client import ApiClient
from gxy_tool_bot.config import BotConfig
from gxy_tool_bot.exemplars import fetch_exemplars
from gxy_tool_bot.lookups import (
    BiocondaInfo,
    GitHubRepoInfo,
    PublicationInfo,
    fetch_doi_metadata,
    fetch_url,
    search_bioconda,
    search_github,
    search_pubmed,
    search_web,
)

logger = logging.getLogger(__name__)

PLAN_MARKER = "<!-- gxy-tool-bot-plan -->"


@dataclass
class ToolRequest:
    tool_name: str
    description: str
    links: list[str]
    contact: str | None = None


@dataclass
class LookupContext:
    bioconda: BiocondaInfo | None
    github: GitHubRepoInfo | None
    publications: list[PublicationInfo]
    readme: str | None
    raw_urls: list[str]


def _extract_doi_from_links(links: list[str]) -> str | None:
    """Extract a DOI from a list of URLs."""
    for link in links:
        if "doi.org" in link:
            return link
    return None


def _run_lookups(request: ToolRequest) -> LookupContext:
    """Phase 1: Run all targeted web lookups."""
    logger.info("Running lookups for tool: %s", request.tool_name)

    bioconda = search_bioconda(request.tool_name)
    github = search_github(request.tool_name)
    publications: list[PublicationInfo] = []

    doi = _extract_doi_from_links(request.links)
    if doi:
        pub = fetch_doi_metadata(doi)
        if pub:
            publications = [pub]
    else:
        publications = search_pubmed(request.tool_name)

    # Fetch README if we found a GitHub repo
    readme = None
    if github:
        readme_url = f"https://raw.githubusercontent.com/{github.full_name}/main/README.md"
        try:
            readme = fetch_url(readme_url, max_bytes=4000)
        except Exception:
            try:
                readme_url = f"https://raw.githubusercontent.com/{github.full_name}/master/README.md"
                readme = fetch_url(readme_url, max_bytes=4000)
            except Exception:
                pass

    # Fetch any user-provided URLs
    raw_urls: list[str] = []
    for link in request.links:
        if "doi.org" in link:
            continue
        try:
            content = fetch_url(link, max_bytes=4000)
            raw_urls.append(f"URL: {link}\n{content}")
        except Exception as e:
            logger.warning("Failed to fetch user URL %s: %s", link, e)

    return LookupContext(
        bioconda=bioconda,
        github=github,
        publications=publications,
        readme=readme,
        raw_urls=raw_urls,
    )


def _build_tool_definitions() -> list[ToolDefinition]:
    """Build the tool function definitions available to the planner agent."""
    return [
        ToolDefinition(
            name="search_bioconda",
            description="Search bioconda for a package by name. Returns package name, version, channel, URL.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Package name to search for"}},
                "required": ["query"],
            },
            handler=lambda args: _format_bioconda(search_bioconda(args["query"])),
        ),
        ToolDefinition(
            name="fetch_doi_metadata",
            description="Fetch publication metadata for a DOI via CrossRef. Returns title, authors, year, journal.",
            parameters={
                "type": "object",
                "properties": {"doi": {"type": "string", "description": "DOI string (with or without https://doi.org/ prefix)"}},
                "required": ["doi"],
            },
            handler=lambda args: _format_publication(fetch_doi_metadata(args["doi"])),
        ),
        ToolDefinition(
            name="search_github",
            description="Search GitHub for a repository. Returns repo URL, description, stars, language, license.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
            handler=lambda args: _format_github(search_github(args["query"])),
        ),
        ToolDefinition(
            name="fetch_url",
            description="Fetch raw text content of a URL (for READMEs, docs, etc.). Truncates at 500K chars. Only text/* and application/json content types.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to fetch"}},
                "required": ["url"],
            },
            handler=lambda args: fetch_url(args["url"]),
        ),
        ToolDefinition(
            name="search_pubmed",
            description="Search PubMed for publications. Returns up to 3 results with title, authors, year, journal, DOI.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
            handler=lambda args: _format_pubmed_results(search_pubmed(args["query"])),
        ),
        ToolDefinition(
            name="search_web",
            description="General web search fallback (DuckDuckGo). Returns titles, URLs, and snippets. Use when other tools don't have what you need.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
            handler=lambda args: _format_web_results(search_web(args["query"])),
        ),
    ]


def _format_bioconda(info: BiocondaInfo | None) -> str:
    if not info:
        return "No bioconda package found."
    return json.dumps({
        "package_name": info.package_name,
        "version": info.version,
        "channel": info.channel,
        "url": info.url,
    })


def _format_github(info: GitHubRepoInfo | None) -> str:
    if not info:
        return "No GitHub repo found."
    return json.dumps({
        "full_name": info.full_name,
        "url": info.url,
        "description": info.description,
        "stars": info.stars,
        "language": info.language,
        "license": info.license,
    })


def _format_publication(pub: PublicationInfo | None) -> str:
    if not pub:
        return "No publication found for that DOI."
    return json.dumps({
        "doi": pub.doi,
        "title": pub.title,
        "authors": pub.authors,
        "year": pub.year,
        "journal": pub.journal,
        "url": pub.url,
    })


def _format_pubmed_results(pubs: list[PublicationInfo]) -> str:
    if not pubs:
        return "No PubMed results found."
    return json.dumps([{
        "doi": p.doi,
        "title": p.title,
        "authors": p.authors,
        "year": p.year,
        "journal": p.journal,
        "url": p.url,
    } for p in pubs])


def _format_web_results(results: list) -> str:
    if not results:
        return "No web search results found."
    return json.dumps([{
        "title": r.title,
        "url": r.url,
        "snippet": r.snippet,
    } for r in results])


def _build_lookup_context_text(ctx: LookupContext) -> str:
    """Format LookupContext as text for the prompt."""
    parts: list[str] = []

    if ctx.bioconda:
        parts.append(f"**Bioconda:** {ctx.bioconda.package_name} v{ctx.bioconda.version} ({ctx.bioconda.channel}) — {ctx.bioconda.url}")
    else:
        parts.append("**Bioconda:** not found")

    if ctx.github:
        parts.append(f"**GitHub:** {ctx.github.full_name} ({ctx.github.url}) — {ctx.github.description} (★{ctx.github.stars}, {ctx.github.language}, {ctx.github.license})")
    else:
        parts.append("**GitHub:** not found")

    if ctx.publications:
        for p in ctx.publications:
            parts.append(f"**Publication:** {p.title} ({', '.join(p.authors[:3])}, {p.year}) — DOI: {p.doi}")
    else:
        parts.append("**Publications:** none found")

    if ctx.readme:
        parts.append(f"**README:**\n{ctx.readme[:4000]}")

    for raw in ctx.raw_urls:
        parts.append(raw)

    return "\n\n".join(parts)


def _build_exemplar_text(exemplars: list) -> str:
    """Format exemplar XMLs as text for the prompt."""
    parts: list[str] = []
    for ex in exemplars:
        parts.append(f"### Exemplar: {ex.name}\n```xml\n{ex.tool_xml}\n```")
        if ex.macros_xml:
            parts.append(f"### Macros: {ex.name}\n```xml\n{ex.macros_xml}\n```")
    return "\n\n".join(parts)


def _load_template(name: str) -> str:
    """Load a Jinja2 template from the templates directory."""
    from jinja2 import Environment, FileSystemLoader

    templates_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(templates_dir)))
    template = env.get_template(name)
    return template


def generate_plan(
    request: ToolRequest,
    config: BotConfig,
    api_key: str,
) -> tuple[str, AgentResult]:
    """
    Full planning pipeline:
    1. Run targeted web lookups (bioconda, github, doi, pubmed).
    2. Fetch exemplar tool XMLs.
    3. Build system + user prompts (from templates).
    4. Run agent loop with lookup tool functions available.
    5. Return the plan Markdown and AgentResult.
    """
    # Phase 1: lookups
    lookup_ctx = _run_lookups(request)

    # Phase 2: fetch exemplars
    exemplars = fetch_exemplars(config.exemplars)

    # Build prompts
    system_prompt = _load_template("planner_system.txt").render()
    user_prompt = _load_template("planner_user.txt").render(
        tool_name=request.tool_name,
        description=request.description,
        links=request.links,
        contact=request.contact,
        lookup_context=_build_lookup_context_text(lookup_ctx),
        exemplars=_build_exemplar_text(exemplars),
    )

    # Check context window
    total_chars = len(system_prompt) + len(user_prompt)
    if total_chars > config.api.max_context_chars:
        logger.warning(
            "Prompt size %d exceeds max_context_chars %d — truncating exemplars",
            total_chars, config.api.max_context_chars,
        )

    # Run agent loop
    tools = _build_tool_definitions()
    with ApiClient(config.api.base_url, api_key, config.api.model) as client:
        result = run_agent_loop(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            max_iterations=config.api.max_tool_iterations,
            temperature=config.api.temperature_plan,
        )

    plan_markdown = result.content
    return plan_markdown, result


def parse_issue_body(body: str) -> ToolRequest:
    """Parse a GitHub issue body into a ToolRequest."""
    tool_name = ""
    description = ""
    contact = None

    # Try to parse structured fields from the issue body
    lines = body.strip().split("\n")
    for line in lines:
        line = line.strip()
        if line.lower().startswith("tool name:"):
            tool_name = line.split(":", 1)[1].strip()
        elif line.lower().startswith("description:"):
            description = line.split(":", 1)[1].strip()
        elif line.lower().startswith("contact:"):
            contact = line.split(":", 1)[1].strip() or None

    # Extract all URLs from the body via regex — robust against any formatting
    links = re.findall(r'https?://[^\s<>"\')]+', body)

    # Fallback: use the whole body as description if no structured fields found
    if not tool_name and not description:
        description = body[:2000]

    return ToolRequest(
        tool_name=tool_name or "unknown",
        description=description,
        links=links,
        contact=contact,
    )


def find_plan_comment(comments: list) -> str | None:
    """Find the plan comment by its hidden marker."""
    for comment in comments:
        if PLAN_MARKER in comment.body:
            return comment.body.replace(PLAN_MARKER, "").strip()
    return None
