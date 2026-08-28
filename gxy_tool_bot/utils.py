"""Shared utility helpers used across multiple modules."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def read_tool_files(tool_dir: Path) -> dict[str, str]:
    """Read all files from a tool directory, returning relative path -> content.

    Skips internal files like ``.tool-name``. Other dotfiles such as
    ``.agent-notes`` are included — the review flow uses them to understand
    the writer's rationale. Binary files are decoded with errors replaced so
    they don't crash the caller.

    Used by both the feedback flow (``_collect_feedback``) and the review flow
    (``collect_review_context``) to gather existing tool files.
    """
    files: dict[str, str] = {}
    if not tool_dir.exists():
        return files
    for f in tool_dir.rglob("*"):
        if f.is_file():
            rel = f.relative_to(tool_dir)
            # Skip internal files
            if rel.name == ".tool-name":
                continue
            try:
                files[str(rel)] = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                files[str(rel)] = f.read_bytes().decode("utf-8", errors="replace")
    return files
