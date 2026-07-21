"""GitHub API client for issue operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from gxy_tool_bot.retry import retry

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str]
    author: str


@dataclass
class Comment:
    id: int
    body: str
    author: str
    file_path: str | None = None
    line: int | None = None


class GitHubClient:
    """Client for GitHub REST API issue operations."""

    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo
        self._client = httpx.Client(
            timeout=_TIMEOUT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        """Create an issue, return issue number."""
        def _do() -> int:
            resp = self._client.post(
                f"https://api.github.com/repos/{self.repo}/issues",
                json={"title": title, "body": body, "labels": labels},
            )
            resp.raise_for_status()
            return resp.json()["number"]
        return retry(_do)

    def add_comment(self, issue_number: int, body: str) -> None:
        """Add a comment to an issue."""
        def _do() -> None:
            resp = self._client.post(
                f"https://api.github.com/repos/{self.repo}/issues/{issue_number}/comments",
                json={"body": body},
            )
            resp.raise_for_status()
        retry(_do)

    def add_label(self, issue_number: int, label: str) -> None:
        """Add a label to an issue."""
        def _do() -> None:
            resp = self._client.post(
                f"https://api.github.com/repos/{self.repo}/issues/{issue_number}/labels",
                json={"labels": [label]},
            )
            resp.raise_for_status()
        retry(_do)

    def get_issue(self, issue_number: int) -> Issue:
        """Fetch issue details (title, body, labels)."""
        def _do() -> Issue:
            resp = self._client.get(
                f"https://api.github.com/repos/{self.repo}/issues/{issue_number}"
            )
            resp.raise_for_status()
            data = resp.json()
            return Issue(
                number=data["number"],
                title=data["title"],
                body=data.get("body", ""),
                labels=[l["name"] for l in data.get("labels", [])],
                author=data.get("user", {}).get("login", ""),
            )
        return retry(_do)

    def get_issue_comments(self, issue_number: int) -> list[Comment]:
        """Fetch all comments on an issue."""
        def _do() -> list[Comment]:
            comments: list[Comment] = []
            page = 1
            while True:
                resp = self._client.get(
                    f"https://api.github.com/repos/{self.repo}/issues/{issue_number}/comments",
                    params={"per_page": 100, "page": page},
                )
                resp.raise_for_status()
                data = resp.json()
                if not data:
                    break
                for c in data:
                    comments.append(Comment(
                        id=c["id"],
                        body=c.get("body", ""),
                        author=c.get("user", {}).get("login", ""),
                    ))
                page += 1
            return comments
        return retry(_do)

    def get_pr(self, pr_number: int) -> dict:
        """Fetch PR details including head branch and base branch."""
        def _do() -> dict:
            resp = self._client.get(
                f"https://api.github.com/repos/{self.repo}/pulls/{pr_number}"
            )
            resp.raise_for_status()
            return resp.json()
        return retry(_do)

    def get_pr_comments(self, pr_number: int) -> list[Comment]:
        """Fetch all issue-level comments on a PR (not review comments)."""
        return self.get_issue_comments(pr_number)

    def get_pr_review_comments(self, pr_number: int) -> list[Comment]:
        """Fetch review comments (inline code comments) on a PR."""
        def _do() -> list[Comment]:
            comments: list[Comment] = []
            page = 1
            while True:
                resp = self._client.get(
                    f"https://api.github.com/repos/{self.repo}/pulls/{pr_number}/comments",
                    params={"per_page": 100, "page": page},
                )
                resp.raise_for_status()
                data = resp.json()
                if not data:
                    break
                for c in data:
                    comments.append(Comment(
                        id=c["id"],
                        body=c.get("body", ""),
                        author=c.get("user", {}).get("login", ""),
                        file_path=c.get("path"),
                        line=c.get("line") or c.get("original_line"),
                    ))
                page += 1
            return comments
        return retry(_do)

    def get_pr_check_runs(self, pr_number: int) -> list[dict]:
        """Fetch check run results for a PR's head SHA."""
        def _do() -> list[dict]:
            pr = self.get_pr(pr_number)
            sha = pr["head"]["sha"]
            runs: list[dict] = []
            page = 1
            while True:
                resp = self._client.get(
                    f"https://api.github.com/repos/{self.repo}/commits/{sha}/check-runs",
                    params={"per_page": 100, "page": page},
                )
                resp.raise_for_status()
                data = resp.json()
                check_runs = data.get("check_runs", [])
                if not check_runs:
                    break
                for r in check_runs:
                    runs.append({
                        "name": r.get("name", ""),
                        "status": r.get("status", ""),
                        "conclusion": r.get("conclusion", ""),
                        "output": r.get("output", {}).get("text", ""),
                    })
                page += 1
            return runs
        return retry(_do)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
