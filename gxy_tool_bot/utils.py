"""Shared utility helpers used across multiple modules."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Environment variables whose names end in these suffixes are not passed to
# subprocesses spawned by tool calls.
_SECRET_ENV_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_CREDENTIALS")


def sanitized_env() -> dict[str, str]:
    """Copy of the process environment minus credential-looking variables."""
    return {
        k: v for k, v in os.environ.items()
        if not k.upper().endswith(_SECRET_ENV_SUFFIXES)
    }


def read_tool_files(tool_dir: Path) -> dict[str, str]:
    """Read all files from a tool directory, returning relative path -> content.

    Skips internal files like ``.tool-name``. Other dotfiles such as
    ``.agent-notes`` are included — the review flow uses them to understand
    the writer's rationale. Binary files are skipped (listed as a placeholder)
    so the bot knows they exist without getting corrupted text in its context.

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
                # Binary file — include a placeholder so the bot knows it exists
                # but doesn't try to read/write it as text.
                files[str(rel)] = f"[binary file — {f.stat().st_size} bytes — use track_file to include in PR]"
    return files
