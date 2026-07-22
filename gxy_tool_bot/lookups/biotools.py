"""bio.tools registry lookup via the bio.tools API."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from gxy_tool_bot.retry import retry

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)
BIO_TOOLS_URL = "https://bio.tools"


@dataclass
class BioToolsEntry:
    biotools_id: str
    name: str
    description: str
    homepage: str | None
    tooltype: list[str]


@dataclass
class BioToolsResult:
    query: str
    total_results: int
    entries: list[BioToolsEntry]


def search_bio_tools(query: str) -> BioToolsResult | None:
    """
    Search the bio.tools registry for tools matching the query.
    GET {BIO_TOOLS_URL}/api/tool?name=<query>
    Returns matching entries with biotools ID, name, description, and tool type.
    """
    def _do_fetch() -> BioToolsResult | None:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                f"{BIO_TOOLS_URL}/api/tool",
                params={"name": query, "format": "json"},
            )
            resp.raise_for_status()
            data = resp.json()

        if not isinstance(data, dict):
            logger.warning("bio.tools returned unexpected type %s for '%s'", type(data).__name__, query)
            return BioToolsResult(query=query, total_results=0, entries=[])

        total = int(data.get("count", 0))
        if total == 0:
            return BioToolsResult(query=query, total_results=0, entries=[])

        entries: list[BioToolsEntry] = []
        for item in data.get("list", []):
            tooltype = [
                t if isinstance(t, str) else t.get("tooltype", "")
                for t in item.get("toolType", [])
                if (t if isinstance(t, str) else t.get("tooltype"))
            ]
            entries.append(BioToolsEntry(
                biotools_id=item.get("biotoolsID", ""),
                name=item.get("name", ""),
                description=item.get("description", "")[:300] if item.get("description") else "",
                homepage=item.get("homepage"),
                tooltype=tooltype,
            ))

        return BioToolsResult(query=query, total_results=total, entries=entries)

    try:
        return retry(_do_fetch)
    except Exception as e:
        logger.warning("search_bio_tools failed for '%s': %s", query, e)
        return None
