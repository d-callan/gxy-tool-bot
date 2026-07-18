"""General web search fallback via DuckDuckGo HTML endpoint."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from gxy_tool_bot.retry import retry

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)
_USER_AGENT = "Mozilla/5.0 (compatible; gxy-tool-bot/0.1; +https://github.com/gxy-tool-bot)"


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def search_web(query: str, max_results: int = 5) -> list[SearchResult]:
    """
    General web search fallback using DuckDuckGo HTML endpoint:
    GET https://html.duckduckgo.com/html/?q={query}
    Parses result titles, URLs, and snippets from HTML.
    No API key required. Sends a User-Agent header to avoid being blocked.
    Best-effort: DuckDuckGo's HTML format may change without notice;
    returns empty list on parse failure.
    """
    def _do_fetch() -> list[SearchResult]:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()

        try:
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            logger.warning("search_web: failed to parse HTML: %s", e)
            return []

        results = []
        for result_div in soup.select(".result"):
            if len(results) >= max_results:
                break

            title_tag = result_div.select_one(".result__title a")
            snippet_tag = result_div.select_one(".result__snippet")

            if not title_tag:
                continue

            title = unescape(title_tag.get_text(strip=True))
            # DuckDuckGo wraps URLs in a redirect; extract the actual URL
            href = title_tag.get("href", "")
            url_match = re.search(r"uddg=([^&]+)", href)
            url = unescape(url_match.group(1)) if url_match else href

            snippet = unescape(snippet_tag.get_text(strip=True)) if snippet_tag else ""

            if title and url:
                results.append(SearchResult(title=title, url=url, snippet=snippet))

        return results

    try:
        return retry(_do_fetch)
    except Exception as e:
        logger.warning("search_web failed for '%s': %s", query, e)
        return []
