"""Bioconda package lookup via anaconda.org API."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from gxy_tool_bot.retry import retry

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)


@dataclass
class BiocondaInfo:
    package_name: str
    version: str
    channel: str
    url: str


def search_bioconda(query: str) -> BiocondaInfo | None:
    """
    Search bioconda via the anaconda.org API:
    GET https://api.anaconda.org/search?name=<query>
    Parse results, prefer bioconda channel matches.
    """
    def _do_fetch() -> BiocondaInfo | None:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                "https://api.anaconda.org/search",
                params={"name": query},
            )
            resp.raise_for_status()
            results = resp.json()

        if not results:
            return None

        # Prefer bioconda channel, sort by downloads
        bioconda_results = [r for r in results if r.get("channel_name") == "bioconda"]
        if not bioconda_results:
            bioconda_results = results

        bioconda_results.sort(key=lambda r: r.get("ndownloads", 0), reverse=True)
        top = bioconda_results[0]

        return BiocondaInfo(
            package_name=top.get("name", ""),
            version=top.get("latest_version", ""),
            channel=top.get("channel_name", ""),
            url=f"https://anaconda.org/{top.get('channel_name', 'bioconda')}/{top.get('name', '')}",
        )

    try:
        return retry(_do_fetch)
    except Exception as e:
        logger.warning("search_bioconda failed for '%s': %s", query, e)
        return None
