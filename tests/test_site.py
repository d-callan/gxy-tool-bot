"""Tests for site generation."""

from __future__ import annotations

from pathlib import Path

from gxy_tool_bot.config import SiteConfig
from gxy_tool_bot.site import generate_site


def test_generate_site_creates_files(tmp_path: Path) -> None:
    config = SiteConfig(
        title="Test Site",
        repo="owner/repo",
        description="Test description",
    )
    generate_site(config, tmp_path, "test-token")

    assert (tmp_path / "index.html").exists()
    assert (tmp_path / "style.css").exists()
    assert (tmp_path / "submit.js").exists()


def test_generate_site_html_contains_title(tmp_path: Path) -> None:
    config = SiteConfig(title="My Tool Form", repo="owner/repo")
    generate_site(config, tmp_path, "token")
    html = (tmp_path / "index.html").read_text()
    assert "My Tool Form" in html


def test_generate_site_js_contains_token_and_repo(tmp_path: Path) -> None:
    config = SiteConfig(title="Test", repo="myorg/myrepo")
    generate_site(config, tmp_path, "secret-token-123")
    js = (tmp_path / "submit.js").read_text()
    assert "secret-token-123" in js
    assert "myorg/myrepo" in js


def test_generate_site_has_honeypot(tmp_path: Path) -> None:
    config = SiteConfig(title="Test", repo="owner/repo")
    generate_site(config, tmp_path, "token")
    html = (tmp_path / "index.html").read_text()
    assert "honeypot" in html
