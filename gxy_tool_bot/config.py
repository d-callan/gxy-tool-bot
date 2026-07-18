"""Configuration loading and validation for gxy-tool-bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ApiConfig:
    base_url: str
    model: str
    max_tool_iterations: int = 25
    temperature_plan: float = 0.4
    temperature_generate: float = 0.2
    max_context_chars: int = 100_000
    max_validation_retries: int = 3


@dataclass
class ExemplarConfig:
    url: str
    macros: str | None = None


@dataclass
class SiteConfig:
    title: str
    repo: str
    description: str | None = None


@dataclass
class LabelConfig:
    request: str = "tool-request"
    plan_ready: str = "plan-ready"
    ready_to_implement: str = "ready-to-implement"
    pr_opened: str = "pr-opened"
    generation_failed: str = "generation-failed"


@dataclass
class BotConfig:
    api: ApiConfig
    exemplars: list[ExemplarConfig]
    site: SiteConfig
    labels: LabelConfig = field(default_factory=LabelConfig)
    allowed_maintainers: list[str] | None = None


def load_config(path: Path) -> BotConfig:
    """Load and validate a .gxy-tool-bot.yml config file."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raise ValueError(f"Config file {path} is empty")

    api_raw = raw.get("api")
    if not api_raw:
        raise ValueError("Config missing required 'api' section")
    api = ApiConfig(
        base_url=api_raw["base_url"],
        model=api_raw["model"],
        max_tool_iterations=api_raw.get("max_tool_iterations", 25),
        temperature_plan=api_raw.get("temperature_plan", 0.4),
        temperature_generate=api_raw.get("temperature_generate", 0.2),
        max_context_chars=api_raw.get("max_context_chars", 100_000),
        max_validation_retries=api_raw.get("max_validation_retries", 3),
    )

    exemplars_raw = raw.get("exemplars", [])
    if not exemplars_raw:
        raise ValueError("Config must define at least one exemplar")
    exemplars = [
        ExemplarConfig(url=e["url"], macros=e.get("macros"))
        for e in exemplars_raw
    ]

    site_raw = raw.get("site")
    if not site_raw:
        raise ValueError("Config missing required 'site' section")
    site = SiteConfig(
        title=site_raw["title"],
        repo=site_raw["repo"],
        description=site_raw.get("description"),
    )

    labels_raw = raw.get("labels", {})
    labels = LabelConfig(
        request=labels_raw.get("request", "tool-request"),
        plan_ready=labels_raw.get("plan_ready", "plan-ready"),
        ready_to_implement=labels_raw.get("ready_to_implement", "ready-to-implement"),
        pr_opened=labels_raw.get("pr_opened", "pr-opened"),
        generation_failed=labels_raw.get("generation_failed", "generation-failed"),
    )

    allowed_maintainers = raw.get("allowed_maintainers")

    return BotConfig(
        api=api,
        exemplars=exemplars,
        site=site,
        labels=labels,
        allowed_maintainers=allowed_maintainers,
    )
