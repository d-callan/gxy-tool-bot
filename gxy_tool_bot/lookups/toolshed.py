"""Galaxy Tool Shed repository lookup via the Tool Shed REST API."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from gxy_tool_bot.retry import retry

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)
TOOL_SHED_URL = "https://toolshed.g2.bx.psu.edu"


@dataclass
class ToolShedRepo:
    name: str
    owner: str
    description: str
    remote_repository_url: str | None
    homepage_url: str | None
    times_downloaded: int
    deprecated: bool


@dataclass
class ToolShedResult:
    query: str
    total_results: int
    repos: list[ToolShedRepo]


def search_tool_shed(query: str) -> ToolShedResult | None:
    """
    Search the Galaxy Tool Shed for repositories matching the query.
    GET {TOOL_SHED_URL}/api/repositories?q=<query>
    Returns up to 10 matching repos with name, owner, description, and links.
    """
    def _do_fetch() -> ToolShedResult | None:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                f"{TOOL_SHED_URL}/api/repositories",
                params={"q": query},
            )
            resp.raise_for_status()
            data = resp.json()

        total = int(data.get("total_results", 0))
        if total == 0:
            return ToolShedResult(query=query, total_results=0, repos=[])

        repos: list[ToolShedRepo] = []
        for hit in data.get("hits", []):
            repo = hit.get("repository", {})
            repos.append(ToolShedRepo(
                name=repo.get("name", ""),
                owner=repo.get("repo_owner_username", ""),
                description=repo.get("description", ""),
                remote_repository_url=repo.get("remote_repository_url"),
                homepage_url=repo.get("homepage_url"),
                times_downloaded=repo.get("times_downloaded", 0),
                deprecated=repo.get("approved") is False and repo.get("times_downloaded", 0) == 0,
            ))

        return ToolShedResult(query=query, total_results=total, repos=repos)

    try:
        return retry(_do_fetch)
    except Exception as e:
        logger.warning("search_tool_shed failed for '%s': %s", query, e)
        return None
