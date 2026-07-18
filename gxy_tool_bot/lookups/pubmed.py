"""PubMed search via NCBI E-utilities API."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from gxy_tool_bot.lookups.doi import PublicationInfo
from gxy_tool_bot.retry import retry

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)
_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def search_pubmed(query: str, max_results: int = 3) -> list[PublicationInfo]:
    """
    Search PubMed via E-utilities API:
    1. esearch.fcgi to get PMIDs
    2. esummary.fcgi to get summaries
    Returns list of PublicationInfo (DOI may be None).
    """
    def _do_fetch() -> list[PublicationInfo]:
        with httpx.Client(timeout=_TIMEOUT) as client:
            # Step 1: search for PMIDs
            resp = client.get(
                f"{_EUTILS}/esearch.fcgi",
                params={"db": "pubmed", "term": query, "retmax": max_results, "retmode": "json"},
            )
            resp.raise_for_status()
            search_data = resp.json()

            id_list = search_data.get("esearchresult", {}).get("idlist", [])
            if not id_list:
                return []

            # Step 2: fetch summaries
            resp = client.get(
                f"{_EUTILS}/esummary.fcgi",
                params={"db": "pubmed", "id": ",".join(id_list), "retmode": "json"},
            )
            resp.raise_for_status()
            summary_data = resp.json()

        results = []
        for pmid in id_list:
            doc = summary_data.get("result", {}).get(pmid, {})
            if not doc or "error" in doc:
                continue

            authors = [a.get("name", "") for a in doc.get("authors", [])]
            year = 0
            pubdate = doc.get("pubdate", "")
            if pubdate:
                try:
                    year = int(pubdate.split(" ")[0].split("/")[0])
                except ValueError:
                    pass

            # DOI may not be in PubMed summary
            doi = None
            for aid in doc.get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = aid.get("value")
                    break

            results.append(PublicationInfo(
                doi=doi,
                title=doc.get("title", ""),
                authors=authors,
                year=year,
                journal=doc.get("fulljournalname", ""),
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            ))

        return results

    try:
        return retry(_do_fetch)
    except Exception as e:
        logger.warning("search_pubmed failed for '%s': %s", query, e)
        return []
