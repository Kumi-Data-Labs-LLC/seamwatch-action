"""HTTP client for the seamwatch API."""

from __future__ import annotations

import gzip
import json
import time
from typing import Any

import httpx

from . import __version__


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_outage(self) -> bool:
        """Server-side or network trouble, or rate limiting (as opposed to a bad key or request)."""
        return self.status_code is None or self.status_code >= 500 or self.status_code == 429


class SeamwatchApi:
    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None, retries: int = 2):
        self.retries = retries
        self.client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
        self.client.base_url = base_url.rstrip("/")
        self.client.headers.update({"Authorization": f"Bearer {api_key}", "User-Agent": f"seamwatch-cli/{__version__}"})

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last: ApiError | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.client.request(method, path, **kwargs)
            except httpx.HTTPError as err:
                last = ApiError(f"cannot reach the seamwatch API: {err}")
            else:
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError:
                        # e.g. a proxy or load balancer page instead of the API
                        last = ApiError(
                            f"seamwatch API returned {response.status_code} with a body that is not JSON: {response.text[:200]!r}"
                        )
                else:
                    detail = _detail(response)
                    if request_id := response.headers.get("x-request-id"):
                        detail += f" (request id {request_id})"  # lets us find the server log
                    last = ApiError(f"seamwatch API returned {response.status_code}: {detail}", response.status_code)
                    if not last.is_outage:
                        break
                    if response.status_code == 429 and attempt < self.retries:
                        time.sleep(_retry_after(response, default=2**attempt))
                        continue
            if attempt < self.retries:
                time.sleep(2**attempt)
        assert last is not None
        raise last

    def sync(self, manifest: bytes, catalog: bytes | None, repo: str, sha: str, dialect: str | None = None) -> dict[str, Any]:
        files = {"manifest": ("manifest.json.gz", gzip.compress(manifest), "application/gzip")}
        if catalog is not None:
            files["catalog"] = ("catalog.json.gz", gzip.compress(catalog), "application/gzip")
        data = {"repo_full_name": repo, "git_commit_sha": sha}
        if dialect:
            data["dialect"] = dialect
        return self._request("POST", "/v1/manifest/sync", files=files, data=data)

    def check_ddl(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/ci/check-ddl", json=payload)

    def check_ddl_with_manifest(
        self, payload: dict[str, Any], manifest: bytes, catalog: bytes | None, dbt_project_dir: str
    ) -> dict[str, Any]:
        """check-ddl against a dbt project compiled from the same PR branch (monorepo)."""
        files = {"manifest": ("manifest.json.gz", gzip.compress(manifest), "application/gzip")}
        if catalog is not None:
            files["catalog"] = ("catalog.json.gz", gzip.compress(catalog), "application/gzip")
        data = {"body": json.dumps(payload), "dbt_project_dir": dbt_project_dir}
        return self._request("POST", "/v1/ci/check-ddl/with-manifest", files=files, data=data)

    def check_sql(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/ci/check-sql", json=payload)


def _retry_after(response: httpx.Response, default: float, cap: float = 30.0) -> float:
    try:
        return min(float(response.headers.get("retry-after", default)), cap)
    except ValueError:
        return default


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return str(detail)[:300]
