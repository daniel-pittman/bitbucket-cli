"""
bb_api — Bitbucket Cloud REST API client for the bb MCP server.

This module is the Python *parallel* of the helpers at the top of the `bb`
bash script (load_config, bb_get/post/put/delete, detect_repo, repo_path).
The MCP server uses this module to talk to Bitbucket directly; it does NOT
shell out to `bb` and parse its output.

Two pieces sit side by side:

    bb (bash)         <-->  Bitbucket REST API  <-->  bb_api (Python)
                                 (single source
                                  of truth)

When a test in test_bb_api.py finds a defect in URL construction, body
shape, or auth handling, the fix lands in both bb_api.py AND `bb` if the
bash side has parallel logic. See CONTRIBUTING.md for the parity rule.

Stdlib-only on purpose: keeps the MCP server's bootstrap fast and minimises
the supply-chain surface. urllib.request is the transport.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

DEFAULT_API_BASE = "https://api.bitbucket.org/2.0"

# Pagination caps. Bitbucket's `next` cursor walking can loop if the server
# returns a malformed page; defend against that and against runaway costs by
# refusing to walk more pages than this. 200 pages * default pagelen (typically
# 10-50) is a generous ceiling for any realistic repo.
MAX_PAGINATION_ITERATIONS = 200


class BBConfigError(RuntimeError):
    """Raised when required configuration is missing or unresolvable."""


class BBApiError(RuntimeError):
    """Raised for non-2xx HTTP responses. Carries status, URL, and body."""

    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} from {url}: {body[:500]}")
        self.status = status
        self.url = url
        self.body = body


@dataclass(frozen=True)
class BBConfig:
    """Resolved credentials + workspace + API base URL for a session."""

    user: str
    token: str
    workspace: str
    api_base: str = DEFAULT_API_BASE


# --- Config loading -------------------------------------------------------


def _read_keyvalue_file(path: Path) -> dict[str, str]:
    """Parse a shell-style KEY=value file. Mirrors what `source` would do for
    the same file in bash, modulo shell substitution. Used for both
    ~/.config/bb/config and an optional .env in the script directory."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Tolerate `export KEY=value` since bash users sometimes write that.
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip a single layer of matching quotes if present.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def load_config(
    *,
    env: dict[str, str] | None = None,
    config_path: Path | None = None,
    dotenv_path: Path | None = None,
) -> BBConfig:
    """Resolve BB_USER / BB_TOKEN / BB_WORKSPACE / BB_API_BASE.

    Precedence (highest first), matching the bash script's behaviour:
        1. Process environment variables.
        2. ~/.config/bb/config (or the explicit `config_path` argument).
        3. .env in the bb script's directory (or the explicit `dotenv_path`).

    Differs from the bash script in one harmless way: bash `source` lets a
    later file overwrite an earlier one. Here, an env var ALWAYS wins so
    test callers can shadow files cleanly. The bash script gives the same
    practical result because env vars set in the current shell are visible
    to `source`d files and stay set unless the file rewrites them.

    Raises BBConfigError if any required key is missing.
    """
    env = env if env is not None else dict(os.environ)
    if config_path is None:
        config_path = Path.home() / ".config" / "bb" / "config"
    # dotenv_path is intentionally optional; callers running outside the
    # script directory don't get one by default.

    file_config: dict[str, str] = {}
    if dotenv_path is not None:
        file_config.update(_read_keyvalue_file(dotenv_path))
    file_config.update(_read_keyvalue_file(config_path))

    def resolve(key: str) -> str | None:
        return env.get(key) or file_config.get(key)

    user = resolve("BB_USER")
    token = resolve("BB_TOKEN")
    workspace = resolve("BB_WORKSPACE")
    api_base = resolve("BB_API_BASE") or DEFAULT_API_BASE

    missing = [k for k, v in [("BB_USER", user), ("BB_TOKEN", token), ("BB_WORKSPACE", workspace)] if not v]
    if missing:
        raise BBConfigError(
            f"Missing required configuration: {', '.join(missing)}. "
            "Set as environment variables or in ~/.config/bb/config."
        )

    # mypy: the missing-check above guarantees these are non-None strings.
    assert user is not None and token is not None and workspace is not None
    return BBConfig(user=user, token=token, workspace=workspace, api_base=api_base)


# --- Repo resolution ------------------------------------------------------

# Matches the trailing `workspace/repo(.git)?` of a Bitbucket remote URL.
# Handles both shapes the bash script supports:
#   https://bitbucket.org/acme/widget-service.git
#   git@bitbucket.org:acme/widget-service.git
# Group 1 is the workspace; group 2 is the repo slug.
_REMOTE_TAIL = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")


def parse_remote_url(url: str) -> tuple[str, str] | None:
    """Extract (workspace, repo_slug) from a Bitbucket remote URL.

    Returns None if the URL doesn't have a recognisable trailing
    `workspace/repo` pair. The bash version's sed regex only returns the
    repo slug; the Python version also returns the workspace because the
    MCP server uses it for cross-workspace context resolution.
    """
    match = _REMOTE_TAIL.search(url.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def detect_repo(
    path: str | os.PathLike[str] | None = None,
    *,
    runner: Any = subprocess,
) -> str:
    """Return the repo slug for the git repository at `path` (default: cwd).

    Mirrors the bash detect_repo: looks up `origin`'s URL and parses the
    repo slug out of it. `runner` is an injection seam for tests so we can
    mock subprocess.run without monkey-patching the module.

    Raises BBConfigError if the directory is not a git repo, or the remote
    URL is unparseable.
    """
    cwd = str(path) if path is not None else None
    try:
        result = runner.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
    except FileNotFoundError as e:
        raise BBConfigError("git executable not found on PATH") from e

    if result.returncode != 0:
        raise BBConfigError(
            "Not a git repository (or no `origin` remote configured). "
            "Pass an explicit repo slug instead."
        )

    parsed = parse_remote_url(result.stdout)
    if parsed is None:
        raise BBConfigError(
            f"Could not parse a repo slug from origin URL: {result.stdout!r}"
        )
    _workspace, repo = parsed
    return repo


def repo_path(workspace: str, repo: str) -> str:
    """Build the Bitbucket REST path for a repo (`/repositories/{ws}/{repo}`).

    Mirrors the bash repo_path helper. Validates that the inputs don't
    contain a slash, which would silently change the path's structure.
    """
    if "/" in workspace or "/" in repo:
        raise ValueError(
            f"workspace and repo must not contain '/'. "
            f"Got workspace={workspace!r}, repo={repo!r}."
        )
    return f"/repositories/{workspace}/{repo}"


# --- HTTP transport -------------------------------------------------------


class BBClient:
    """Thin urllib-based Bitbucket REST client.

    Constructed once per session by mcp_server.py. Each MCP tool calls a
    bb_ops.<operation>(client, ...) function rather than instantiating its
    own client, so config + auth state is shared.

    The public surface is intentionally narrow: get / post / put / delete /
    paginate. Higher-level operations live in bb_ops.py.

    `opener` is an injection seam for tests; the default is a fresh
    urllib opener with no proxy / cookie handling so the test suite
    doesn't accidentally hit a real server.
    """

    def __init__(
        self,
        config: BBConfig,
        *,
        opener: urllib.request.OpenerDirector | None = None,
        timeout: float = 30.0,
    ):
        self.config = config
        self._opener = opener or urllib.request.build_opener()
        self._timeout = timeout
        # Pre-compute the Basic auth header so each request constructs
        # the same string (cheap, but more importantly easy to assert on
        # in tests).
        creds = f"{config.user}:{config.token}".encode()
        self._auth_header = "Basic " + base64.b64encode(creds).decode()

    # -- Internal request builder --

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = self.config.api_base + path
        if query:
            # Drop None values so callers can pass `branch=None` to mean
            # "skip this query parameter."
            cleaned = {k: v for k, v in query.items() if v is not None}
            if cleaned:
                url = url + "?" + urllib.parse.urlencode(cleaned, doseq=True)

        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
            "User-Agent": "bb-mcp/1.0 (+https://github.com/daniel-pittman/bitbucket-cli)",
        }
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                body = resp.read()
                # 204 No Content is a valid empty response on DELETE / some
                # mutation endpoints; return None so callers can branch on it.
                if not body:
                    return None
                # The Bitbucket API returns JSON for every non-empty success
                # response we care about. If a future endpoint returns
                # something else (e.g. raw log text), callers should switch
                # to a lower-level fetch path; we don't try to guess here.
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - HTTPError.read can raise anything
                pass
            raise BBApiError(e.code, url, body_text) from e

    # -- Public methods --

    def get(self, path: str, *, query: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, query=query)

    def post(self, path: str, *, json_body: Any = None) -> Any:
        return self._request("POST", path, json_body=json_body)

    def put(self, path: str, *, json_body: Any = None) -> Any:
        return self._request("PUT", path, json_body=json_body)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def paginate(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        max_iterations: int = MAX_PAGINATION_ITERATIONS,
    ) -> Iterator[Any]:
        """Walk a Bitbucket paginated endpoint, yielding each item in `values`.

        Bitbucket Cloud's pagination shape:
            { "values": [...], "next": "https://api.bitbucket.org/2.0/...?page=2", ... }

        Defends against two failure modes:
          * Stuck cursor: if `next` doesn't change between iterations, stop.
          * Runaway: if we walk more than `max_iterations` pages, raise.

        The first page uses `path` + `query`; subsequent pages use the full
        URL from `next`, which already includes the relevant query string.
        """
        url: str | None = None
        last_next: str | None = None
        page_query: dict[str, Any] | None = query

        for iteration in range(max_iterations):
            if url is None:
                payload = self._request("GET", path, query=page_query)
            else:
                # Strip the api_base off `next` so _request can re-add it;
                # this keeps every request going through the same code path
                # and means tests don't need to special-case page-2 URLs.
                if url.startswith(self.config.api_base):
                    rel = url[len(self.config.api_base) :]
                else:
                    # Bitbucket's `next` should always start with our base,
                    # but be defensive: if it doesn't, refuse rather than
                    # silently following a redirect to an arbitrary host.
                    raise BBApiError(
                        0,
                        url,
                        f"pagination cursor host mismatch (expected {self.config.api_base})",
                    )
                payload = self._request("GET", rel)
                page_query = None  # only the first page uses caller's query

            if not isinstance(payload, dict):
                raise BBApiError(
                    0,
                    url or (self.config.api_base + path),
                    f"expected dict from paginated endpoint, got {type(payload).__name__}",
                )

            for item in payload.get("values", []):
                yield item

            next_url = payload.get("next")
            if not next_url:
                return
            if next_url == last_next:
                # Stuck cursor — server returned the same `next` again.
                return
            last_next = next_url
            url = next_url

        raise BBApiError(
            0,
            url or (self.config.api_base + path),
            f"pagination exceeded {max_iterations} pages without terminating",
        )
