"""The few git operations the CLI needs (the CI checkout must include the base commit)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChangedFile:
    status: str  # "A", "M" or "D" (renames are reported as D + A)
    path: str


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def changed_files(base: str, cwd: Path | None = None) -> list[ChangedFile]:
    """Files changed on this branch since it diverged from `base`."""
    try:
        out = _git("diff", "--name-status", "--no-renames", f"{base}...HEAD", cwd=cwd)
    except GitError as err:
        raise GitError(
            f"cannot diff against '{base}': {err}. In GitHub Actions, check out with `fetch-depth: 0` so the base commit is available."
        ) from err
    files = []
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        if status and path:
            files.append(ChangedFile(status[0], path))
    return files


def show(ref: str, path: str, cwd: Path | None = None) -> str | None:
    """File contents at `ref`, or None if it did not exist there."""
    proc = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.stdout if proc.returncode == 0 else None


def files_at(ref: str, cwd: Path | None = None) -> list[str]:
    base = _git("merge-base", ref, "HEAD", cwd=cwd).strip()
    return _git("ls-tree", "-r", "--name-only", base, cwd=cwd).splitlines()


def merge_base(ref: str, cwd: Path | None = None) -> str:
    return _git("merge-base", ref, "HEAD", cwd=cwd).strip()


def head_sha(cwd: Path | None = None) -> str:
    return _git("rev-parse", "HEAD", cwd=cwd).strip()
