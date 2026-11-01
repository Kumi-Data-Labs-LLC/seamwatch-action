# seamwatch CLI

Thin client for the seamwatch API, used directly or through the GitHub Action
(`action.yml` at the repo root). It only reads files and git history and talks
to the API; the analysis runs server-side.

```bash
pip install ./packages/cli
export SEAMWATCH_API_URL=... SEAMWATCH_API_KEY=sg_live_...

seamwatch sync-dbt                       # analytics repo, after `dbt compile` on main
seamwatch check-ddl --base origin/main   # backend repo PR
seamwatch check-sql --base origin/main   # analytics repo PR, after `dbt compile` on the PR branch
```

| Command | Reads | Sends |
| --- | --- | --- |
| `sync-dbt` | `target/manifest.json`, `target/catalog.json` | both, gzipped |
| `check-ddl` | migrations added/changed since the base (globs in `.seamwatch.yml`), migration history at the base, or `schema.prisma` base vs head when no SQL migration changed; or `--migration-file` | SQL / Prisma text |
| `check-sql` | changed / deleted model files and their compiled SQL from the manifest | compiled + raw SQL of changed models |

In GitHub Actions it reads the PR number and base commit from the event,
keeps one sticky PR comment per check up to date, and posts verified FinOps
rewrites (Pro FinOps) as line suggestions with a "Commit suggestion" button
(at most 20 per run, on lines in the diff).

The full report is written to the job summary and to `seamwatch-report/`
(`report.md`, and `report.json` with the raw API result); the Action uploads
that folder as the `seamwatch-report-<command>-<job>` artifact (kept 30 days;
turn off with `upload-report: false`). The PR comment is the same report
shortened for large PRs: each section shows its first 10 findings in full with
up to 10 affected models each, the rest one line each, and it is cut below
GitHub's 65,536-character limit with a link to the full report.

Exit codes: `0` passed, `1` failed per `fail_on` (or the API rejected the
request, e.g. a bad key), `2` usage/config/git error. If the API is unreachable
or rate limited, or the CLI crashes, and `fail_open` is on (default), it warns
and exits `0`. Runs without secrets (fork and Dependabot PRs) are skipped with a
warning.

Configuration lives in an optional `.seamwatch.yml`; see `seamwatch/config.py`
for every key. `rules:` and `ignore:` are validated here and sent to the API with
each check, which applies them (and inline `seamwatch: ignore` comments) after
the checks run; see `seamwatch_engine/rule_config.py`.
