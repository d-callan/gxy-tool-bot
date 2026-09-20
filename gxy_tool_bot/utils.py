"""Shared utility helpers used across multiple modules."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Environment variables are treated as credentials when any
# underscore/dash-delimited name component matches a marker — this catches
# AWS_ACCESS_KEY_ID and DOCKER_AUTH_CONFIG while leaving ordinary config vars
# like GITHUB_SERVER_URL, PIP_INDEX_URL, and PYTHON_KEYRING_BACKEND alone.
_SECRET_ENV_COMPONENTS = {
    "TOKEN", "KEY", "APIKEY", "SECRET", "PASSWORD", "PASSWD",
    "CREDENTIAL", "CREDENTIALS", "AUTH", "DSN",
}


def _is_sensitive_env_name(name: str) -> bool:
    components = re.split(r"[_\-.]+", name.upper())
    return any(part in _SECRET_ENV_COMPONENTS for part in components)


def sanitized_env(extra_names: set[str] | None = None) -> dict[str, str]:
    """Copy of the process environment minus credential-looking variables.

    ``extra_names`` are additional variable names to drop unconditionally —
    e.g. the configured LLM API key env var, whose name need not contain a
    credential marker.
    """
    extra = {n.upper() for n in (extra_names or ())}
    return {
        k: v for k, v in os.environ.items()
        if not _is_sensitive_env_name(k) and k.upper() not in extra
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
