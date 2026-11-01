"""Post results to the pull request with the workflow's GITHUB_TOKEN.

* One **sticky comment** per check kind: found again on later runs by its
  hidden markers and edited in place, so pushes don't stack up comments.
* **Line suggestions** for verified FinOps rewrites, posted as a review so
  GitHub renders ```suggestion blocks with "Commit suggestion". Each carries a
  hidden key so the same suggestion isn't posted twice.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

STICKY_MARKER = "<!-- seamwatch-sticky-comment -->"
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
COMMENT_LIMIT = 60_000  # GitHub rejects comments over 65,536 characters
MAX_SUGGESTIONS = 20  # per run, so a large PR isn't buried in review comments


def fit_comment(body: str, more: str, limit: int = COMMENT_LIMIT) -> str:
    """Cut `body` at a line boundary so it fits in a comment, closing any open
    <details> or code fence, and end it with `more` (where the full report is)."""
    if len(body) <= limit:
        return body
    budget = limit - len(more) - 200
    kept: list[str] = []
    size = 0
    for line in body.splitlines():
        if size + len(line) + 1 > budget:
            break
        kept.append(line)
        size += len(line) + 1
    text = "\n".join(kept)
    tail = []
    if sum(line.startswith(("```", "````")) for line in kept) % 2:
        tail.append("```")
    tail += ["", "</details>"] * (text.count("<details>") - text.count("</details>"))
    return "\n".join([text, *tail, "", "---", "", f"**Truncated:** {more}", ""])


def kind_marker(kind: str) -> str:
    return f"<!-- seamwatch:{kind} -->"


@dataclass
class LineSuggestion:
    path: str
    start_line: int
    end_line: int
    replacement: str
    title: str
    message: str

    @property
    def key(self) -> str:
        raw = f"{self.path}|{self.start_line}|{self.end_line}|{self.replacement}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def body(self) -> str:
        return f"**{self.title}**\n\n{self.message}\n\n```suggestion\n{self.replacement}\n```\n<!-- seamwatch-suggestion:{self.key} -->"


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str, repo: str, client: httpx.Client | None = None, api_url: str | None = None):
        self.repo = repo
        self.client = client or httpx.Client(timeout=30.0)
        self.client.base_url = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.client.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        )

    def _check(self, response: httpx.Response) -> httpx.Response:
        if response.status_code >= 400:
            raise GitHubError(
                f"GitHub API {response.request.method} {response.request.url.path} -> {response.status_code}: {response.text[:300]}"
            )
        return response

    def _paginate(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url: str | None = f"{path}?per_page=100"
        while url:
            response = self._check(self.client.get(url))
            items.extend(response.json())
            url = response.links.get("next", {}).get("url")
        return items

    # -- sticky comment ---------------------------------------------------------------

    def upsert_sticky(self, pr: int, kind: str, body: str, create: bool = True) -> str:
        """Create or update the sticky comment. Returns 'created', 'updated' or 'skipped'."""
        marker = kind_marker(kind)
        if STICKY_MARKER not in body:
            body = f"{STICKY_MARKER}\n{body}"
        body = f"{marker}\n{body}"
        existing = next(
            (c for c in self._paginate(f"/repos/{self.repo}/issues/{pr}/comments") if marker in (c.get("body") or "")),
            None,
        )
        if existing is not None:
            if existing.get("body") != body:
                self._check(self.client.patch(f"/repos/{self.repo}/issues/comments/{existing['id']}", json={"body": body}))
            return "updated"
        if not create:
            return "skipped"
        self._check(self.client.post(f"/repos/{self.repo}/issues/{pr}/comments", json={"body": body}))
        return "created"

    # -- line suggestions -------------------------------------------------------------

    def commentable_lines(self, pr: int) -> dict[str, set[int]]:
        """Right-side lines that appear in the PR diff (GitHub rejects comments elsewhere)."""
        out: dict[str, set[int]] = {}
        for f in self._paginate(f"/repos/{self.repo}/pulls/{pr}/files"):
            lines: set[int] = set()
            right = 0
            for raw in (f.get("patch") or "").splitlines():
                hunk = _HUNK.match(raw)
                if hunk:
                    right = int(hunk.group(1))
                    continue
                if raw.startswith("-"):
                    continue
                if raw.startswith(("+", " ")):
                    lines.add(right)
                    right += 1
            out[f["filename"]] = lines
        return out

    def post_suggestions(self, pr: int, head_sha: str, suggestions: list[LineSuggestion]) -> tuple[int, int]:
        """Returns (posted, skipped)."""
        if not suggestions:
            return 0, 0
        existing = "\n".join(c.get("body") or "" for c in self._paginate(f"/repos/{self.repo}/pulls/{pr}/comments"))
        commentable = self.commentable_lines(pr)
        comments, skipped = [], 0
        for s in suggestions:
            in_diff = all(line in commentable.get(s.path, set()) for line in range(s.start_line, s.end_line + 1))
            if not in_diff or f"seamwatch-suggestion:{s.key}" in existing or len(comments) >= MAX_SUGGESTIONS:
                skipped += 1
                continue
            comment: dict[str, Any] = {"path": s.path, "line": s.end_line, "side": "RIGHT", "body": s.body()}
            if s.start_line != s.end_line:
                comment.update(start_line=s.start_line, start_side="RIGHT")
            comments.append(comment)
        if comments:
            self._check(
                self.client.post(
                    f"/repos/{self.repo}/pulls/{pr}/reviews",
                    json={"commit_id": head_sha, "event": "COMMENT", "body": "", "comments": comments},
                )
            )
        return len(comments), skipped
