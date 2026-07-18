"""GitHub repository search via REST API."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from gxy_tool_bot.retry import retry

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)


@dataclass
class GitHubRepoInfo:
    full_name: str
    url: str
    description: str
    stars: int
    language: str
    license: str | None


def search_github(query: str, token: str | None = None) -> GitHubRepoInfo | None:
    """
    Search GitHub via REST API:
    GET https://api.github.com/search/repositories?q=<query>&sort=stars
    Return top result. Token optional but helps with rate limits.
    """
    if token is None:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    def _do_fetch() -> GitHubRepoInfo | None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                "https://api.github.com/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc"},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        items = data.get("items", [])
        if not items:
            return None

        top = items[0]
        license_info = top.get("license")
        return GitHubRepoInfo(
            full_name=top.get("full_name", ""),
            url=top.get("html_url", ""),
            description=top.get("description", ""),
            stars=top.get("stargazers_count", 0),
            language=top.get("language", ""),
            license=license_info.get("spdx_id") if license_info else None,
        )

    try:
        return retry(_do_fetch)
    except Exception as e:
        logger.warning("search_github failed for '%s': %s", query, e)
        return None
