# seamwatch

**Stop application database migrations from silently breaking your dbt models, and catch
expensive SQL before it merges.**

seamwatch is a GitHub Action with two checks:

* **Schema contract check (application PRs).** A Postgres or MySQL migration, or a
  `schema.prisma` change, is matched against your dbt project's column-level
  lineage, including columns used only in `WHERE`, `JOIN ON`, `GROUP BY` and
  window clauses. The PR fails if a drop, rename, narrowing type change or enum
  value rename would break a model, and flags changes that dbt tests and
  contracts will catch later (a column made nullable, a dropped primary key, a
  time zone change). The comment lists every affected model, exposure and owner.
* **Model check (dbt PRs).** Removed columns and deleted models are checked
  against everything downstream, and warehouse-cost anti-patterns get a rewrite
  that is verified in DuckDB before it is suggested.

> [!NOTE]
> seamwatch is in private preview. The action is not on the Marketplace yet,
> and the dashboard (`https://seamwatch.kumidatalabs.com`), API
> (`https://api.seamwatch.kumidatalabs.com`) and docs
> (`https://docs.seamwatch.kumidatalabs.com`) addresses are planned but **not
> live yet**.

## Quickstart

1. Sign in to the [dashboard](https://seamwatch.kumidatalabs.com) and create an
   API key.
2. In each repository (or once for the organization), under **Settings →
   Secrets and variables → Actions**, add the secret `SEAMWATCH_API_KEY` and the
   variable `SEAMWATCH_API_URL` (from the dashboard's Setup page).
3. Add the workflows below. Set up the dbt repo first: schema checks compare
   against its last sync.

Full guides, including a monorepo setup, are in the
[docs](https://docs.seamwatch.kumidatalabs.com).

### dbt repo

Syncs lineage on every push to `main` and checks model changes on PRs.

```yaml
# .github/workflows/seamwatch.yml
name: seamwatch
on:
  push:
    branches: [main]
  pull_request:
    paths: ["models/**", "snapshots/**", ".seamwatch.yml"]

permissions:
  contents: read
  pull-requests: write

jobs:
  seamwatch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install dbt-snowflake # your adapter
      # Compiled SQL is required (`dbt parse` leaves it empty). A read-only CI role is enough.
      - run: dbt deps && dbt compile && dbt docs generate
        env:
          DBT_PROFILES_DIR: .
          SNOWFLAKE_PASSWORD: ${{ secrets.SNOWFLAKE_CI_PASSWORD }}

      - if: github.event_name == 'push'
        uses: Kumi-Data-Labs-LLC/seamwatch-action@v0
        with:
          command: sync-dbt
          api-key: ${{ secrets.SEAMWATCH_API_KEY }}
          api-url: ${{ vars.SEAMWATCH_API_URL }}

      - if: github.event_name == 'pull_request'
        uses: Kumi-Data-Labs-LLC/seamwatch-action@v0
        with:
          command: check-sql
          api-key: ${{ secrets.SEAMWATCH_API_KEY }}
          api-url: ${{ vars.SEAMWATCH_API_URL }}
```

### Application repo

Checks migrations and `schema.prisma` changes on PRs.

```yaml
# .github/workflows/seamwatch.yml
name: seamwatch
on:
  pull_request:
    paths: ["**/migrations/**", "prisma/schema.prisma", ".seamwatch.yml"]

permissions:
  contents: read
  pull-requests: write

jobs:
  schema-contract:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0 # the check diffs against the PR's base commit
      - uses: Kumi-Data-Labs-LLC/seamwatch-action@v0
        with:
          command: check-ddl
          api-key: ${{ secrets.SEAMWATCH_API_KEY }}
          api-url: ${{ vars.SEAMWATCH_API_URL }}
```

Using MySQL, Drizzle, Flyway or renamed replica tables? Add a
[`.seamwatch.yml`](https://docs.seamwatch.kumidatalabs.com/reference/configuration).
The same file turns rules off or changes their severity (`rules:`) and ignores
files or dbt models (`ignore:`); a `-- seamwatch: ignore S003` comment ignores
one statement or model.
Alembic, Django and TypeORM pass rendered SQL with `migration-files`.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `command` | yes | | `sync-dbt` (dbt repo, on push to `main`), `check-ddl` (application repo PRs) or `check-sql` (dbt PRs) |
| `api-key` | yes | | seamwatch API key (`sg_live_...`), from a secret |
| `api-url` | yes | | seamwatch API base URL, from the dashboard's Setup page |
| `github-token` | no | `github.token` | Token for the PR comment and suggestions; needs `pull-requests: write` |
| `config` | no | `.seamwatch.yml` | Path to the config file |
| `base` | no | the PR's base commit | Ref or commit to diff against |
| `manifest` | no | from `.seamwatch.yml`, else `target/manifest.json` | Compiled `manifest.json` |
| `migration-files` | no | | `check-ddl` only: newline-separated rendered SQL files to check instead of the diff |
| `python-version` | no | `3.12` | Python used to run the CLI (3.10+) |
| `upload-report` | no | `true` | Upload `report.md` and `report.json` as the `seamwatch-report-<command>-<job>` artifact |

## Secrets and variables

| Name | Kind | Value |
| --- | --- | --- |
| `SEAMWATCH_API_KEY` | Secret | API key from the dashboard's API keys page |
| `SEAMWATCH_API_URL` | Variable | API URL from the dashboard's Setup page |

The workflow needs `contents: read` and `pull-requests: write`.

## What you see on the PR

One comment per check, updated on every push. When an application PR drops a column
a model reads, the job fails and seamwatch comments:

> ### ⛔ seamwatch · Schema check: failed
>
> **Result:** 1 breaking, 0 to review, 1 safe  
> **Checked against:** `tally/analytics`
>
> #### Breaking changes
>
> **⛔ CRITICAL · S003 · DROP COLUMN `accounts.industry`**  
> `db/migrations/0005_release.sql:4`
>
> Column is referenced downstream (SELECT_PASS); affected models will fail or change results.
>
> <details><summary>Affected downstream nodes (2)</summary>
>
> | Node | Usage | File | Owner |
> | --- | --- | --- | --- |
> | `model.tally_analytics.dim_accounts` | SELECT_PASS | models/marts/dim_accounts.sql | @data-platform |
> | `model.tally_analytics.stg_accounts` | SELECT_PASS | models/staging/stg_accounts.sql | - |
>
> </details>
>
> <details><summary>✅ Safe changes (1)</summary>
>
> - S003 · DROP COLUMN `events.user_agent`: Safe pass-through only: the column flows through SELECT * and nothing references it by name.
>
> </details>

<!-- TODO: replace with a screenshot of a real PR comment once a public demo PR exists. -->

On dbt PRs, cost findings come with a rewrite verified in DuckDB, posted as a
one-click **Commit suggestion** on the Pro FinOps plan. The full report is in
the job summary and the `seamwatch-report` artifact. See
[Reading the PR comment](https://docs.seamwatch.kumidatalabs.com/pr-output).

## Supported

dbt 1.0 to 2.0 (Fusion) · Snowflake, BigQuery, Redshift, Databricks, Spark,
Postgres, DuckDB, Trino, Athena · Postgres, MySQL and Prisma application databases · Prisma,
Drizzle, Flyway, golang-migrate, sqlx, diesel, dbmate, goose and plain SQL
migrations, plus rendered SQL from Alembic, Django, TypeORM and Laravel.
Details in [Supported versions](https://docs.seamwatch.kumidatalabs.com/reference/supported).

## License

The action and the `seamwatch` CLI in this repository are released under the
[MIT License](LICENSE). The seamwatch API they call is a hosted service.
