"""Generic URL fetcher with SSRF protection."""

from __future__ import annotations

import ipaddress
import logging
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx

from gxy_tool_bot.retry import retry

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # treat unparseable as private (deny by default)
    for net in _PRIVATE_NETWORKS:
        if ip in net:
            return True
    return False


def _validate_url(url: str) -> None:
    """Validate URL for SSRF protection. Raises ValueError if blocked."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked: only http/https schemes allowed, got '{parsed.scheme}'")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Blocked: no hostname in URL")
    if hostname in ("localhost", "0.0.0.0"):
        raise ValueError(f"Blocked: hostname '{hostname}' is not allowed")
    # Resolve DNS and check IP
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise ValueError(f"Blocked: cannot resolve hostname '{hostname}'")
    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            raise ValueError(f"Blocked: hostname '{hostname}' resolves to private IP '{ip}'")


def fetch_url(url: str, max_bytes: int = 500_000) -> str:
    """
    Fetch raw content of a URL. Truncates at max_bytes.
    Used for READMEs, documentation pages, etc.
    SSRF protected: only http/https schemes, blocks private IP ranges
    (10.x, 172.16-31.x, 192.168.x, 127.x, 169.254.x), rejects localhost.
    """
    _validate_url(url)

    def _do_fetch() -> str:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if not (content_type.startswith("text/") or "application/json" in content_type):
                    return f"Error: unsupported content type '{content_type}' (only text/* and application/json)"

                data = b""
                for chunk in resp.iter_bytes(chunk_size=8192):
                    data += chunk
                    if len(data) >= max_bytes:
                        data = data[:max_bytes]
                        logger.warning("fetch_url truncated at %d bytes for %s", max_bytes, url)
                        break

                encoding = resp.encoding or "utf-8"
                text = data.decode(encoding, errors="replace")
                if len(data) >= max_bytes:
                    text += "\n<!-- ... truncated ... -->"
                return text

    return retry(_do_fetch)


def download_file(url: str, dest_path: str, max_bytes: int = 10_000_000, output_dir: str = ".") -> str:
    """
    Download a binary file directly to the output directory.
    Used for test data (BAM, FASTQ, etc.). Same SSRF protection as fetch_url.
    dest_path is validated to be within the output directory (no path traversal).
    If the file exceeds max_bytes, abort the download and return an error string
    to the agent (does not write a partial file).
    Returns the path the file was saved to.
    """
    _validate_url(url)

    # Validate dest_path is within output_dir
    dest = Path(output_dir) / dest_path
    resolved_dest = dest.resolve()
    resolved_output = Path(output_dir).resolve()
    if not resolved_dest.is_relative_to(resolved_output):
        return f"Error: dest_path '{dest_path}' is outside the output directory"

    def _do_download() -> str:
        resolved_dest.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()

                content_length = resp.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    return f"Error: file size {content_length} exceeds max_bytes {max_bytes}"

                data = b""
                for chunk in resp.iter_bytes(chunk_size=8192):
                    data += chunk
                    if len(data) > max_bytes:
                        return f"Error: download exceeded max_bytes {max_bytes}, aborted"

                resolved_dest.write_bytes(data)
                logger.info("Downloaded %s -> %s (%d bytes)", url, resolved_dest, len(data))
                return str(dest_path)

    return retry(_do_download)
