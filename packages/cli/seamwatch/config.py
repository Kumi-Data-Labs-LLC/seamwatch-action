"""`.seamwatch.yml`: optional per-repo settings.

```yaml
# Backend repo
database: postgres         # postgres | mysql (default: from schema.prisma's provider, else postgres)
migrations:                # globs for raw SQL migrations (default below)
  - "db/migrations/**/*.sql"
prisma_schema: prisma/schema.prisma
source_names: [app]        # only match dbt sources in these source groups
table_map:                 # backend table -> warehouse table, if replication renames it
  accounts: app_accounts
fail_on: critical          # critical | warning | never

# Both: turn rules off, change their severity, or ignore files and dbt models.
rules:
  C005: off
  S006: critical
ignore:
  - paths: ["db/migrations/legacy/**"]
  - models: ["dim_date_spine"]
    rules: [C006]

# Analytics (dbt) repo. In a monorepo (backend and dbt project in one repo), check-ddl
# also reads this manifest, compiled on the PR branch, so models the PR updates count.
dbt:
  project_dir: .           # folder containing dbt_project.yml
  manifest: target/manifest.json   # relative to project_dir
  catalog: target/catalog.json
```
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MIGRATION_GLOBS = ["**/migrations/**/*.sql", "migrations/*.sql"]
DEFAULT_PRISMA_SCHEMAS = ["prisma/schema.prisma", "schema.prisma"]
FAIL_ON = ("critical", "warning", "never")
DATABASES = ("postgres", "mysql")


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    migrations: list[str] = field(default_factory=lambda: list(DEFAULT_MIGRATION_GLOBS))
    database: str = "postgres"  # the backend's SQL dialect
    prisma_schema: str | None = None
    source_names: list[str] | None = None
    table_map: dict[str, str] = field(default_factory=dict)
    fail_on: str = "critical"
    fail_open: bool = True  # if the seamwatch API is unreachable, warn instead of failing CI
    comment_on_pass: bool = False
    dbt_project_dir: str = "."  # where dbt_project.yml lives, relative to the repo root
    dbt_configured: bool = False  # a `dbt:` section is present (an analytics repo, or a monorepo)
    manifest_path: str = "target/manifest.json"
    catalog_path: str | None = "target/catalog.json"
    rules: dict[str, str] = field(default_factory=dict)  # code -> off | info | warning | critical
    ignore: list[dict[str, Any]] = field(default_factory=list)  # {paths, models, rules}
    warnings: list[str] = field(default_factory=list)


_KNOWN = {
    "database",
    "migrations",
    "prisma_schema",
    "source_names",
    "table_map",
    "fail_on",
    "fail_open",
    "comment_on_pass",
    "dbt",
    "rules",
    "ignore",
}
RULE_VALUES = ("off", "info", "warning", "critical")
_CODE = re.compile(r"^[CS]\d{3}$")
_IGNORE_KEYS = {"paths", "models", "rules"}


def load_config(path: str | Path = ".seamwatch.yml", root: Path | None = None) -> Config:
    root = root or Path.cwd()
    file = root / path
    config = Config()
    if not file.exists():
        config.prisma_schema = _detect_prisma(root)
        config.database = _prisma_provider(root, config.prisma_schema) or config.database
        return config
    try:
        data: dict[str, Any] = yaml.safe_load(file.read_text()) or {}
    except yaml.YAMLError as err:
        raise ConfigError(f"{path}: invalid YAML ({err})") from err
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    for key in sorted(set(data) - _KNOWN):
        config.warnings.append(f"{path}: unknown key '{key}' ignored")
    if "migrations" in data:
        config.migrations = _str_list(data["migrations"], "migrations")
    config.prisma_schema = data.get("prisma_schema") or _detect_prisma(root)
    config.database = str(data.get("database") or _prisma_provider(root, config.prisma_schema) or config.database).lower()
    if config.database not in DATABASES:
        raise ConfigError(f"database must be one of {', '.join(DATABASES)} (got {config.database!r})")
    if data.get("source_names") is not None:
        config.source_names = _str_list(data["source_names"], "source_names")
    table_map = data.get("table_map") or {}
    if not isinstance(table_map, dict):
        raise ConfigError("table_map must be a mapping of backend table -> warehouse table")
    config.table_map = {str(k): str(v) for k, v in table_map.items()}
    config.fail_on = str(data.get("fail_on", config.fail_on)).lower()
    if config.fail_on not in FAIL_ON:
        raise ConfigError(f"fail_on must be one of {', '.join(FAIL_ON)}")
    config.fail_open = bool(data.get("fail_open", config.fail_open))
    config.comment_on_pass = bool(data.get("comment_on_pass", config.comment_on_pass))
    dbt = data.get("dbt") or {}
    config.dbt_configured = "dbt" in data
    config.dbt_project_dir = str(dbt.get("project_dir", config.dbt_project_dir))
    config.manifest_path = dbt.get("manifest", config.manifest_path)
    config.catalog_path = dbt.get("catalog", config.catalog_path)
    config.rules = _rules(data.get("rules"), path, config.warnings)
    config.ignore = _ignore(data.get("ignore"), path, config.warnings)
    return config


def _code(value: Any, where: str, warnings: list[str], path: str | Path) -> str:
    code = str(value).strip().upper()
    if not _CODE.match(code):
        raise ConfigError(f"{where}: {value!r} is not a rule code (like C006 or S003)")
    return code


def _rules(value: Any, path: str | Path, warnings: list[str]) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("rules must map a rule code to off, info, warning or critical")
    out = {}
    for key, setting in value.items():
        code = _code(key, "rules", warnings, path)
        # YAML reads a bare `off` as False.
        text = "off" if setting is False else str(setting).strip().lower()
        if text not in RULE_VALUES:
            raise ConfigError(f"rules.{code} must be one of {', '.join(RULE_VALUES)} (got {setting!r})")
        out[code] = text
    return out


def _ignore(value: Any, path: str | Path, warnings: list[str]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError("ignore must be a list of entries with `paths`, `models` and optionally `rules`")
    out = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ConfigError(f"ignore[{i}] must be a mapping with `paths` or `models`")
        for key in sorted(set(entry) - _IGNORE_KEYS - {"rule"}):
            warnings.append(f"{path}: unknown key 'ignore[{i}].{key}' ignored")
        codes = entry.get("rules", entry.get("rule"))
        parsed = {
            "paths": _str_list(entry["paths"], f"ignore[{i}].paths") if entry.get("paths") is not None else [],
            "models": _str_list(entry["models"], f"ignore[{i}].models") if entry.get("models") is not None else [],
            "rules": [_code(c, f"ignore[{i}].rules", warnings, path) for c in _str_list(codes, f"ignore[{i}].rules")] if codes else None,
        }
        if not parsed["paths"] and not parsed["models"]:
            raise ConfigError(f"ignore[{i}] needs `paths` or `models` (to turn a rule off everywhere, use `rules: {{CODE: off}}`)")
        out.append(parsed)
    return out


def _str_list(value: Any, key: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise ConfigError(f"{key} must be a string or a list of strings")


def _detect_prisma(root: Path) -> str | None:
    return next((p for p in DEFAULT_PRISMA_SCHEMAS if (root / p).exists()), None)


_PRISMA_PROVIDER = re.compile(r'(?s)datasource\s+\w+\s*\{[^}]*?provider\s*=\s*"(?P<provider>\w+)"')


def _prisma_provider(root: Path, schema: str | None) -> str | None:
    """`postgres` or `mysql` from schema.prisma's datasource, if it names one of them."""
    if not schema or not (root / schema).exists():
        return None
    match = _PRISMA_PROVIDER.search((root / schema).read_text())
    provider = match.group("provider").lower() if match else None
    return {"postgresql": "postgres", "postgres": "postgres", "mysql": "mysql"}.get(provider or "")


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Path glob with `**` (any number of directories), `*` and `?`."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(glob_to_regex(p).match(path) for p in patterns)


_DOWN_FILE = re.compile(r"(?i)(?:^|[._-])down\.sql$|^U\d[\w.]*__.*\.sql$")


def is_down_migration(path: str) -> bool:
    """Rollback files (`0002_x.down.sql`, diesel's `down.sql`, Flyway undo `U2__x.sql`) never run on deploy."""
    return bool(_DOWN_FILE.search(path.rsplit("/", 1)[-1]))


_VERSION_PREFIX = re.compile(r"(?i)^[VR]?(\d+(?:[._]\d+)*)")


def _natural(text: str) -> list[tuple[int, int | str]]:
    return [(0, int(part)) if part.isdigit() else (1, part) for part in re.split(r"(\d+)", text) if part]


def migration_sort_key(path: str) -> tuple:
    """Apply order: numbers compare as numbers (Flyway `V10` after `V2`) and a
    dotted / underscored version prefix is one version (`V1_1` after `V1`)."""
    directory, _, name = path.rpartition("/")
    match = _VERSION_PREFIX.match(name)
    version = tuple(int(p) for p in re.split(r"[._]", match.group(1))) if match else ()
    return (_natural(directory), version, _natural(name))
