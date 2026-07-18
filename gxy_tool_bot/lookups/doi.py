"""DOI / publication metadata lookup via CrossRef API."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from gxy_tool_bot.retry import retry

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)


@dataclass
class PublicationInfo:
    doi: str | None
    title: str
    authors: list[str]
    year: int
    journal: str
    url: str


def fetch_doi_metadata(doi: str) -> PublicationInfo | None:
    """
    Fetch publication metadata via CrossRef API:
    GET https://api.crossref.org/works/<doi>
    Handles DOIs with or without 'https://doi.org/' prefix.
    """
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")

    def _do_fetch() -> PublicationInfo | None:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(f"https://api.crossref.org/works/{doi}")
            resp.raise_for_status()
            data = resp.json()

        work = data.get("message", {})
        title_list = work.get("title", [])
        title = title_list[0] if title_list else ""

        authors = []
        for a in work.get("author", []):
            given = a.get("given", "")
            family = a.get("family", "")
            authors.append(f"{given} {family}".strip())

        year = 0
        date_parts = work.get("published-print", {}).get("date-parts", [[]])
        if not date_parts[0]:
            date_parts = work.get("published-online", {}).get("date-parts", [[]])
        if date_parts and date_parts[0]:
            year = date_parts[0][0]

        journal = work.get("container-title", [""])[0] if work.get("container-title") else ""
        url = work.get("URL", f"https://doi.org/{doi}")

        return PublicationInfo(
            doi=doi,
            title=title,
            authors=authors,
            year=year,
            journal=journal,
            url=url,
        )

    try:
        return retry(_do_fetch)
    except Exception as e:
        logger.warning("fetch_doi_metadata failed for '%s': %s", doi, e)
        return None
