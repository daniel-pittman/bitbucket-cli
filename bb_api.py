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
    """Raised when an API call cannot be completed.

    The common case is a non-2xx HTTP response — `status` carries the HTTP
    code, `body` carries the response body (truncated in the message). The
    same exception is also used as a generic transport-error wrapper for
    network failures (DNS, TLS, timeout) and for malformed-response errors
    surfaced by `BBClient.paginate`; in those cases `status` is `0` and
    `body` is a diagnostic string. Callers branching on HTTP semantics
    should check `status > 0` before dispatching by code.
    """

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

    Precedence (highest first), matching the bash script's `source` order:
        1. Process environment variables (an env var set, even to "", wins).
        2. .env in the bb script's directory (`dotenv_path`).
        3. ~/.config/bb/config (`config_path`).

    The bash script `source`s ~/.config/bb/config first and `.env` second,
    so .env values overwrite the home-config values. This function mirrors
    that order. .env is a repo-local development override; ~/.config/bb is
    user-global.

    Membership test (rather than `or` coalesce) so an explicitly-set empty
    env var doesn't silently fall through to the file. The required-keys
    check below still catches empty values as missing — that part matches
    bash's `[[ -z "$BB_USER" ]]` check.

    Raises BBConfigError if any required key is missing or empty.
    """
    env = env if env is not None else dict(os.environ)
    if config_path is None:
        config_path = Path.home() / ".config" / "bb" / "config"
    # dotenv_path is intentionally optional; callers running outside the
    # script directory don't get one by default.

    # Build the file_config in the SAME order bash sources its files so
    # .env wins over the home config. Later .update() overwrites.
    file_config: dict[str, str] = {}
    file_config.update(_read_keyvalue_file(config_path))
    if dotenv_path is not None:
        file_config.update(_read_keyvalue_file(dotenv_path))

    def resolve(key: str) -> str | None:
        if key in env:
            return env[key]
        return file_config.get(key)

    user = resolve("BB_USER")
    token = resolve("BB_TOKEN")
    workspace = resolve("BB_WORKSPACE")
    api_base_raw = resolve("BB_API_BASE")
    api_base = api_base_raw if api_base_raw else DEFAULT_API_BASE
    # Normalise trailing slash so api_base + "/path" never produces "//path".
    api_base = api_base.rstrip("/")

    missing = [k for k, v in [("BB_USER", user), ("BB_TOKEN", token), ("BB_WORKSPACE", workspace)] if not v]
    if missing:
        raise BBConfigError(
            f"Missing required configuration: {', '.join(missing)}. "
            "Set as environment variables or in ~/.config/bb/config."
        )

    # Explicit narrow (not `assert ... is not None`, which `python -O` strips).
    if user is None or token is None or workspace is None:
        raise BBConfigError("Internal: required key resolved to None despite missing-check.")
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

    Caller contract — IMPORTANT: this function does NOT anchor to any
    specific host (matches bash's loose parsing, and intentional so
    enterprise / self-hosted Bitbucket deployments work). For URLs the
    developer controls (`git remote get-url origin`), that's fine. For
    URLs sourced from untrusted external input (e.g. webhook payload
    fields like `repository.links.clone[].href`), the caller is
    responsible for verifying the URL belongs to a known Bitbucket
    host BEFORE feeding it here — otherwise a github.com URL silently
    parses to a (workspace, repo) tuple that the MCP server would
    authenticate to against Bitbucket Cloud. If a future consumer
    needs that guarantee, add a `strict=True` mode here rather than
    pushing the check to every call site.
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

    Mirrors the bash repo_path helper. Validates inputs reject:
      - empty / whitespace-only values (would produce `/repositories//repo`)
      - embedded `/` (would silently change path structure)
      - `.` or `..` segments (path-traversal: after URL normalisation,
        `/repositories/../widget` resolves to `/repositories/widget` with
        the wrong workspace)
    """
    for label, value in (("workspace", workspace), ("repo", repo)):
        if not value or not value.strip():
            raise ValueError(f"{label} must be a non-empty, non-whitespace string. Got {value!r}.")
        if "/" in value:
            raise ValueError(f"{label} must not contain '/'. Got {value!r}.")
        if value in (".", ".."):
            raise ValueError(f"{label} must not be '.' or '..'. Got {value!r}.")
    return f"/repositories/{workspace}/{repo}"


# --- HTTP transport -------------------------------------------------------


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib redirect handler that refuses to follow any 3xx response.

    Why: urllib's default `HTTPRedirectHandler` resubmits the original
    request — including the `Authorization` header — against the
    `Location` URL on a 3xx response. urllib does NOT strip the auth
    header on cross-origin redirects, so a misconfigured proxy, a
    DNS-hijack, or a future Bitbucket-side 3xx pointing at a different
    host could leak the Basic auth credential to an arbitrary server.

    The bash script's `curl -sf` doesn't follow redirects by default
    either (the `bb logs` command explicitly opts in via `-L` because
    Bitbucket's log endpoint returns a 307 to S3). When bb_ops adds a
    `pipeline_logs` operation later, it will need a separate code path
    that follows redirects but strips `Authorization` on cross-host
    hops. For every other Bitbucket REST endpoint, refusing redirects
    is the correct behaviour and is what bb does today.

    Returning None from redirect_request causes urllib to surface the
    3xx as an HTTPError, which our `_request` already wraps into
    BBApiError.
    """

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _validate_query_value(key: str, value: Any) -> None:
    """Reject non-scalar query values that urlencode would silently stringify.

    With `doseq=True`, urlencode iterates dicts as their keys (`{"a":"b"}` ->
    `q=a`) and stringifies arbitrary objects via repr. Both produce surprising
    URLs that the caller never explicitly authored. Allow only scalars and
    homogeneous lists/tuples of scalars.
    """
    scalar = (str, int, float, bool)
    if isinstance(value, scalar):
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, scalar):
                raise TypeError(
                    f"query[{key!r}]: list/tuple elements must be scalars "
                    f"(str/int/float/bool), got {type(item).__name__}"
                )
        return
    raise TypeError(
        f"query[{key!r}]: must be scalar or list/tuple of scalars, "
        f"got {type(value).__name__}"
    )


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

    `timeout` is the default for every request. Each method accepts an
    override via the `timeout=` kwarg for endpoints that legitimately take
    longer (pipeline log streaming, large PR diffs).
    """

    def __init__(
        self,
        config: BBConfig,
        *,
        opener: urllib.request.OpenerDirector | None = None,
        timeout: float = 30.0,
    ):
        self.config = config
        # Default opener refuses 3xx redirects so Authorization headers
        # are never resubmitted to a Location URL. Tests pass their own
        # opener in via the `opener=` kwarg.
        self._opener = opener or urllib.request.build_opener(_NoRedirectHandler)
        self._default_timeout = timeout
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
        timeout: float | None = None,
    ) -> Any:
        url = self.config.api_base + path
        if query:
            # Drop None values so callers can pass `branch=None` to mean
            # "skip this query parameter." Validate the rest so a nested
            # dict/object doesn't silently become a meaningless URL.
            cleaned = {k: v for k, v in query.items() if v is not None}
            for k, v in cleaned.items():
                _validate_query_value(k, v)
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
        effective_timeout = timeout if timeout is not None else self._default_timeout
        try:
            with self._opener.open(req, timeout=effective_timeout) as resp:
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
        except urllib.error.URLError as e:
            # DNS failure, connection refused, TLS error, socket timeout, etc.
            # HTTPError is a subclass of URLError, so the order above matters
            # (HTTPError caught first). Wrap with status=0 to preserve the
            # documented BBApiError contract (see class docstring).
            raise BBApiError(0, url, f"network error: {e.reason}") from e

    # -- Public methods --

    def get(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        return self._request("GET", path, query=query, timeout=timeout)

    def post(
        self,
        path: str,
        *,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        return self._request("POST", path, json_body=json_body, timeout=timeout)

    def put(
        self,
        path: str,
        *,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        return self._request("PUT", path, json_body=json_body, timeout=timeout)

    def delete(self, path: str, *, timeout: float | None = None) -> Any:
        return self._request("DELETE", path, timeout=timeout)

    def fetch_redirected_text(
        self,
        path: str,
        *,
        max_redirects: int = 5,
        timeout: float | None = None,
    ) -> str:
        """Fetch raw text from an endpoint that may return a 3xx redirect
        to an external host (e.g. pipeline-log download → S3 signed URL).

        Why this exists alongside the regular `get` path:

          * The default opener refuses 3xx (see _NoRedirectHandler). The
            log endpoint at /pipelines/{uuid}/steps/{uuid}/log returns
            either an inline log body (200) OR a 307 to a signed S3 URL.
            The 307 case has to be followed to retrieve the log.

          * We must NEVER send the Bitbucket `Authorization` header to S3.
            The signed URL has its own auth via the `Signature` query
            parameter; S3 will reject the request if Basic auth is also
            present, and even when it didn't, sending the credential to
            an arbitrary host is a credential-leak.

          * The log body is plain text, not JSON. The regular `get` path
            assumes JSON.

        Implementation: open with the default opener (refuses redirects);
        on a 3xx HTTPError, extract `Location`, build a fresh Request
        WITHOUT Authorization, and follow up to `max_redirects` hops.
        Cross-host hops are always allowed (the whole point of this
        method is to follow Bitbucket -> S3); same-host hops keep the
        auth header in case Bitbucket itself ever redirects internally.

        Symmetry with bash: `bb logs` uses `curl -sfL -u user:tok`, and
        curl's `--location` does NOT resend `-u` credentials on a
        cross-host redirect (only `--location-trusted` would, or a
        custom `-H Authorization` header). So the bash side is also
        safe today. We don't rely on that behaviour — the MCP server
        may later need header-based auth where the curl analogue would
        leak, and the explicit Python check is the symmetric, future-
        proof version.

        Returns the body of the final 200 response as a decoded string.
        Raises BBApiError on too-many-redirects, missing Location header,
        non-3xx HTTP errors, or transport failures.
        """
        url = self.config.api_base + path
        # The first request carries Bitbucket auth. We rebuild headers on
        # each hop so a cross-host redirect can drop the credential.
        # Hostname compared case-insensitively per RFC 3986 §3.2.2 so
        # `API.bitbucket.org` doesn't trigger a needless auth-strip on a
        # capitalisation-only difference.
        bitbucket_host = urllib.parse.urlparse(self.config.api_base).netloc.lower()
        send_auth = True

        for hop in range(max_redirects + 1):
            headers = {
                "Accept": "*/*",
                "User-Agent": "bb-mcp/1.0 (+https://github.com/daniel-pittman/bitbucket-cli)",
            }
            if send_auth:
                headers["Authorization"] = self._auth_header

            req = urllib.request.Request(url, method="GET", headers=headers)
            effective_timeout = timeout if timeout is not None else self._default_timeout
            try:
                with self._opener.open(req, timeout=effective_timeout) as resp:
                    body = resp.read()
                    return body.decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                try:
                    if e.code not in (301, 302, 303, 307, 308):
                        body_text = ""
                        try:
                            body_text = e.read().decode("utf-8", errors="replace")
                        except Exception:  # noqa: BLE001
                            pass
                        raise BBApiError(e.code, url, body_text) from e
                    # 3xx: extract Location, follow it. urllib's HTTPError
                    # exposes the response headers via e.headers.
                    location = e.headers.get("Location") if e.headers else None
                    if not location:
                        raise BBApiError(
                            e.code, url, "redirect response missing Location header"
                        ) from e
                    new_url = urllib.parse.urljoin(url, location)
                    new_host = urllib.parse.urlparse(new_url).netloc.lower()
                    # Strip auth on any cross-host hop. Once stripped, keep it
                    # stripped for all subsequent hops in this chain.
                    if new_host != bitbucket_host:
                        send_auth = False
                    url = new_url
                finally:
                    # Explicit close so the underlying socket is released
                    # immediately on every HTTPError path (both the 3xx
                    # redirect-extract branch and the non-3xx error-with-
                    # body branch). GC would do this eventually but
                    # explicit-is-better under load.
                    try:
                        e.close()
                    except Exception:  # noqa: BLE001
                        pass
            except urllib.error.URLError as e:
                raise BBApiError(0, url, f"network error: {e.reason}") from e

        raise BBApiError(
            0, url, f"redirect chain exceeded {max_redirects} hops"
        )

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
        # Acceptable separators after api_base in a continuation URL. A bare
        # `startswith(api_base)` is a *string* match that lets
        # `https://api.bitbucket.org/2.0evil.example.com/...` slip past, so
        # we require the next character to be a path or query separator.
        base_with_path = self.config.api_base + "/"
        base_with_query = self.config.api_base + "?"

        for iteration in range(max_iterations):
            if url is None:
                # First page uses caller's `path` + `query` (already in
                # closure as the `query` parameter — no second variable
                # needed since subsequent pages route through the `next`
                # URL which carries any continuation params itself).
                payload = self._request("GET", path, query=query)
            else:
                # Strip the api_base off `next` so _request can re-add it;
                # this keeps every request going through the same code path
                # and means tests don't need to special-case page-2 URLs.
                if url.startswith(base_with_path) or url.startswith(base_with_query):
                    rel = url[len(self.config.api_base) :]
                else:
                    # Bitbucket's `next` should always start with our base
                    # followed by `/` or `?`. Anything else (different host,
                    # or the prefix-trick where api_base is followed by
                    # arbitrary characters) is refused rather than followed.
                    raise BBApiError(
                        0,
                        url,
                        f"pagination cursor host mismatch (expected {self.config.api_base})",
                    )
                payload = self._request("GET", rel)

            if not isinstance(payload, dict):
                raise BBApiError(
                    0,
                    url or (self.config.api_base + path),
                    f"expected dict from paginated endpoint, got {type(payload).__name__}",
                )

            if "values" not in payload:
                raise BBApiError(
                    0,
                    url or (self.config.api_base + path),
                    "paginated response missing 'values' key",
                )

            for item in payload["values"]:
                yield item

            next_url = payload.get("next")
            if not next_url:
                return
            if not isinstance(next_url, str):
                raise BBApiError(
                    0,
                    url or (self.config.api_base + path),
                    f"paginated response 'next' must be a string, got {type(next_url).__name__}",
                )
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
