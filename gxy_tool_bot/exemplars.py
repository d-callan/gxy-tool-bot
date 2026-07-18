"""Fetch and cache exemplar tool XMLs from GitHub URLs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp

from gxy_tool_bot.config import ExemplarConfig
from gxy_tool_bot.lookups.fetch import fetch_url

logger = logging.getLogger(__name__)


@dataclass
class Exemplar:
    tool_xml: str
    macros_xml: str | None
    name: str


def fetch_exemplars(config: list[ExemplarConfig], max_chars: int = 15000) -> list[Exemplar]:
    """
    Fetch exemplar tool XMLs (and optional macros) from GitHub raw URLs.
    Truncates each XML to max_chars. Caches to a temp directory to avoid
    re-fetching within a single run.
    """
    cache_dir = Path(mkdtemp(prefix="gxy-exemplars-"))
    exemplars: list[Exemplar] = []

    for ec in config:
        name = ec.url.rstrip("/").split("/")[-1].replace(".xml", "")
        cache_path = cache_dir / f"{name}.xml"

        if cache_path.exists():
            tool_xml = cache_path.read_text()
        else:
            try:
                tool_xml = fetch_url(ec.url, max_bytes=max_chars)
                cache_path.write_text(tool_xml)
            except Exception as e:
                logger.warning("Failed to fetch exemplar %s: %s", ec.url, e)
                continue

        if len(tool_xml) >= max_chars:
            tool_xml = tool_xml[:max_chars] + "\n<!-- ... truncated ... -->"

        macros_xml = None
        if ec.macros:
            macros_cache = cache_dir / f"{name}_macros.xml"
            if macros_cache.exists():
                macros_xml = macros_cache.read_text()
            else:
                try:
                    macros_xml = fetch_url(ec.macros, max_bytes=max_chars)
                    macros_cache.write_text(macros_xml)
                except Exception as e:
                    logger.warning("Failed to fetch macros %s: %s", ec.macros, e)
                    macros_xml = None

            if macros_xml and len(macros_xml) >= max_chars:
                macros_xml = macros_xml[:max_chars] + "\n<!-- ... truncated ... -->"

        exemplars.append(Exemplar(tool_xml=tool_xml, macros_xml=macros_xml, name=name))

    return exemplars
