"""What the CLI knows about the CI run (GitHub Actions env, overridable by flags)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CiContext:
    repo: str | None = None  # owner/name
    sha: str | None = None
    pr_number: int | None = None
    base_ref: str | None = None  # commit or ref the PR targets
    head_sha: str | None = None
    # GitHub withholds repository secrets from these runs, so the API key is empty.
    secrets_withheld: bool = False


def from_env(env: dict[str, str] | None = None) -> CiContext:
    env = dict(os.environ if env is None else env)
    ctx = CiContext(repo=env.get("GITHUB_REPOSITORY"), sha=env.get("GITHUB_SHA"))
    ctx.secrets_withheld = env.get("GITHUB_ACTOR") == "dependabot[bot]"
    event_path = env.get("GITHUB_EVENT_PATH")
    if event_path and Path(event_path).exists():
        try:
            event = json.loads(Path(event_path).read_text())
        except ValueError:
            event = {}
        pr = event.get("pull_request") or {}
        if pr:
            ctx.pr_number = pr.get("number")
            ctx.base_ref = (pr.get("base") or {}).get("sha")
            ctx.head_sha = (pr.get("head") or {}).get("sha")
            head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
            base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
            if head_repo and base_repo and head_repo != base_repo:
                ctx.secrets_withheld = True  # a pull request from a fork
    return ctx
