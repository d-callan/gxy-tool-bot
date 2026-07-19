"""Tests for config loading and validation."""

from pathlib import Path

import pytest
import yaml

from gxy_tool_bot.config import load_config


def _write_config(tmp_path: Path, data: dict) -> Path:
    config_path = tmp_path / ".gxy-tool-bot.yml"
    config_path.write_text(yaml.dump(data))
    return config_path


def _valid_config() -> dict:
    return {
        "api": {
            "base_url": "https://api.example.com/v1",
            "model": "test-model",
        },
        "exemplars": [
            {"url": "https://example.com/tool.xml"},
        ],
        "repo": "owner/repo",
    }


def test_load_valid_config(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _valid_config())
    config = load_config(path)

    assert config.api.base_url == "https://api.example.com/v1"
    assert config.api.model == "test-model"
    assert config.api.max_tool_iterations == 25
    assert config.api.temperature_plan == 0.4
    assert config.api.temperature_generate == 0.2
    assert config.api.max_context_chars == 100_000
    assert config.api.api_key_env == "GXY_TOOL_BOT_API_KEY"

    assert len(config.exemplars) == 1
    assert config.exemplars[0].url == "https://example.com/tool.xml"

    assert config.repo == "owner/repo"

    assert config.labels.request == "tool-request"
    assert config.labels.generation_failed == "generation-failed"

    assert config.allowed_maintainers is None


def test_load_config_custom_api_key_env(tmp_path: Path) -> None:
    data = _valid_config()
    data["api"]["api_key_env"] = "GXY_TOOL_BOT_API_KEY_CZ"
    path = _write_config(tmp_path, data)
    config = load_config(path)

    assert config.api.api_key_env == "GXY_TOOL_BOT_API_KEY_CZ"


def test_load_config_missing_api(tmp_path: Path) -> None:
    data = _valid_config()
    del data["api"]
    path = _write_config(tmp_path, data)
    with pytest.raises(ValueError, match="api"):
        load_config(path)


def test_load_config_missing_exemplars(tmp_path: Path) -> None:
    data = _valid_config()
    del data["exemplars"]
    path = _write_config(tmp_path, data)
    with pytest.raises(ValueError, match="exemplar"):
        load_config(path)


def test_load_config_missing_repo(tmp_path: Path) -> None:
    data = _valid_config()
    del data["repo"]
    path = _write_config(tmp_path, data)
    with pytest.raises(ValueError, match="repo"):
        load_config(path)


def test_load_config_custom_labels(tmp_path: Path) -> None:
    data = _valid_config()
    data["labels"] = {
        "request": "custom-request",
        "plan_ready": "custom-plan-ready",
    }
    path = _write_config(tmp_path, data)
    config = load_config(path)
    assert config.labels.request == "custom-request"
    assert config.labels.plan_ready == "custom-plan-ready"
    # Defaults for unspecified labels
    assert config.labels.ready_to_implement == "ready-to-implement"


def test_load_config_allowed_maintainers(tmp_path: Path) -> None:
    data = _valid_config()
    data["allowed_maintainers"] = ["alice", "bob"]
    path = _write_config(tmp_path, data)
    config = load_config(path)
    assert config.allowed_maintainers == ["alice", "bob"]


def test_load_config_empty_file(tmp_path: Path) -> None:
    path = tmp_path / ".gxy-tool-bot.yml"
    path.write_text("")
    with pytest.raises(ValueError, match="empty"):
        load_config(path)
