#!/usr/bin/env python3
"""
bb MCP server — exposes Bitbucket Cloud operations to any Claude Code session
on this machine via the bb_ops / git_ops Python modules.

The MCP agent gets tools covering:
  - Pipelines: list, show, steps, trigger, stop, logs
  - Pull requests: list, show, activity, create, approve, unapprove, merge,
    decline, diff, comments-list, comment-add
  - Repos / branches / vars / downloads / commits
  - Git context: current_branch, status, remote_repo, recent_commits,
    uncommitted_changes
  - Meta: whoami

Every Bitbucket tool accepts an optional `repo` argument:
  - "" (empty)  → auto-detect from `git remote get-url origin` in cwd
  - "myrepo"    → use BB_WORKSPACE from config + "myrepo"
  - "acme/repo" → use "acme" workspace + "repo" slug (overrides config)

Run as a subprocess (stdio transport):
    python3 mcp_server.py

The script self-bootstraps a venv under `$XDG_DATA_HOME/bitbucket-cli/venv`
(default `~/.local/share/bitbucket-cli/venv`) on first run, installs the
`mcp` package into it, then re-execs under that venv. Any python3 on
PATH that can run `python3 -m venv` works as the launcher (must be 3.10+;
on macOS prefer Homebrew or pyenv over Apple's bundled 3.9 at
/usr/bin/python3). The venv location is durable (survives reboot), so
subsequent launches re-exec into the existing venv without rebuilding.

Register user-scope so every Claude Code session sees it:
    claude mcp add --scope user bitbucket \\
        -- python3 /path/to/bitbucket-cli/mcp_server.py

Environment overrides:
  BB_USER, BB_TOKEN, BB_WORKSPACE — auth + workspace (see bb_api docs)
  BB_API_BASE                     — Bitbucket REST base (default api.bitbucket.org/2.0)
  BB_DEFAULT_REPO_PATH            — git checkout dir for auto-detect (default: cwd)
  BB_MCP_SKIP_BOOTSTRAP=1         — test escape hatch (skips venv + stubs FastMCP)
  XDG_DATA_HOME                   — overrides the venv parent dir (default
                                    ~/.local/share); the venv lives at
                                    `$XDG_DATA_HOME/bitbucket-cli/venv`
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Self-bootstrap: ensure the venv exists and re-exec into it. Built under
# `$XDG_DATA_HOME/bitbucket-cli/venv` (default `~/.local/share/bitbucket-cli/
# venv`) so it survives reboots — the previous `/tmp/bbenv` location would
# get wiped at every boot, forcing a fresh ~30s rebuild. The new location
# follows the XDG Base Directory spec and matches the pattern used by
# zenhub-cli (`~/.local/share/zenhub-cli/venv`).
#
# Must run before any third-party import (mcp).
# ---------------------------------------------------------------------------


def _xdg_data_home() -> Path:
    """Return the resolved XDG data dir for app state. Honors
    XDG_DATA_HOME when set (per the spec); falls back to
    `~/.local/share`. Returned at module-import time so the path is
    pinned for the rest of the bootstrap."""
    explicit = os.environ.get("XDG_DATA_HOME")
    if explicit:
        return Path(explicit)
    return Path.home() / ".local" / "share"


_VENV_DIR = _xdg_data_home() / "bitbucket-cli" / "venv"
_VENV_PY = _VENV_DIR / "bin" / "python3"
# Sentinel file written ONLY after the full bootstrap (venv create + pip
# install) succeeds. If pip is Ctrl-C'd / OOM-killed / disk-full mid-run,
# _VENV_PY exists but `mcp` doesn't — without this sentinel, every
# subsequent launch would silently skip reinstall, re-exec into the
# broken venv, and die on `from mcp.server.fastmcp import FastMCP`.
_VENV_READY = _VENV_DIR / ".bbenv-ready"
# Pin to mcp>=1.0,<2 so a breaking mcp 2.x release doesn't silently
# install on a fresh-machine bootstrap (or a manual `rm` of the venv)
# and break every subsequent launch. Matches the pyproject.toml [mcp]
# extra.
_VENV_DEPS = ("mcp>=1.0,<2",)  # No heavy deps (no torch / sentence-transformers).
_VENV_MIN_PY = (3, 10)  # bb_api uses PEP 604 unions; mcp also needs >=3.10


def _find_builder_python() -> str:
    """Return a python3 executable suitable for building the venv. Prefer
    the interpreter that invoked us; fall back to common Homebrew / pyenv /
    system locations. Skips anything below `_VENV_MIN_PY`."""
    import shutil

    if sys.version_info >= _VENV_MIN_PY:
        return sys.executable
    candidates = [
        "/opt/homebrew/opt/pyenv/shims/python3",
        os.path.expanduser("~/.pyenv/shims/python3"),
        shutil.which("python3"),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]
    probe = (
        "import sys; "
        f"sys.exit(0 if sys.version_info >= {_VENV_MIN_PY} else 1)"
    )
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen or not os.path.exists(cand):
            continue
        seen.add(cand)
        try:
            subprocess.check_call(
                [cand, "-c", probe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return cand
        except (subprocess.CalledProcessError, OSError):
            continue
    raise RuntimeError(
        f"No python3 >= {_VENV_MIN_PY[0]}.{_VENV_MIN_PY[1]} found to build "
        f"{_VENV_DIR}; install one (e.g. via pyenv or `brew install python`) "
        f"and retry."
    )


def _pip_install_or_diagnose(args: list[str]) -> None:
    """Run pip install, capturing stderr so a failure surfaces with the
    real diagnostic (network blip, version yank, SSL cert, proxy) rather
    than `CalledProcessError: returned non-zero exit status 1`.

    Dropping `--quiet` AND capture_output=True so the user sees pip's
    actual error in the bootstrap-failure message.
    """
    try:
        subprocess.run(args, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        # Re-raise with the captured stderr inlined so the user can act
        # on it. The original CalledProcessError loses pip's diagnostic.
        diag = (e.stderr or e.stdout or "").strip()
        raise RuntimeError(
            f"[bb-mcp] pip install failed (exit {e.returncode}):\n{diag}"
        ) from e


def _bootstrap_venv() -> None:
    """Create the venv on first run, install deps, re-exec under it.

    Idempotent: a fully-bootstrapped venv (sentinel file present)
    re-execs immediately. A partially-bootstrapped venv (venv exists
    but sentinel doesn't — e.g. previous pip install was Ctrl-C'd or
    OOM-killed) gets the pip install retried with no manual cleanup
    needed.

    Creates parent directories if needed (e.g. on a fresh machine the
    XDG data dir may not exist yet).
    """
    if not _VENV_READY.exists():
        builder = _find_builder_python()
        # Log to stderr so MCP stdio transport isn't corrupted.
        print(
            f"[bb-mcp] bootstrapping {_VENV_DIR} with {builder}",
            file=sys.stderr,
        )
        # Make sure the parent directory exists. On a fresh machine
        # `~/.local/share` may exist but `~/.local/share/bitbucket-cli`
        # won't yet — `python -m venv` doesn't create intermediate
        # directories above the target.
        _VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
        # Only create the venv if it doesn't already exist (a previous
        # half-finished bootstrap left _VENV_PY in place).
        if not _VENV_PY.exists():
            subprocess.check_call([builder, "-m", "venv", str(_VENV_DIR)])
        _pip_install_or_diagnose(
            [str(_VENV_PY), "-m", "pip", "install",
             "--no-cache-dir", "--upgrade", "pip"]
        )
        _pip_install_or_diagnose(
            [str(_VENV_PY), "-m", "pip", "install",
             "--no-cache-dir", *_VENV_DEPS]
        )
        # Sentinel last — any earlier failure leaves it absent so the
        # next launch retries the install.
        _VENV_READY.touch()

    # Detect "are we already running under the bootstrap venv?" via
    # resolved sys.prefix. `python -m venv` on Linux/macOS symlinks
    # the interpreter into the venv's bin/, and various platform path
    # quirks (e.g. macOS's `/tmp -> /private/tmp`, `~/` resolution
    # differences) mean we must resolve BOTH sides through realpath,
    # not just rely on string equality. Without the resolve, the
    # comparison can disagree on the same logical path and trigger
    # an infinite execv loop.
    if Path(sys.prefix).resolve() != _VENV_DIR.resolve():
        # Resolve __file__ so a relative-launch (`python3 mcp_server.py`
        # from inside the repo) followed by any future chdir between
        # launch and execv doesn't leave the venv python with an
        # unresolvable script path.
        os.execv(
            str(_VENV_PY),
            [str(_VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]],
        )


# Test-mode escape hatch: setting BB_MCP_SKIP_BOOTSTRAP=1 in the environment
# skips the venv bootstrap AND substitutes a minimal FastMCP stub for the
# import below. This lets the pytest suite exercise tool wiring + result-dict
# shapes without pulling in `mcp`. Production (the actual MCP server
# transport) must NEVER set this — without the real FastMCP, the server
# doesn't serve.
_MCP_SKIP_BOOTSTRAP = os.environ.get("BB_MCP_SKIP_BOOTSTRAP", "") == "1"

if not _MCP_SKIP_BOOTSTRAP:
    _bootstrap_venv()
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]
    except ImportError as e:
        # Sentinel-present launch found `mcp` missing — manual `pip
        # uninstall`, partial filesystem cleanup that wiped the package
        # dir but spared the touch file, or an image-layer accident.
        # Tell the user the recovery path explicitly rather than
        # letting them chase a bare ModuleNotFoundError.
        raise ImportError(
            f"[bb-mcp] FastMCP import failed even though {_VENV_READY} "
            f"says the venv is ready ({e}). The mcp package was probably "
            f"removed out-of-band. Recover with:\n"
            f"  rm {_VENV_READY}\n"
            f"…then relaunch — bootstrap will reinstall. Or nuke the "
            f"whole venv with `rm -rf {_VENV_DIR}` if state is corrupt."
        ) from e
else:
    # Minimal no-op stub. `@mcp.tool()` returns the function unchanged so
    # tests can call the wrapped tool directly. The stub class is callable
    # as `FastMCP("name")` and exposes a `.run()` that raises (we don't
    # want a test accidentally launching a server).
    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name
            self._tools: dict[str, Any] = {}

        def tool(self, *args: Any, **kwargs: Any):  # noqa: ARG002
            def _decorator(fn: Any) -> Any:
                self._tools[fn.__name__] = fn
                return fn
            return _decorator

        def run(self) -> None:
            raise RuntimeError(
                "FastMCP stub: BB_MCP_SKIP_BOOTSTRAP is set. The MCP "
                "server cannot run in this mode; it's for unit tests only."
            )


# ---------------------------------------------------------------------------
# Path setup so sibling modules are importable when the bootstrap venv
# launches us from any cwd.
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bb_api  # noqa: E402  (must come after sys.path insert)
import bb_ops  # noqa: E402
import git_ops  # noqa: E402


# ---------------------------------------------------------------------------
# Shared client + repo resolution
# ---------------------------------------------------------------------------

# Module-level client cache. load_config() reads from disk on every call;
# the MCP server is long-lived so we resolve once and reuse. Tests reset
# this via the _reset_client_cache() hook below.
_client_cache: bb_api.BBClient | None = None


def _get_client() -> bb_api.BBClient:
    """Lazily construct (and cache) the BBClient from environment / config
    files. Raises BBConfigError if required keys are missing."""
    global _client_cache
    if _client_cache is None:
        config = bb_api.load_config()
        _client_cache = bb_api.BBClient(config)
    return _client_cache


def _reset_client_cache() -> None:
    """Test hook. Production never calls this — the cache lives for the
    full server lifetime."""
    global _client_cache
    _client_cache = None


def _default_repo_path() -> str:
    """Working directory for git auto-detection. Priority:
      1. BB_DEFAULT_REPO_PATH environment variable
      2. Current working directory at MCP server launch time

    `os.environ.get("KEY", os.getcwd())` evaluates the default
    eagerly — `os.getcwd()` would run even when the env var is set,
    meaning the env-var override never actually protects against a
    deleted cwd. Use `... or os.getcwd()` so the override is lazy.
    """
    return os.environ.get("BB_DEFAULT_REPO_PATH") or os.getcwd()


def _resolve_repo(repo: str | None = "") -> tuple[bb_api.BBClient, str, str]:
    """Resolve (client, workspace, repo_slug) from a single repo argument.

    Accepted shapes for `repo`:
      - "" / None        → auto-detect via `git remote get-url origin`
                           from BB_DEFAULT_REPO_PATH (or cwd). Workspace +
                           slug come from the remote URL.
      - "myrepo"         → use config workspace (BB_WORKSPACE) + "myrepo"
      - "acme/myrepo"    → use "acme" workspace + "myrepo" slug
                           (overrides BB_WORKSPACE for this call)

    Whitespace stripped on the whole arg AND on each slug-part after
    split, so " acme/widget " AND "acme/ widget" both normalise to
    ("acme", "widget"). A `None` from a deserialised JSON `null` is
    treated the same as `""` (auto-detect path), not a crash.

    Validation happens BEFORE _get_client() so a malformed slug on a
    fresh-machine user without ~/.config/bb/config surfaces as a clean
    ValueError, not a BBConfigError that masks the real cause.

    Raises bb_api.BBConfigError on missing config (only AFTER repo is
    validated).
    Raises ValueError on malformed `repo` argument.
    """
    # Normalise: None → "", strip whitespace. JSON `null` from the MCP
    # client deserialises to None; without this guard, .strip() crashes
    # uncaught with AttributeError.
    repo = (repo or "").strip()

    if not repo:
        # Auto-detect path. _get_client AFTER any structural validation.
        client = _get_client()
        workspace, repo_slug = git_ops.git_remote_repo(path=_default_repo_path())
        return client, workspace, repo_slug

    if "/" in repo:
        # Strip every part to handle "acme/ widget" → ("acme", "widget").
        parts = [p.strip() for p in repo.split("/")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                f"repo must be 'workspace/repo' or 'repo'; got {repo!r}"
            )
        # Symmetric with the bare-slug branch below — validate `.` / `..`
        # in either segment BEFORE _get_client() so a malformed slug on
        # a config-less machine surfaces as ValueError rather than the
        # misleading BBConfigError.
        if parts[0] in (".", "..") or parts[1] in (".", ".."):
            raise ValueError(
                f"workspace and repo must not be '.' or '..'; got {repo!r}"
            )
        client = _get_client()
        return client, parts[0], parts[1]

    # Bare slug → use configured workspace. Validate against the same
    # rules bb_api.repo_path enforces (no `.` / `..`) BEFORE calling
    # _get_client(), so a malformed slug on a config-less machine
    # surfaces as ValueError rather than the misleading BBConfigError.
    if repo in (".", ".."):
        raise ValueError(
            f"repo must not be '.' or '..'; got {repo!r}"
        )
    client = _get_client()
    # BB_WORKSPACE is optional (v1.2.0). A bare slug has no workspace of
    # its own, so it can only resolve against the configured default. If
    # that's absent, fail with an actionable error rather than building a
    # "/repositories//slug" URL — name the two ways to supply one.
    if not client.config.workspace:
        raise ValueError(
            f"no workspace for bare slug {repo!r}: set BB_WORKSPACE, or pass "
            f"'workspace/{repo}'. (Inside a git checkout, omit the repo arg "
            "entirely to auto-detect both from the origin remote.)"
        )
    return client, client.config.workspace, repo


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------

# Every tool returns either {"ok": True, ...result} or {"ok": False, ...error}.
# Keeping a consistent shape means the MCP agent can branch on `ok` once and
# render the result vs. error path uniformly.

# Match a URL with embedded credentials. `[^/]+@` is greedy up to the
# last `@` before the path so passwords containing literal `@` don't
# slip through the redactor.
_URL_CRED_PATTERN = re.compile(r"://[^/]+@")

# SCP-style remote URLs (`user:token@host:path`) have no scheme prefix,
# so the regex above doesn't catch them. Match a `<user[:tok]>@<host>:`
# at the start of a line or after whitespace.
_SCP_CRED_PATTERN = re.compile(r"(^|\s)[^/:\s@]+(?::[^/@\s]*)?@(?=[^/\s]+:)")

# Lowercase signed-URL indicators (compared against the lowercased
# query part). Covers:
#   AWS:    X-Amz-Signature / X-Amz-Credential
#   GCP:    X-Goog-Signature / X-Goog-Credential
#   Azure:  sig=, sv=, se=  (SAS query parameters)
#   Plain:  Signature= (some non-AWS S3-compatible services)
#   Bearer: access_token=, api_key=  (URLs that embed bearer tokens)
# Tuples of trailing `=` so `sig=` doesn't match the trailing of
# `signature=` (which would over-match harmlessly anyway, but the
# specific patterns are clearer).
_SIGNED_URL_INDICATORS_LOWER = (
    "x-amz-signature=", "x-amz-credential=",
    "x-goog-signature=", "x-goog-credential=",
    "sig=", "signature=",
    "access_token=", "api_key=",
)


def _redact_url(url: str) -> str:
    """Strip URL-embedded credentials AND replace signed URLs whose
    query string contains a meaningful credential parameter. Used in
    `_error_dict` to defend against `pipeline_logs` / `pr_diff`
    redirect chains landing on Bitbucket's signed S3 URLs — bb_api's
    fetch_redirected_text follows the redirect and (on a downstream
    failure like S3 clock skew → 403) raises BBApiError(url=<signed
    S3 URL>). The signed URL embeds AWS credentials in the query and
    must not flow into agent context or downstream logs.

    Case-insensitive query-param match so MinIO / R2 / Backblaze /
    mixed-case AWS variants don't slip past. Covers AWS / GCP / Azure
    SAS / generic Signature= / bearer-token-in-URL shapes.
    """
    if not url:
        return url
    # `user:token@host` form (Bitbucket basic-auth-embedded URLs).
    redacted = _URL_CRED_PATTERN.sub("://[redacted]@", url)
    # Presigned-URL detection — if any of the credential-bearing query
    # parameters are present, replace the whole query string with a
    # marker. Path is preserved so the agent knows what host/path was
    # called.
    if "?" in redacted:
        path_part, _, query_part = redacted.partition("?")
        query_lower = query_part.lower()
        if any(ind in query_lower for ind in _SIGNED_URL_INDICATORS_LOWER):
            redacted = f"{path_part}?[redacted-signed-url-params]"
    return redacted


# Match ANY URL scheme (http, https, ssh, git+ssh, etc.) so SCP-style
# variants and ssh:// URLs with embedded passphrases don't slip past
# free-form-text redaction. Stops at whitespace / quote / angle-bracket /
# closing-paren — covers URLs embedded in typical log / error shapes.
_ANY_URL_PATTERN = re.compile(r"(?:[a-zA-Z][a-zA-Z0-9+.-]*)://[^\s'\"<>)]+")


def _safe_text(text: str) -> str:
    """Redact every URL-shaped substring AND SCP-style `user:tok@host:`
    forms from a free-form text field. Applied uniformly to every
    string field going into the error dict (message / body / stderr)
    so a credential leak through ANY one of those fields requires a
    new threat vector, not just a new field name.

    The previous rounds whack-a-moled fields one at a time:
      - Round 2: redacted `url` (left `message` leaking)
      - Round 3: redacted `message` (left `body` leaking)
      - Round 4: this routes all three through one helper so the
        leak class is structurally closed.
    """
    if not text:
        return text
    def _sub_url(m: re.Match[str]) -> str:
        return _redact_url(m.group(0))
    # Pass 1: redact URL-scheme forms (http://, https://, ssh://, git+ssh://, ...).
    redacted = _ANY_URL_PATTERN.sub(_sub_url, text)
    # Pass 2: SCP-style (user:tok@host:path) with no scheme prefix.
    redacted = _SCP_CRED_PATTERN.sub(lambda m: f"{m.group(1)}[redacted]@", redacted)
    return redacted


# Legacy alias retained for the existing test_bbapierror_redacts_signed_s3_url
# / test_bbapierror_redacts_embedded_creds tests. Renamed forwarder.
_redact_message = _safe_text


def _error_dict(e: Exception) -> dict[str, Any]:
    """Translate any tool-side exception into a structured error dict.

    The agent sees `kind`, `message`, and (for BBApiError) the HTTP
    status + redacted URL so it can branch on `kind == "BBApiError"
    and status == 404` without parsing the message string.

    EVERY string field that could contain a URL or credential is
    routed through `_safe_text`:
      - message     (str(e) embeds URL for BBApiError)
      - url         (BBApiError, dedicated url-only redactor)
      - body        (API response can echo the redirect target URL)
      - stderr      (GitOpError; git stderr commonly contains remote URLs
                     once Phase 4.7 wraps remote-touching commands)
    """
    kind = type(e).__name__
    out: dict[str, Any] = {
        "ok": False,
        "kind": kind,
        "message": _safe_text(str(e)),
    }
    if isinstance(e, bb_api.BBApiError):
        out["status"] = e.status
        out["url"] = _redact_url(e.url)
        out["body"] = _safe_text(e.body)
    elif isinstance(e, git_ops.GitOpError):
        out["returncode"] = e.returncode
        out["stderr"] = _safe_text(e.stderr)
    return out


def _error_dict_with(e: Exception, **extras: Any) -> dict[str, Any]:
    """Like `_error_dict` but threads caller-supplied identifiers
    (pr_id, step_index, number, ...) into the error response so the
    agent can correlate fan-out failures with their originating
    requests. Without this, parallel pipeline_logs / pr_show calls
    fail with no way to tell which call's error went to which result
    slot."""
    return {**_error_dict(e), **extras}


# Exceptions every tool wraps. Other exceptions propagate (they're
# programmer errors and should crash visibly during development).
#
# Includes:
#   - OSError covers IsADirectoryError, ConnectionResetError,
#     BlockingIOError, ChildProcessError — paths git_ops._run_git
#     doesn't wrap explicitly (only FileNotFoundError /
#     NotADirectoryError / PermissionError do). Also catches
#     os.getcwd() on a deleted cwd inside _default_repo_path().
#   - AttributeError covers the JSON-null-into-string-arg case where
#     an MCP client sends {"repo": null} and `.strip()` would
#     otherwise crash uncaught.
#
# Deliberately EXCLUDES TypeError — a refactor that renames a bb_ops
# kwarg should surface as an obvious dev-time crash, not a fake
# Bitbucket failure the agent reports back. The only intentional
# TypeError raise is in bb_api._validate_query_value, which is at
# a layer no MCP wrapper drives directly.
_TOOL_EXPECTED_EXCEPTIONS = (
    bb_api.BBApiError,
    bb_api.BBConfigError,
    bb_ops.BBOpNotFound,
    git_ops.GitOpError,
    OSError,
    AttributeError,
    ValueError,
)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("bb")


def _opt_str(value: str | None) -> str | None:
    """Normalise an MCP-string-or-null arg to a non-empty stripped string
    or None. Used for optional string parameters (branch, pattern,
    query, message) so that "", "   ", and None all funnel to None
    rather than getting inconsistently reported as different errors
    by the bb_ops layer."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


# =============================================================================
#  PIPELINE TOOLS
# =============================================================================


@mcp.tool()
def pipelines_list(
    repo: str = "",
    count: int = 10,
    branch: str = "",
    sort: str = "-created_on",
) -> dict[str, Any]:
    """List recent Bitbucket pipelines (most-recent first by default).

    Args:
        repo: Repo slug, "workspace/slug", or "" to auto-detect from git.
        count: Maximum number of pipelines to return (paginates if > 100).
        branch: Optional branch filter (e.g. "main", "feat/widget").
        sort: Sort key (default "-created_on" = newest first).
              "created_on" for oldest first.
    """
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        pipelines = bb_ops.pipelines_list(
            client, workspace, repo_slug,
            count=count,
            branch=_opt_str(branch),
            sort=sort,
        )
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "pipelines": pipelines}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def pipeline_show(number: int, repo: str = "") -> dict[str, Any]:
    """Fetch a single pipeline by build number."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        pipeline = bb_ops.pipeline_show(client, workspace, repo_slug, number)
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "pipeline": pipeline}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, number=number)


@mcp.tool()
def pipeline_steps(number: int, repo: str = "") -> dict[str, Any]:
    """List the steps of a pipeline by build number."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        steps = bb_ops.pipeline_steps(client, workspace, repo_slug, number)
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "steps": steps}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, number=number)


@mcp.tool()
def pipeline_trigger(
    branch: str,
    repo: str = "",
    pattern: str = "",
    variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Trigger a new pipeline run.

    Args:
        branch: Branch ref name (e.g. "main", "feat/widget").
        repo: Repo slug, "workspace/slug", or "" to auto-detect.
        pattern: Custom pipeline name (matches `custom:` entries in
                 bitbucket-pipelines.yml). Empty for the branch's default
                 pipeline.
        variables: Dict of {name: value} pairs to pass as pipeline
                   variables. Values must be strings.
    """
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        # Strip the branch so " main" / "main " don't slip through to a
        # 4xx with an opaque body. bb_ops.pipeline_trigger checks
        # `if not branch` (catches empty) but not whitespace-only or
        # trailing whitespace — symmetric with _opt_str() everywhere
        # else, but required here (cannot funnel to None).
        normalised_branch = (branch or "").strip()
        if not normalised_branch:
            raise ValueError(
                f"branch is required and must be non-empty/non-whitespace; got {branch!r}"
            )
        pipeline = bb_ops.pipeline_trigger(
            client, workspace, repo_slug,
            branch=normalised_branch,
            pattern=_opt_str(pattern),
            variables=variables,
        )
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "pipeline": pipeline}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def pipeline_stop(number: int, repo: str = "") -> dict[str, Any]:
    """Stop a running pipeline by build number."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        result = bb_ops.pipeline_stop(client, workspace, repo_slug, number)
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "result": result}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, number=number)


@mcp.tool()
def pipeline_logs(
    number: int,
    step_index: int,
    repo: str = "",
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Fetch raw log text for a pipeline step (0-based step index).

    The log endpoint may return inline text or redirect to a signed S3
    URL; the underlying fetcher follows the redirect while stripping the
    Bitbucket Authorization header on cross-host hops.

    Args:
        number: Pipeline build number.
        step_index: 0-based step position.
        repo: Repo slug, "workspace/slug", or "" to auto-detect.
        timeout: Per-call timeout in seconds (default 120). Bump for
                 pipelines with very large log payloads.
    """
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        text = bb_ops.pipeline_logs(
            client, workspace, repo_slug, number, step_index,
            timeout=timeout,
        )
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "step_index": step_index,
            "log": text,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, number=number, step_index=step_index)


# =============================================================================
#  PULL REQUEST TOOLS
# =============================================================================


@mcp.tool()
def prs_list(
    repo: str = "", state: str = "OPEN", count: int = 25, verbose: bool = False
) -> dict[str, Any]:
    """List pull requests filtered by state.

    By default each PR is slimmed (drops the bulky description / summary
    / rendered / participants fields) so the response fits the MCP
    25k-token cap even on repos with rich PR bodies — the list/triage
    workflow only needs identity + state + branches + author + links,
    and the full body is one `pr_show` call away. Set verbose=True only
    if you specifically need the full PR objects in the list (rarely).

    Args:
        repo: Repo slug, "workspace/slug", or "" to auto-detect.
        state: OPEN, MERGED, DECLINED, or SUPERSEDED.
        count: Maximum number of PRs to return.
        verbose: If True, return full (unslimmed) PR objects.
    """
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        prs = bb_ops.prs_list(
            client, workspace, repo_slug, state=state, count=count, verbose=verbose
        )
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "prs": prs}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, state=state)


@mcp.tool()
def pr_show(pr_id: int, repo: str = "") -> dict[str, Any]:
    """Fetch a single pull request by ID."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        pr = bb_ops.pr_show(client, workspace, repo_slug, pr_id)
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "pr": pr}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, pr_id=pr_id)


@mcp.tool()
def pr_activity(pr_id: int, repo: str = "", count: int = 50) -> dict[str, Any]:
    """List PR activity stream (approvals, comments, state transitions)."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        activity = bb_ops.pr_activity(client, workspace, repo_slug, pr_id, count=count)
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "pr_id": pr_id,
            "activity": activity,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, pr_id=pr_id)


@mcp.tool()
def pr_create(
    title: str,
    source_branch: str = "",
    destination_branch: str = "main",
    repo: str = "",
    description: str = "",
    close_source_branch: bool = True,
    reviewers: list[str] | None = None,
) -> dict[str, Any]:
    """Create a pull request.

    Args:
        title: PR title (required).
        source_branch: Source branch name. If empty/whitespace,
                       auto-detected via `git rev-parse --abbrev-ref HEAD`.
                       Detached HEAD / unborn-branch states are rejected
                       (git returns "HEAD" as the branch literal — not a
                       valid PR source).
        destination_branch: Destination branch (default: "main").
        repo: Repo slug, "workspace/slug", or "" to auto-detect.
        description: PR description (markdown). Empty/whitespace omitted.
        close_source_branch: Delete the source branch on merge (default: True).
        reviewers: Optional list of reviewer Bitbucket UUIDs (each
                   wrapped as `{"uuid": "..."}` in the payload).
    """
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        # Normalise every string arg at the boundary. `(x or "").strip()`
        # handles None (JSON null), whitespace-only, and trailing/leading
        # whitespace uniformly. Symmetric with pipeline_trigger's branch
        # handling (round 3 fix #2) and _resolve_repo's repo handling
        # (round 2). Without this, "feat/widget " posts a 404, "Hi "
        # ships a title with literal trailing whitespace, etc.
        source_branch = (source_branch or "").strip()
        destination_branch = (destination_branch or "main").strip() or "main"
        normalised_title = (title or "").strip()
        normalised_description = (description or "").strip()
        if not normalised_title:
            raise ValueError(
                f"title is required and must be non-empty/non-whitespace; got {title!r}"
            )

        # Default source_branch to the current git branch when empty —
        # matches the bash `bb pr-create` behaviour.
        if not source_branch:
            source_branch = git_ops.git_current_branch(path=_default_repo_path())
        # Reject "HEAD" regardless of whether it came from auto-detect
        # or was supplied explicitly. Bitbucket would silently create a
        # degenerate PR named after the literal `HEAD` ref.
        if source_branch.strip() == "HEAD":
            raise ValueError(
                "source_branch cannot be 'HEAD' (detached HEAD / unborn "
                "branch state). Pass a real branch name explicitly."
            )
        pr = bb_ops.pr_create(
            client, workspace, repo_slug,
            title=normalised_title,
            source_branch=source_branch,
            destination_branch=destination_branch,
            description=normalised_description,
            close_source_branch=close_source_branch,
            reviewers=reviewers,
        )
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "pr": pr}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        # Thread title for parallel-call correlation (e.g. agent fanning
        # out one pr_create per stacked branch in a PR train). Use the
        # raw title so the agent can match against what it sent.
        return _error_dict_with(e, title=title)


@mcp.tool()
def pr_approve(pr_id: int, repo: str = "") -> dict[str, Any]:
    """Approve a pull request as the authenticated user."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        result = bb_ops.pr_approve(client, workspace, repo_slug, pr_id)
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "pr_id": pr_id,
            "approval": result,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, pr_id=pr_id)


@mcp.tool()
def pr_unapprove(pr_id: int, repo: str = "") -> dict[str, Any]:
    """Remove the authenticated user's approval from a PR."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        result = bb_ops.pr_unapprove(client, workspace, repo_slug, pr_id)
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "pr_id": pr_id,
            "result": result,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, pr_id=pr_id)


@mcp.tool()
def pr_merge(
    pr_id: int,
    repo: str = "",
    strategy: str = "merge_commit",
    close_source_branch: bool = True,
    message: str = "",
) -> dict[str, Any]:
    """Merge a pull request.

    Strategies: merge_commit (default), squash, fast_forward.
    """
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        result = bb_ops.pr_merge(
            client, workspace, repo_slug, pr_id,
            strategy=strategy,
            close_source_branch=close_source_branch,
            message=_opt_str(message),
        )
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "pr": result}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, pr_id=pr_id)


@mcp.tool()
def pr_decline(pr_id: int, repo: str = "") -> dict[str, Any]:
    """Decline (close without merging) a pull request."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        result = bb_ops.pr_decline(client, workspace, repo_slug, pr_id)
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "pr": result}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, pr_id=pr_id)


@mcp.tool()
def pr_diff(pr_id: int, repo: str = "", timeout: float = 120.0) -> dict[str, Any]:
    """Fetch the unified diff text for a pull request.

    Args:
        pr_id: Pull request ID.
        repo: Repo slug, "workspace/slug", or "" to auto-detect.
        timeout: Per-call timeout in seconds (default 120). Bump for
                 very large PR diffs.
    """
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        diff = bb_ops.pr_diff(client, workspace, repo_slug, pr_id, timeout=timeout)
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "pr_id": pr_id,
            "diff": diff,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, pr_id=pr_id)


@mcp.tool()
def pr_comments_list(pr_id: int, repo: str = "", count: int = 100) -> dict[str, Any]:
    """List comments on a pull request."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        comments = bb_ops.pr_comments_list(client, workspace, repo_slug, pr_id, count=count)
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "pr_id": pr_id,
            "comments": comments,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, pr_id=pr_id)


@mcp.tool()
def pr_comment_add(pr_id: int, body: str, repo: str = "") -> dict[str, Any]:
    """Add a top-level comment to a pull request."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        comment = bb_ops.pr_comment_add(client, workspace, repo_slug, pr_id, body)
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "pr_id": pr_id,
            "comment": comment,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, pr_id=pr_id)


# =============================================================================
#  WORKSPACES TOOL
# =============================================================================


@mcp.tool()
def workspaces_list(count: int = 100) -> dict[str, Any]:
    """List the Bitbucket workspaces the authenticated user belongs to.

    Uses the CHANGE-3022 endpoint GET /2.0/user/workspaces (the
    replacement for the cross-workspace listing endpoints removed
    under CHANGE-2770, effective 2026-04-14).

    Requires the `read:workspace:bitbucket` scope on the API token. A
    token granted only repository/pullrequest/pipeline scopes returns
    the standard error envelope `{"ok": False, "kind": "BBApiError",
    "status": 403, "body": ...}` (flat `ok`, NOT the `auth.ok` shape
    that's specific to the `whoami` tool). Bitbucket's "credentials
    lack one or more required privilege scopes" message is in `body` —
    exactly which scope to add is recoverable from there.

    Each workspace entry is a `workspace_access` envelope:
    `{administrator: bool, workspace: {slug, uuid, links, ...}}`.
    The legacy `name` and `permission` string fields are NOT present
    in the new schema — branch on `administrator` (bool) for
    role-style decisions.

    Args:
        count: Maximum number of workspaces to return (default 100).
    """
    try:
        client = _get_client()
        workspaces = bb_ops.workspaces_list(client, count=count)
        return {"ok": True, "workspaces": workspaces}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, count=count)


@mcp.tool()
def projects_list(workspace: str = "", count: int = 100) -> dict[str, Any]:
    """List the projects in a Bitbucket workspace.

    Each project record carries `.key` (the short key used in repo
    bodies, e.g. "WID"), `.name`, `.uuid`, and `.links`. The key is what
    `repo_create(project=...)` and `repo_update(project=...)` expect.

    Requires the `read:project:bitbucket` scope on the API token. A token
    without it returns the standard error envelope `{"ok": False, "kind":
    "BBApiError", "status": 403, "body": ...}`; Bitbucket's "credentials
    lack one or more required privilege scopes" message is in `body`, and
    the exact missing scope is under `error.detail.required`.

    Args:
        workspace: Workspace slug. Empty = use BB_WORKSPACE from config.
        count: Maximum number of projects to return (default 100).
    """
    try:
        client = _get_client()
        # Mirror repos_list: strip + fall back so " acme" / "acme " don't
        # build `/workspaces/%20acme/projects`; whitespace-only falls back
        # to the configured workspace.
        ws = (workspace or "").strip() or client.config.workspace
        projects = bb_ops.projects_list(client, workspace=ws, count=count)
        return {"ok": True, "workspace": ws, "projects": projects}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


# =============================================================================
#  REPO / BRANCH / VARS / DOWNLOADS / COMMITS TOOLS
# =============================================================================


@mcp.tool()
def repos_list(
    workspace: str = "",
    count: int = 100,
    sort: str = "-updated_on",
    query: str = "",
) -> dict[str, Any]:
    """List repositories in a workspace.

    Args:
        workspace: Workspace slug. Empty = use BB_WORKSPACE from config.
        count: Maximum number of repos to return.
        sort: Sort key (default: most-recently-updated first).
        query: Optional BBQL filter (e.g. 'name ~ "widget"').
    """
    try:
        client = _get_client()
        # Strip + fall back so " acme" / "acme " don't slip through and
        # end up as `/repositories/%20acme` (404). Whitespace-only
        # workspace falls back to the configured one.
        ws = (workspace or "").strip() or client.config.workspace
        repos = bb_ops.repos_list(
            client,
            workspace=ws,
            count=count,
            sort=sort,
            query=_opt_str(query),
        )
        return {"ok": True, "workspace": ws, "repos": repos}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def repo_show(repo: str = "") -> dict[str, Any]:
    """Fetch repository metadata (language, size, clone URLs, etc.)."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        info = bb_ops.repo_show(client, workspace, repo_slug)
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "info": info}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def repo_create(
    name: str,
    workspace: str = "",
    is_private: bool = True,
    project: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Create a new Bitbucket repository.

    Args:
        name: Repo slug to create (e.g. "widget-service").
        workspace: Workspace slug. Empty = use BB_WORKSPACE from config.
        is_private: Create as private (default True). Pass False for public.
        project: Optional Bitbucket project key. A workspace with any
            projects REQUIRES one; the create 400s without it (the error
            body names the constraint).
        description: Optional repository description.

    Returns the created repo record. The clone URL is surfaced as
    `clone_https` for convenience; the full record is under `info`.
    """
    try:
        client = _get_client()
        # Mirror repos_list: strip + fall back so " acme" / "acme " don't
        # build `/repositories/%20acme`; whitespace-only falls back to the
        # configured workspace.
        ws = (workspace or "").strip() or client.config.workspace
        info = bb_ops.repo_create(
            client,
            ws,
            name,
            is_private=is_private,
            project_key=_opt_str(project),
            description=_opt_str(description),
        )
        # Pull the https clone URL out of the links for the caller's
        # convenience (matches what the bash command prints).
        clone_https = None
        for link in (info.get("links", {}) or {}).get("clone", []) or []:
            if link.get("name") == "https":
                clone_https = link.get("href")
                break
        return {
            "ok": True,
            "workspace": ws,
            "repo": name,
            "clone_https": clone_https,
            "info": info,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict_with(e, repo=name)


@mcp.tool()
def repo_update(
    repo: str = "",
    project: str = "",
    description: str | None = None,
) -> dict[str, Any]:
    """Update an existing repository (move its project, change description).

    The dominant use is reassigning a repo's project: pass `project` with
    the target project KEY (e.g. "WID"). `repo_create` accepts a project
    on creation but nothing else could change it afterward — this closes
    that gap. At least one of `project` / `description` must be supplied.

    Requires `admin:repository:bitbucket` scope on the token (same as
    repo_create — changing repo settings is an admin operation).
    `write:repository:bitbucket` alone returns 403, whose body names the
    missing scope under `error.detail.required`.

    Args:
        repo: "", "slug", or "workspace/slug" (auto-detect when empty).
        project: Target project KEY to move the repo into. Empty = leave
            the project unchanged.
        description: New repository description. `null` (omit) = leave it
            unchanged; `""` (empty string) = intentionally CLEAR the
            description; any other string = set it. This three-way
            distinction matches the bash surface (`bb repo-update
            --description ""` clears), so the two surfaces have the same
            capability — a plain `_opt_str` collapse would silently turn a
            clear into a no-op.

    Returns the updated repo record under `info`, plus the resolved
    `project` key for convenience.
    """
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        info = bb_ops.repo_update(
            client,
            workspace,
            repo_slug,
            project_key=_opt_str(project),
            # description is passed through as-is (no _opt_str): None means
            # "no change", "" means "clear" — both are meaningful and must
            # reach bb_ops.repo_update distinctly.
            description=description,
        )
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "project": (info.get("project", {}) or {}).get("key"),
            "info": info,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def branches_list(
    repo: str = "",
    count: int = 50,
    sort: str = "-target.date",
    query: str = "",
) -> dict[str, Any]:
    """List branches in a repo, default sort is most-recently-updated first."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        branches = bb_ops.branches_list(
            client, workspace, repo_slug,
            count=count,
            sort=sort,
            query=_opt_str(query),
        )
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "branches": branches}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def branch_show(name: str, repo: str = "") -> dict[str, Any]:
    """Fetch a single branch by name. URL-encodes slashes in the name."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        branch = bb_ops.branch_show(client, workspace, repo_slug, name)
        # Echo the stripped name so the response matches what Bitbucket
        # actually resolved (bb_ops.branch_show strips before encoding).
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "name": name.strip(),
            "branch": branch,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


def _resolve_vars_scope(
    repo: str, scope: str, environment: str
) -> tuple[bb_api.BBClient, str, str | None, str | None]:
    """Resolve (client, workspace, repo_or_None, environment_or_None) for a
    variables operation at the given scope.

    - repo / deployment scopes need a repo, so they go through
      `_resolve_repo` (auto-detect or explicit ws/slug).
    - workspace scope has no repo. If `repo` carries a "ws/slug" or bare
      workspace hint we honour its workspace; otherwise we use the
      configured BB_WORKSPACE. A bare slug's workspace is the config
      workspace too, so passing a repo at the workspace scope just
      borrows its workspace and drops the slug.
    - the deployment scope requires a non-empty environment; the others
      reject it.
    """
    scope = (scope or "repo").strip() or "repo"
    if scope not in bb_ops._VARS_SCOPES:
        raise ValueError(
            f"scope must be one of {bb_ops._VARS_SCOPES}, got {scope!r}"
        )
    env = _opt_str(environment)

    if scope == "deployment" and env is None:
        raise ValueError("environment is required for the deployment scope")
    if scope != "deployment" and env is not None:
        raise ValueError("environment is only valid for the deployment scope")

    if scope == "workspace":
        # No repo for workspace scope. Resolve the workspace the same way
        # the bash `resolve_workspace` does: a repo hint wins, else the git
        # origin of the working dir, else the configured BB_WORKSPACE. The
        # git-origin step matters for parity: `bb vars --workspace` inside
        # an `acme/widget` checkout with no BB_WORKSPACE set resolves to
        # `acme`, so the MCP tool must too rather than erroring.
        if (repo or "").strip():
            client, workspace, _slug = _resolve_repo(repo)
            return client, workspace, None, None
        client = _get_client()
        # Try the git origin of the default repo path (mirrors _resolve_repo's
        # auto-detect arm). A non-git dir / no-origin raises GitOpError,
        # which we swallow to fall back to the configured workspace.
        workspace = ""
        try:
            workspace, _slug = git_ops.git_remote_repo(path=_default_repo_path())
        except _TOOL_EXPECTED_EXCEPTIONS:
            workspace = ""
        if not workspace:
            workspace = client.config.workspace
        if not workspace:
            raise ValueError(
                "no workspace for workspace-scope variables: set "
                "BB_WORKSPACE, run inside a Bitbucket git checkout, or pass "
                "repo='workspace/anything' to supply one."
            )
        return client, workspace, None, None

    client, workspace, repo_slug = _resolve_repo(repo)
    return client, workspace, repo_slug, env


@mcp.tool()
def vars_list(
    repo: str = "",
    count: int = 100,
    scope: str = "repo",
    environment: str = "",
) -> dict[str, Any]:
    """List pipeline configuration variables at a chosen scope.

    Args:
        repo: Repo slug, "workspace/slug", or "" to auto-detect. Ignored
            for scope="workspace" (which has no repo) except as a way to
            borrow a workspace.
        count: Maximum number of variables to return.
        scope: "repo" (default), "workspace", or "deployment".
        environment: Deployment environment NAME or slug. Required when
            scope="deployment"; resolved to its UUID. Rejected otherwise.

    Secured values come back as null from Bitbucket; the `secured` flag
    distinguishes that case."""
    try:
        client, workspace, repo_slug, env = _resolve_vars_scope(
            repo, scope, environment
        )
        variables = bb_ops.vars_list(
            client, workspace, repo_slug,
            count=count, scope=scope.strip() or "repo", environment=env,
        )
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "scope": scope.strip() or "repo",
            "environment": env,
            "variables": variables,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


# `value` not-supplied marker for vars_set. An empty string is a LEGAL
# pipeline-variable value (e.g. clearing a feature flag), so "" can't
# double as "value not supplied" (that would make an empty value
# unsettable via MCP while the bash CLI accepts `--value ""`). We use
# `None` (JSON `null`) as the not-supplied marker rather than a magic
# string default: a string sentinel gets serialized into the tool's JSON
# inputSchema as a default, and a NUL-bearing or otherwise-odd literal
# can be normalized away by a strict MCP client, silently collapsing the
# empty-vs-unset distinction. `None` is a first-class JSON value with no
# such hazard, and an explicit "" is still distinct from it.


@mcp.tool()
def vars_set(
    key: str,
    repo: str = "",
    value: str | None = None,
    value_file: str = "",
    value_env: str = "",
    secured: bool = False,
    scope: str = "repo",
    environment: str = "",
) -> dict[str, Any]:
    """Create or update a pipeline variable at a chosen scope (create-or-update).

    Provide the value via EXACTLY ONE of `value`, `value_file`, or
    `value_env`. For SECRET values prefer `value_file` (read from a file
    on disk) or `value_env` (read from an environment variable) so the
    secret never appears inline in the tool-call arguments / agent
    transcript / process list.

    Args:
        key: Variable name (e.g. "AWS_SECRET").
        repo: Repo slug, "workspace/slug", or "" to auto-detect. Ignored
            for scope="workspace".
        value: Literal value. Use only for NON-secret values.
        value_file: Path to a file whose contents (one trailing newline
            stripped) become the value. Preferred for secrets.
        value_env: Name of an environment variable whose value is used.
            Preferred for secrets.
        secured: When True, mark the variable secured (Bitbucket masks it
            and never echoes it back). Default False.
        scope: "repo" (default), "workspace", or "deployment".
        environment: Deployment environment NAME or slug. Required when
            scope="deployment"; resolved to its UUID. Rejected otherwise.

    Secret hygiene: the value is NEVER echoed in the response. The
    response reports the key, secured flag, and whether the operation
    created or updated the variable — but masks the value as "***".
    """
    try:
        scope_norm = (scope or "repo").strip() or "repo"

        # Validate the value source BEFORE resolving the repo. _resolve_repo
        # can shell out to git (auto-detect path); an obviously invalid call
        # (no source, two sources) should error without paying that cost,
        # matching the bash CLI which checks sources first.
        #
        # `value` is "supplied" iff it's not None (the not-supplied marker),
        # so an explicit empty string counts as supplied (parity with bash
        # `--value ""`).
        value_supplied = value is not None
        sources = [
            ("value", value_supplied),
            ("value_file", (value_file or "").strip() != ""),
            ("value_env", (value_env or "").strip() != ""),
        ]
        supplied = [name for name, present in sources if present]
        if len(supplied) != 1:
            raise ValueError(
                "provide exactly one of value / value_file / value_env "
                f"(got: {supplied or 'none'})"
            )

        if value_supplied:
            resolved_value = value
        elif (value_file or "").strip():
            path = value_file.strip()
            # Read as text; strip a single trailing newline so a value
            # piped into a file (`echo secret > f`) doesn't carry the
            # newline into Bitbucket. Other whitespace is preserved (a
            # value could legitimately contain leading spaces).
            try:
                with open(path, encoding="utf-8") as fh:
                    resolved_value = fh.read()
            except OSError as e:
                # Re-raise as OSError (already in _TOOL_EXPECTED_EXCEPTIONS).
                raise OSError(f"could not read value_file {path!r}: {e}") from e
            if resolved_value.endswith("\n"):
                resolved_value = resolved_value[:-1]
        else:
            env_name = value_env.strip()
            if env_name not in os.environ:
                raise ValueError(
                    f"value_env {env_name!r} is not set in the environment"
                )
            resolved_value = os.environ[env_name]

        # Sources are valid; now resolve the repo/workspace/scope (may shell
        # out to git for the auto-detect path).
        client, workspace, repo_slug, env = _resolve_vars_scope(
            repo, scope, environment
        )

        # Build the collection base ONCE (the deployment scope resolves an
        # environment NAME->UUID here via a GET). Reuse it for both the
        # existence check and the write so the deployment path doesn't
        # list environments twice — matches the bash CLI's single
        # resolution into `$base`.
        base = bb_ops._variables_base(
            client, workspace, repo_slug, scope=scope_norm, environment=env
        )
        existing = bb_ops._find_var_by_key_at(client, base, key.strip())
        action = "updated" if existing is not None else "created"

        result = bb_ops.vars_set(
            client, workspace, repo_slug, key, resolved_value,
            secured=secured, scope=scope_norm, environment=env, base=base,
            existing=existing,
        )
        # NEVER surface the value. Report the key + secured flag + action.
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "scope": scope_norm,
            "environment": env,
            "key": key.strip(),
            "secured": bool(secured),
            "action": action,
            "value": "***",
            "uuid": result.get("uuid") if isinstance(result, dict) else None,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        # Report the stripped key so error and success envelopes agree (an
        # agent correlating retries by key string sees the same value on
        # both paths). key may not be a str on a malformed call, so guard.
        return _error_dict_with(
            e, key=key.strip() if isinstance(key, str) else key
        )


@mcp.tool()
def downloads_list(repo: str = "", count: int = 25) -> dict[str, Any]:
    """List repository download artifacts."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        downloads = bb_ops.downloads_list(client, workspace, repo_slug, count=count)
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "downloads": downloads}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def commits_list(repo: str = "", branch: str = "", count: int = 10) -> dict[str, Any]:
    """List recent commits.

    Args:
        repo: Repo slug, "workspace/slug", or "" to auto-detect.
        branch: Branch name. Empty = all branches via /commits.
        count: Maximum number of commits to return.
    """
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        normalised_branch = _opt_str(branch)
        commits = bb_ops.commits_list(
            client, workspace, repo_slug,
            branch=normalised_branch,
            count=count,
        )
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "branch": normalised_branch,
            "commits": commits,
        }
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


# =============================================================================
#  GIT CONTEXT TOOLS
# =============================================================================


@mcp.tool()
def git_current_branch(path: str = "") -> dict[str, Any]:
    """Return the current git branch name. Detached HEAD returns "HEAD"."""
    try:
        cwd = path or _default_repo_path()
        branch = git_ops.git_current_branch(path=cwd)
        return {"ok": True, "path": cwd, "branch": branch}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def git_status(path: str = "") -> dict[str, Any]:
    """Return structured working-tree state (branch / upstream / ahead / behind /
    clean / staged / modified / untracked / unmerged + *_omitted caps).

    The payload is keyed under `working_tree` rather than `status` to
    avoid collision with the `status` field _error_dict uses for HTTP
    status codes on BBApiError. Today the collision can't fire
    (git_status doesn't raise BBApiError), but the rename pre-empts a
    future broadening hazard.
    """
    try:
        cwd = path or _default_repo_path()
        status = git_ops.git_status(path=cwd)
        return {"ok": True, "path": cwd, "working_tree": status}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def git_remote_repo(path: str = "") -> dict[str, Any]:
    """Return (workspace, repo_slug) parsed from the `origin` remote URL."""
    try:
        cwd = path or _default_repo_path()
        workspace, repo_slug = git_ops.git_remote_repo(path=cwd)
        return {"ok": True, "path": cwd, "workspace": workspace, "repo": repo_slug}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def git_recent_commits(path: str = "", count: int = 10, ref: str = "HEAD") -> dict[str, Any]:
    """List the most recent `count` commits reachable from `ref`."""
    try:
        cwd = path or _default_repo_path()
        # `(ref or "").strip() or "HEAD"`: None, "", and "   " all
        # funnel to the HEAD default. Previously whitespace-only collapsed
        # to "" then errored, inconsistent with the empty-string success
        # path. Same shape as `_opt_str` but with a fallback rather than
        # None (ref is required by git_ops).
        stripped_ref = (ref or "").strip() or "HEAD"
        commits = git_ops.git_recent_commits(path=cwd, count=count, ref=stripped_ref)
        return {"ok": True, "path": cwd, "ref": stripped_ref, "commits": commits}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def git_uncommitted_changes(path: str = "") -> dict[str, Any]:
    """Return staged diff, working diff, and untracked-file list. Diffs
    are capped at 1 MiB each; the untracked list is capped at 10000
    entries with the omitted count in `untracked_files_omitted`."""
    try:
        cwd = path or _default_repo_path()
        changes = git_ops.git_uncommitted_changes(path=cwd)
        return {"ok": True, "path": cwd, "changes": changes}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


# =============================================================================
#  META TOOLS
# =============================================================================


@mcp.tool()
def whoami() -> dict[str, Any]:
    """Report the resolved Bitbucket user, workspace, API base, the
    auto-detected git context for the current working directory, and a
    workspace-reachability probe that confirms the credential reaches
    the configured workspace.

    Does NOT echo the token.

    Three-phase: (1) config (fatal — flips ok=False on failure);
    (2) git context (best-effort — failures stored as structured
    sub-errors but don't flip ok=False, since the server is useful
    even outside a git repo); (3) workspace reachability via a single
    low-cost `GET /repositories/{workspace}?pagelen=1` with a 10 s
    timeout (best-effort — failures recorded as `auth` payload but
    don't flip ok=False, since config + git context are still useful
    with a stale token).

    The probed workspace is the configured BB_WORKSPACE, or — since
    BB_WORKSPACE is optional as of v1.2.0 — the git-detected workspace
    from Phase 2 as a fallback. If neither resolves, the probe is
    SKIPPED (`auth.ok = None`, with a `skipped` reason) rather than
    probing an empty workspace, which would hit the global
    `/repositories/` endpoint and falsely report `auth.ok = True`.
    `auth` carries the `workspace` it actually probed. This mirrors the
    bash cmd_whoami behavior exactly.

    The reachability probe targets the workspace endpoint (not /user)
    because Atlassian's workspace-scoped tokens — the now-recommended
    shape — reject /user with 401/403 while serving the workspace
    endpoint correctly, so a /user probe would false-negative valid
    tokens. Note the converse trade-off: this endpoint requires
    `repository:read` scope, so a workspace-scoped token granting only
    `pipelines:read` or `pullrequest:read` will surface as
    `auth.ok=False` even though pipeline / PR ops still work. No
    single endpoint covers every scope; treat `auth.ok=False` as a
    "this scope probably can't do repo listing" signal rather than as
    a global credential verdict.
    """
    out: dict[str, Any] = {"ok": True}

    # Phase 1: config. Wrap the full breadth of expected exceptions
    # (including OSError for os.getcwd-on-deleted-cwd inside the
    # whoami body itself).
    client: bb_api.BBClient | None = None
    try:
        client = _get_client()
        out["user"] = client.config.user
        out["workspace"] = client.config.workspace
        out["api_base"] = client.config.api_base
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        err = _error_dict(e)
        out.update({"ok": False, **err})

    # Phase 2: git context. Use _TOOL_EXPECTED_EXCEPTIONS (not narrow
    # GitOpError) so an unwrapped OSError from a deleted cwd inside
    # _default_repo_path() lands here instead of escaping. Store
    # failures as the full structured error dict so an agent
    # branching on returncode / kind / stderr has the same shape
    # available as every other tool.
    try:
        cwd = _default_repo_path()
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        out["cwd_error"] = _error_dict(e)
        cwd = None
    else:
        out["cwd"] = cwd

    if cwd is not None:
        try:
            out["git_branch"] = git_ops.git_current_branch(path=cwd)
        except _TOOL_EXPECTED_EXCEPTIONS as e:
            out["git_branch_error"] = _error_dict(e)
        try:
            ws, slug = git_ops.git_remote_repo(path=cwd)
            out["git_workspace"] = ws
            out["git_repo"] = slug
        except _TOOL_EXPECTED_EXCEPTIONS as e:
            out["git_remote_error"] = _error_dict(e)

    # Phase 3: workspace reachability. Skip if Phase 1 failed (no client
    # to probe with). Single cheap GET; success means the credential is
    # valid for the probed workspace right now.
    #
    # BB_WORKSPACE is optional as of v1.2.0, so config.workspace can be
    # "". Probing an empty workspace would build `GET /repositories/`,
    # which hits the GLOBAL public-repositories endpoint and returns 200
    # — a false-positive "auth OK" that verified nothing. Mirror the bash
    # cmd_whoami: pick the configured workspace, else the git-detected one
    # (already resolved in Phase 2 as out["git_workspace"]), and skip the
    # probe with an explicit ok=None when neither resolves rather than
    # probing a bad URL.
    if client is not None:
        probe_ws = client.config.workspace or out.get("git_workspace", "")
        if not probe_ws:
            out["auth"] = {
                "ok": None,
                "skipped": "no workspace to probe — set BB_WORKSPACE or run "
                "inside a Bitbucket git checkout. Config + token loaded OK.",
            }
        else:
            try:
                client.get(
                    f"/repositories/{urllib.parse.quote(probe_ws, safe='')}",
                    query={"pagelen": "1"},
                    timeout=10.0,
                )
                out["auth"] = {"ok": True, "workspace": probe_ws}
            except _TOOL_EXPECTED_EXCEPTIONS as e:
                out["auth"] = {"ok": False, "workspace": probe_ws, **_error_dict(e)}

    return out


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
