"""Tests for SSRF protection in fetch_url — security-critical, no HTTP mocking needed."""

import pytest

from gxy_tool_bot.lookups.fetch import _validate_url


def test_ssrf_blocks_private_ip() -> None:
    with pytest.raises(ValueError, match="private IP"):
        _validate_url("http://10.0.0.1/secret")


def test_ssrf_blocks_localhost() -> None:
    with pytest.raises(ValueError, match="localhost"):
        _validate_url("http://localhost:8080/secret")


def test_ssrf_blocks_127() -> None:
    with pytest.raises(ValueError, match="private IP"):
        _validate_url("http://127.0.0.1:8080/secret")


def test_ssrf_blocks_192_168() -> None:
    with pytest.raises(ValueError, match="private IP"):
        _validate_url("http://192.168.1.1/secret")


def test_ssrf_blocks_169_254() -> None:
    with pytest.raises(ValueError, match="private IP"):
        _validate_url("http://169.254.169.254/latest/meta-data/")


def test_ssrf_blocks_non_http_scheme() -> None:
    with pytest.raises(ValueError, match="scheme"):
        _validate_url("file:///etc/passwd")


def test_ssrf_allows_public_url() -> None:
    # Should not raise — github.com resolves to a public IP
    _validate_url("https://raw.githubusercontent.com/foo/bar/main/README.md")
