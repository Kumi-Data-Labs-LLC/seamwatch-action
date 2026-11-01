"""seamwatch CLI: `sync-dbt`, `check-ddl`, `check-sql`.

Exit codes: 0 passed (or only below `fail_on`), 1 check failed or the API
rejected the request, 2 usage / configuration / git error. When the API is
unreachable or rate limited, or the CLI itself crashes, and `fail_open` is on
(the default), the CLI warns and exits 0 so trouble on our side never blocks a
deploy.
"""

from __future__ import annotations

import json
import os
import posixpath
import sys
import traceback
from pathlib import Path
from typing import Any

import typer

from . import __version__, git
from .api import ApiError, SeamwatchApi
from .config import Config, ConfigError, is_down_migration, load_config, matches_any, migration_sort_key
from .context import CiContext, from_env
from .github_comment import MAX_SUGGESTIONS, GitHub, GitHubError, LineSuggestion, fit_comment

app = typer.Typer(add_completion=False, no_args_is_help=True, help="seamwatch: cross-repo schema guardrails and SQL FinOps checks.")

MAX_PRIOR_MIGRATION_BYTES = 5 * 1024 * 1024

ApiUrl = typer.Option(None, "--api-url", envvar="SEAMWATCH_API_URL", help="seamwatch API base URL.")
ApiKey = typer.Option(None, "--api-key", envvar="SEAMWATCH_API_KEY", help="sg_live_... key.")
ConfigPath = typer.Option(".seamwatch.yml", "--config", help="Path to .seamwatch.yml.")
Repo = typer.Option(None, "--repo", envvar="GITHUB_REPOSITORY", help="owner/name of this repository.")
Base = typer.Option(None, "--base", help="Commit or ref the PR targets (default: the PR's base, else origin/main).")
Pr = typer.Option(None, "--pr", help="Pull request number (default: from the GitHub event).")
Token = typer.Option(None, "--github-token", envvar="GITHUB_TOKEN", help="Token used to comment on the PR.")
Comment = typer.Option(True, "--comment/--no-comment", help="Post / update the sticky PR comment.")


class Exit(Exception):
    def __init__(self, code: int, message: str = ""):
        self.code, self.message = code, message


def _err(message: str) -> None:
    typer.echo(message, err=True)


# The loaded config's `fail_open`, so `main` can honour it when the CLI crashes.
_fail_open = True


def _setup(api_url: str | None, api_key: str | None, config_path: str) -> tuple[SeamwatchApi, Config, CiContext]:
    global _fail_open
    ctx = from_env()
    if not api_url:
        raise Exit(2, "Set SEAMWATCH_API_URL (or --api-url).")
    if not api_key:
        if ctx.secrets_withheld:
            raise Exit(
                0,
                "::warning title=seamwatch::skipped: GitHub does not pass repository secrets to pull requests "
                "from forks or Dependabot, so SEAMWATCH_API_KEY is empty.",
            )
        raise Exit(2, "Set SEAMWATCH_API_KEY (or --api-key).")
    try:
        config = load_config(config_path)
    except ConfigError as err:
        raise Exit(2, str(err)) from err
    _fail_open = config.fail_open
    for warning in config.warnings:
        _err(f"warning: {warning}")
    return SeamwatchApi(api_url, api_key), config, ctx


def _read_text(path: Path | str) -> str:
    """A file from the checkout. Undecodable bytes (a Latin-1 comment, say) must not stop the check."""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _read_manifest(path: Path) -> tuple[bytes, dict[str, Any]]:
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except ValueError as err:
        raise Exit(2, f"{path} is not valid JSON ({err}). Re-run `dbt compile`.") from err
    if not isinstance(data, dict):
        raise Exit(2, f"{path} is not a dbt manifest (expected a JSON object).")
    return raw, data


def _call(fn, config: Config, *args: Any) -> dict[str, Any] | None:
    try:
        return fn(*args)
    except ApiError as err:
        if err.is_outage and config.fail_open:
            print(f"::warning title=seamwatch::{err} (fail_open is on, so this check is skipped)")
            return None
        raise Exit(1, str(err)) from err


def _run_url() -> str | None:
    env = os.environ
    if env.get("GITHUB_REPOSITORY") and env.get("GITHUB_RUN_ID"):
        server = env.get("GITHUB_SERVER_URL", "https://github.com")
        return f"{server}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
    return None


REPORT_DIR = "seamwatch-report"
SUMMARY_LIMIT = 1_000_000  # GitHub drops job summaries over 1 MiB


def _publish(result: dict[str, Any]) -> None:
    """Print the comment, and write the full report to the job summary and to
    `seamwatch-report/` (report.md and report.json), which the Action uploads as an artifact."""
    typer.echo(result["markdown_comment"])
    report = result.get("markdown_report") or result["markdown_comment"]
    out = Path(os.environ.get("SEAMWATCH_REPORT_DIR", REPORT_DIR))
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.md").write_text(report, encoding="utf-8")
        (out / "report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except OSError as err:
        _err(f"warning: could not write {out}: {err}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(fit_comment(report, "the full report is in the `seamwatch-report` artifact.", SUMMARY_LIMIT) + "\n")


def _rule_options(config: Config) -> dict[str, Any]:
    """`rules:` and `ignore:` from .seamwatch.yml; omitted when unset so older APIs see the same body."""
    out: dict[str, Any] = {}
    if config.rules:
        out["rules"] = config.rules
    if config.ignore:
        out["ignore"] = config.ignore
    return out


def _exit_code(status: str, fail_on: str) -> int:
    if fail_on == "never":
        return 0
    if status == "FAILED":
        return 1
    return 1 if (status == "WARNING" and fail_on == "warning") else 0


def _base(base: str | None, ctx: CiContext) -> str:
    return base or ctx.base_ref or "origin/main"


def _github(token: str | None, repo: str | None, pr: int | None) -> GitHub | None:
    if not (token and repo and pr):
        return None
    return GitHub(token, repo)


def _comment(gh: GitHub | None, pr: int | None, kind: str, markdown: str, status: str, config: Config) -> None:
    if gh is None or pr is None:
        return
    try:
        url = _run_url()
        where = f"[this run's summary]({url})" if url else "this run's summary"
        markdown = fit_comment(
            markdown,
            f"this PR has more findings than fit in one comment. The full report is in {where} and in its `seamwatch-report` artifact.",
        )
        outcome = gh.upsert_sticky(pr, kind, markdown, create=status != "PASSED" or config.comment_on_pass)
        _err(f"PR comment {outcome}.")
    except GitHubError as err:
        print(f"::warning title=seamwatch::could not comment on the PR: {err}")


# --- sync-dbt ---------------------------------------------------------------------------


@app.command("sync-dbt")
def sync_dbt(
    manifest: Path | None = typer.Option(None, help="Compiled manifest.json (default from .seamwatch.yml)."),
    catalog: Path | None = typer.Option(None, help="catalog.json from `dbt docs generate` (optional)."),
    sha: str | None = typer.Option(None, envvar="GITHUB_SHA", help="Commit being synced."),
    dialect: str | None = typer.Option(None, help="Override the dialect detected from the manifest."),
    repo: str | None = Repo,
    api_url: str | None = ApiUrl,
    api_key: str | None = ApiKey,
    config_path: str = ConfigPath,
) -> None:
    """Upload the dbt project's lineage (run on the analytics repo's main branch)."""
    api, config, _ = _setup(api_url, api_key, config_path)
    project = Path(config.dbt_project_dir)
    manifest = manifest or project / config.manifest_path
    if catalog is None and config.catalog_path and (project / config.catalog_path).exists():
        catalog = project / config.catalog_path
    if not manifest.exists():
        raise Exit(2, f"{manifest} not found. Run `dbt compile` (and ideally `dbt docs generate`) first.")
    if not repo:
        raise Exit(2, "Set --repo (GITHUB_REPOSITORY).")
    raw, data = _read_manifest(manifest)
    _warn_if_uncompiled(data)
    result = _call(api.sync, config, raw, catalog.read_bytes() if catalog else None, repo, sha or git.head_sha(), dialect)
    if result is None:
        return
    typer.echo(
        f"Synced {result['repo_full_name']}@{result['git_commit_sha'][:12]} ({result['dialect']}): "
        f"{result['node_count']} nodes, {result['edge_count']} column edges."
    )
    for diag in result.get("diagnostics", [])[:20]:
        _err(f"{diag['level']}: {diag.get('node_uid') or ''} {diag['message']}")


def _compiled(node: dict[str, Any]) -> str | None:
    # dbt < 1.3 (manifest v4-v6) wrote `compiled_sql`; later versions write `compiled_code`.
    return node.get("compiled_code") or node.get("compiled_sql")


def _warn_if_uncompiled(manifest: dict[str, Any]) -> None:
    models = [n for n in (manifest.get("nodes") or {}).values() if n.get("resource_type") == "model"]
    if models and not any(_compiled(n) for n in models):
        _err("warning: manifest has no compiled SQL. It looks like `dbt parse` output; run `dbt compile` so lineage can be traced.")


# --- check-ddl --------------------------------------------------------------------------


@app.command("check-ddl")
def check_ddl(
    migration_file: list[Path] = typer.Option(
        [], "--migration-file", help="Explicit SQL to check (e.g. `alembic upgrade head --sql` output). Skips git discovery."
    ),
    base: str | None = Base,
    pr: int | None = Pr,
    repo: str | None = Repo,
    github_token: str | None = Token,
    comment: bool = Comment,
    api_url: str | None = ApiUrl,
    api_key: str | None = ApiKey,
    config_path: str = ConfigPath,
) -> None:
    """Check a backend PR's migrations / Prisma schema against synced dbt lineage."""
    api, config, ctx = _setup(api_url, api_key, config_path)
    pr = pr or ctx.pr_number
    payload: dict[str, Any] = {"repo_full_name": repo or "unknown", "pr_number": pr, "migration_files": []}
    if config.database != "postgres":
        payload["dialect"] = config.database
    payload.update(_rule_options(config))
    if config.source_names:
        payload["source_names"] = config.source_names
    if config.table_map:
        payload["table_map"] = config.table_map

    try:
        if migration_file:
            payload["migration_files"] = [{"file_path": str(p), "raw_sql": _read_text(p)} for p in migration_file]
        else:
            ref = _base(base, ctx)
            changed = git.changed_files(ref)
            migrations = [
                f for f in changed if f.status in "AM" and matches_any(f.path, config.migrations) and not is_down_migration(f.path)
            ]
            payload["migration_files"] = [
                {"file_path": f.path, "raw_sql": _read_text(f.path)} for f in sorted(migrations, key=lambda f: migration_sort_key(f.path))
            ]
            payload["prior_migrations"] = _prior_migrations(ref, config, {f.path for f in migrations})
            schema = config.prisma_schema
            if schema and not migrations and any(f.path == schema and f.status == "M" for f in changed):
                base_schema = git.show(git.merge_base(ref), schema)
                if base_schema is not None:
                    payload["prisma"] = {"file_path": schema, "base_schema": base_schema, "head_schema": _read_text(schema)}
    except git.GitError as err:
        raise Exit(2, str(err)) from err

    gh = _github(github_token, repo, pr) if comment else None
    if not payload["migration_files"] and "prisma" not in payload:
        typer.echo("No migration or Prisma schema changes in this PR.")
        _comment(
            gh,
            pr,
            "check-ddl",
            "### ✅ seamwatch · Schema check: passed\n\nThis PR no longer changes migrations or the Prisma schema.",
            "PASSED",
            config,
        )
        return

    head = _head_dbt_project(config)
    if head is not None:
        result = _call(api.check_ddl_with_manifest, config, payload, *head, config.dbt_project_dir)
    else:
        result = _call(api.check_ddl, config, payload)
    if result is None:
        return
    _publish(result)
    for diag in result.get("diagnostics", []):
        where = ":".join(str(p) for p in (diag.get("file_path"), diag.get("line")) if p)
        _err(f"{diag['level']}: {where + ' ' if where else ''}{diag['message']}")
    _comment(gh, pr, "check-ddl", result["markdown_comment"], result["status"], config)
    code = _exit_code(result["status"], config.fail_on)
    if code:
        raise Exit(code)


def _head_dbt_project(config: Config) -> tuple[bytes, bytes | None] | None:
    """In a monorepo, the dbt manifest (and catalog) compiled on this PR branch, if present."""
    if not config.dbt_configured:
        return None
    project = Path(config.dbt_project_dir)
    manifest = project / config.manifest_path
    if not manifest.exists():
        _err(
            f"note: {manifest} not found, so models are checked as synced from main. In a monorepo, run "
            "`dbt compile` before check-ddl so models this PR also updates are taken into account."
        )
        return None
    catalog = project / config.catalog_path if config.catalog_path else None
    return manifest.read_bytes(), catalog.read_bytes() if catalog and catalog.exists() else None


def _prior_migrations(ref: str, config: Config, changed: set[str]) -> list[str]:
    """Migrations already on the base branch, so the API knows column types before ALTER ... TYPE."""
    base = git.merge_base(ref)
    out, total = [], 0
    paths = [p for p in git.files_at(ref) if p not in changed and matches_any(p, config.migrations) and not is_down_migration(p)]
    for path in sorted(paths, key=migration_sort_key):
        sql = git.show(base, path) or ""
        total += len(sql)
        if total > MAX_PRIOR_MIGRATION_BYTES:
            _err("warning: migration history is large; old column types will come from the warehouse catalog instead.")
            return []
        out.append(sql)
    return out


# --- check-sql --------------------------------------------------------------------------


@app.command("check-sql")
def check_sql(
    manifest: Path | None = typer.Option(None, help="manifest.json compiled from the PR branch."),
    base: str | None = Base,
    pr: int | None = Pr,
    repo: str | None = Repo,
    github_token: str | None = Token,
    comment: bool = Comment,
    suggestions: bool = typer.Option(True, "--suggestions/--no-suggestions", help="Post verified rewrites as line suggestions."),
    api_url: str | None = ApiUrl,
    api_key: str | None = ApiKey,
    config_path: str = ConfigPath,
) -> None:
    """Check an analytics PR's changed models for broken contracts and cost anti-patterns."""
    api, config, ctx = _setup(api_url, api_key, config_path)
    pr = pr or ctx.pr_number
    if not repo:
        raise Exit(2, "Set --repo (GITHUB_REPOSITORY).")
    project = config.dbt_project_dir
    manifest = manifest or Path(project) / config.manifest_path
    if not manifest.exists():
        raise Exit(2, f"{manifest} not found. Run `dbt compile` on the PR branch first.")
    nodes = _read_manifest(manifest)[1].get("nodes") or {}
    by_path = {
        posixpath.normpath(posixpath.join(project, n["original_file_path"])): n
        for n in nodes.values()
        if n.get("resource_type") in ("model", "snapshot") and n.get("original_file_path")
    }

    try:
        changed = git.changed_files(_base(base, ctx))
    except git.GitError as err:
        raise Exit(2, str(err)) from err
    models, deleted = [], []
    for f in changed:
        if not f.path.endswith(".sql"):
            continue
        if f.status == "D":
            deleted.append(posixpath.relpath(f.path, project) if project != "." else f.path)
            continue
        node = by_path.get(f.path)
        if node is None:
            continue
        compiled = _compiled(node)
        if not compiled:
            _err(f"warning: {f.path} has no compiled SQL in the manifest; run `dbt compile`.")
            continue
        models.append(
            {
                "file_path": f.path,
                "unique_id": node["unique_id"],
                "compiled_sql": compiled,
                "raw_sql": _read_text(f.path),
            }
        )

    gh = _github(github_token, repo, pr) if comment else None
    if not models and not deleted:
        typer.echo("No dbt model changes in this PR.")
        _comment(gh, pr, "check-sql", "### ✅ seamwatch · Model check: passed\n\nThis PR no longer changes dbt models.", "PASSED", config)
        return
    payload = {"repo_full_name": repo, "pr_number": pr, "models": models, "deleted_models": deleted, **_rule_options(config)}
    result = _call(api.check_sql, config, payload)
    if result is None:
        return
    _publish(result)
    _comment(gh, pr, "check-sql", result["markdown_comment"], result["status"], config)
    if gh is not None and suggestions:
        line_suggestions = [
            LineSuggestion(
                path=f["file_path"],
                start_line=f["suggestion"]["start_line"],
                end_line=f["suggestion"]["end_line"],
                replacement=f["suggestion"]["replacement"],
                title=f"{f['rule_id']} · {f['title']}",
                message=f["message"],
            )
            for f in result["finops_optimizations"]
            if f.get("suggestion") and f.get("file_path")
        ]
        try:
            posted, skipped = gh.post_suggestions(pr, ctx.head_sha or git.head_sha(), line_suggestions)
            if line_suggestions:
                _err(
                    f"Line suggestions: {posted} posted, {skipped} skipped "
                    f"(already posted, outside the diff, or over {MAX_SUGGESTIONS} per run)."
                )
        except GitHubError as err:
            print(f"::warning title=seamwatch::could not post suggestions: {err}")
    code = _exit_code(result["status"], config.fail_on)
    if code:
        raise Exit(code)


@app.command()
def version() -> None:
    """Print the CLI version."""
    typer.echo(__version__)


def main(argv: list[str] | None = None) -> int:
    global _fail_open
    _fail_open = True
    try:
        app(args=argv, standalone_mode=False)
    except Exit as exc:
        if exc.message and exc.code == 0:
            print(exc.message)  # a workflow command (::warning), which GitHub reads from stdout
        elif exc.message:
            _err(f"error: {exc.message}" if exc.code == 2 else exc.message)
        return exc.code
    except typer.Exit as exc:
        return exc.exit_code
    except Exception as exc:
        # Usage errors. Typer >= 0.27 no longer installs Click, so they are matched by shape, not class.
        if callable(getattr(exc, "show", None)) and isinstance(getattr(exc, "exit_code", None), int):
            exc.show()
            return 2
        # A bug in seamwatch: keep the traceback for the report, and treat it like an outage.
        traceback.print_exc()
        message = f"seamwatch {__version__} crashed: {type(exc).__name__}: {exc}"
        if _fail_open:
            print(f"::warning title=seamwatch::{message} (fail_open is on, so this check is skipped)")
            return 0
        print(f"::error title=seamwatch::{message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
