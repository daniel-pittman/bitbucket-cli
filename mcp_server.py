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
    /usr/bin/python3 mcp_server.py

The script self-bootstraps /tmp/bbenv (mcp package) on first run and on
reboot when /tmp is wiped, then re-execs under that venv. Any python3 on
PATH that can run `python3 -m venv` works as the launcher.

Register user-scope so every Claude Code session sees it:
    claude mcp add --scope user bb \\
        /usr/bin/python3 \\
        /path/to/bitbucket-cli/mcp_server.py

Environment overrides:
  BB_USER, BB_TOKEN, BB_WORKSPACE — auth + workspace (see bb_api docs)
  BB_API_BASE                     — Bitbucket REST base (default api.bitbucket.org/2.0)
  BB_DEFAULT_REPO_PATH            — git checkout dir for auto-detect (default: cwd)
  BB_MCP_SKIP_BOOTSTRAP=1         — test escape hatch (skips venv + stubs FastMCP)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Self-bootstrap: /tmp gets wiped on reboot. If our venv is missing, build it
# with stdlib-only code and re-exec under it. Must run before any third-party
# import (mcp).
# ---------------------------------------------------------------------------

_VENV_DIR = Path("/tmp/bbenv")
_VENV_PY = _VENV_DIR / "bin" / "python3"
# Sentinel file written ONLY after the full bootstrap (venv create + pip
# install) succeeds. If pip is Ctrl-C'd / OOM-killed / disk-full mid-run,
# _VENV_PY exists but `mcp` doesn't — without this sentinel, every
# subsequent launch would silently skip reinstall, re-exec into the
# broken venv, and die on `from mcp.server.fastmcp import FastMCP`.
_VENV_READY = _VENV_DIR / ".bbenv-ready"
# Pin to mcp>=1.0,<2 so a breaking mcp 2.x release doesn't silently
# install on the next /tmp wipe and break every fresh launch. Matches
# the pyproject.toml [mcp] extra.
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


def _bootstrap_venv() -> None:
    """Create /tmp/bbenv on first run, install deps, re-exec under it.

    Idempotent: a fully-bootstrapped venv (sentinel file present)
    re-execs immediately. A partially-bootstrapped venv (venv exists
    but sentinel doesn't — e.g. previous pip install was Ctrl-C'd or
    OOM-killed) gets the pip install retried with no manual cleanup
    needed.
    """
    if not _VENV_READY.exists():
        builder = _find_builder_python()
        # Log to stderr so MCP stdio transport isn't corrupted.
        print(
            f"[bb-mcp] bootstrapping {_VENV_DIR} with {builder}",
            file=sys.stderr,
        )
        # Only create the venv if it doesn't already exist (a previous
        # half-finished bootstrap left _VENV_PY in place).
        if not _VENV_PY.exists():
            subprocess.check_call([builder, "-m", "venv", str(_VENV_DIR)])
        subprocess.check_call(
            [str(_VENV_PY), "-m", "pip", "install",
             "--quiet", "--no-cache-dir", "--upgrade", "pip"]
        )
        subprocess.check_call(
            [str(_VENV_PY), "-m", "pip", "install",
             "--quiet", "--no-cache-dir", *_VENV_DEPS]
        )
        # Sentinel last — any earlier failure leaves it absent so the
        # next launch retries the install.
        _VENV_READY.touch()

    # Detect "are we already running under the bootstrap venv?" via
    # sys.prefix rather than realpath(sys.executable). `python -m venv`
    # on Linux/macOS defaults to --symlinks, so realpath(/tmp/bbenv/bin/
    # python3) resolves to the SAME canonical path as the builder
    # interpreter (e.g. /usr/bin/python3.12). Comparing realpaths would
    # claim "already under venv" when we're actually still under the
    # system interpreter, skipping the execv and dying on the mcp
    # import. sys.prefix is set per-interpreter from the venv layout
    # and is the authoritative signal.
    if sys.prefix != str(_VENV_DIR):
        os.execv(str(_VENV_PY), [str(_VENV_PY), __file__, *sys.argv[1:]])


# Test-mode escape hatch: setting BB_MCP_SKIP_BOOTSTRAP=1 in the environment
# skips the venv bootstrap AND substitutes a minimal FastMCP stub for the
# import below. This lets the pytest suite exercise tool wiring + result-dict
# shapes without pulling in `mcp`. Production (the actual MCP server
# transport) must NEVER set this — without the real FastMCP, the server
# doesn't serve.
_MCP_SKIP_BOOTSTRAP = os.environ.get("BB_MCP_SKIP_BOOTSTRAP", "") == "1"

if not _MCP_SKIP_BOOTSTRAP:
    _bootstrap_venv()
    from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]
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
    """
    return os.environ.get("BB_DEFAULT_REPO_PATH", os.getcwd())


def _resolve_repo(repo: str = "") -> tuple[bb_api.BBClient, str, str]:
    """Resolve (client, workspace, repo_slug) from a single repo argument.

    Accepted shapes for `repo`:
      - ""               → auto-detect via `git remote get-url origin` from
                           BB_DEFAULT_REPO_PATH (or cwd). Workspace + slug
                           come from the remote URL.
      - "myrepo"         → use config workspace (BB_WORKSPACE) + "myrepo"
      - "acme/myrepo"    → use "acme" workspace + "myrepo" slug (overrides
                           BB_WORKSPACE for this call)

    Whitespace is stripped before parsing so a sloppy paste or
    agent-side string concat ("  acme/widget  ") doesn't slip through
    as workspace="  acme" and surface as a deep API failure.

    Raises bb_api.BBConfigError on missing config or unresolvable remote.
    Raises ValueError on malformed `repo` argument.
    """
    client = _get_client()
    repo = repo.strip()

    if not repo:
        # Auto-detect from git remote.
        workspace, repo_slug = git_ops.git_remote_repo(path=_default_repo_path())
        return client, workspace, repo_slug

    if "/" in repo:
        parts = repo.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                f"repo must be 'workspace/repo' or 'repo'; got {repo!r}"
            )
        return client, parts[0], parts[1]

    # Bare slug → use configured workspace.
    return client, client.config.workspace, repo


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------

# Every tool returns either {"ok": True, ...result} or {"ok": False, ...error}.
# Keeping a consistent shape means the MCP agent can branch on `ok` once and
# render the result vs. error path uniformly.

def _error_dict(e: Exception) -> dict[str, Any]:
    """Translate any tool-side exception into a structured error dict.

    The agent sees `kind`, `message`, and (for BBApiError) the HTTP status
    + URL so it can branch on `kind == "BBApiError" and status == 404`
    without parsing the message string.
    """
    kind = type(e).__name__
    out: dict[str, Any] = {"ok": False, "kind": kind, "message": str(e)}
    if isinstance(e, bb_api.BBApiError):
        out["status"] = e.status
        out["url"] = e.url
        out["body"] = e.body
    elif isinstance(e, git_ops.GitOpError):
        out["returncode"] = e.returncode
        out["stderr"] = e.stderr
    return out


# Exceptions every tool wraps. Other exceptions propagate (they're
# programmer errors and should crash visibly during development).
# OSError covers IsADirectoryError, ConnectionResetError, BlockingIOError,
# and a few other paths that git_ops._run_git doesn't wrap explicitly
# (only FileNotFoundError / NotADirectoryError / PermissionError do).
# Also catches os.getcwd() on a deleted cwd, which fires inside
# _default_repo_path() BEFORE any wrapped git call.
_TOOL_EXPECTED_EXCEPTIONS = (
    bb_api.BBApiError,
    bb_api.BBConfigError,
    bb_ops.BBOpNotFound,
    git_ops.GitOpError,
    OSError,
    ValueError,
    TypeError,
)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("bb")


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
            branch=branch or None,
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
        return _error_dict(e)


@mcp.tool()
def pipeline_steps(number: int, repo: str = "") -> dict[str, Any]:
    """List the steps of a pipeline by build number."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        steps = bb_ops.pipeline_steps(client, workspace, repo_slug, number)
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "steps": steps}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


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
        pipeline = bb_ops.pipeline_trigger(
            client, workspace, repo_slug,
            branch=branch,
            pattern=pattern or None,
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
        return _error_dict(e)


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
        return _error_dict(e)


# =============================================================================
#  PULL REQUEST TOOLS
# =============================================================================


@mcp.tool()
def prs_list(repo: str = "", state: str = "OPEN", count: int = 25) -> dict[str, Any]:
    """List pull requests filtered by state.

    Args:
        repo: Repo slug, "workspace/slug", or "" to auto-detect.
        state: OPEN, MERGED, DECLINED, or SUPERSEDED.
        count: Maximum number of PRs to return.
    """
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        prs = bb_ops.prs_list(client, workspace, repo_slug, state=state, count=count)
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "prs": prs}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def pr_show(pr_id: int, repo: str = "") -> dict[str, Any]:
    """Fetch a single pull request by ID."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        pr = bb_ops.pr_show(client, workspace, repo_slug, pr_id)
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "pr": pr}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


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
        return _error_dict(e)


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
        # Default source_branch to the current git branch when
        # empty/whitespace — matches the bash `bb pr-create` behaviour.
        # `.strip()` so " " (sloppy whitespace) doesn't bypass auto-detect.
        if not source_branch.strip():
            source_branch = git_ops.git_current_branch(path=_default_repo_path())
            # `git_current_branch` returns the literal "HEAD" for both
            # detached and unborn state. Bitbucket would accept this
            # silently and create a degenerate PR; surface a clear local
            # error instead.
            if source_branch == "HEAD":
                raise ValueError(
                    "cannot auto-detect source_branch: git reports detached "
                    "HEAD / unborn branch. Pass source_branch= explicitly."
                )
        pr = bb_ops.pr_create(
            client, workspace, repo_slug,
            title=title,
            source_branch=source_branch,
            destination_branch=destination_branch,
            description=description,
            close_source_branch=close_source_branch,
            reviewers=reviewers,
        )
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "pr": pr}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


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
        return _error_dict(e)


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
        return _error_dict(e)


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
            message=message or None,
        )
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "pr": result}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


@mcp.tool()
def pr_decline(pr_id: int, repo: str = "") -> dict[str, Any]:
    """Decline (close without merging) a pull request."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        result = bb_ops.pr_decline(client, workspace, repo_slug, pr_id)
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "pr": result}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


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
        return _error_dict(e)


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
        return _error_dict(e)


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
        ws = workspace or client.config.workspace
        repos = bb_ops.repos_list(
            client,
            workspace=ws,
            count=count,
            sort=sort,
            query=query or None,
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
            query=query or None,
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


@mcp.tool()
def vars_list(repo: str = "", count: int = 100) -> dict[str, Any]:
    """List pipeline configuration variables. Secured values come back
    as null from Bitbucket; the `secured` flag distinguishes that case."""
    try:
        client, workspace, repo_slug = _resolve_repo(repo)
        variables = bb_ops.vars_list(client, workspace, repo_slug, count=count)
        return {"ok": True, "workspace": workspace, "repo": repo_slug, "variables": variables}
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        return _error_dict(e)


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
        commits = bb_ops.commits_list(
            client, workspace, repo_slug,
            branch=branch or None,
            count=count,
        )
        return {
            "ok": True,
            "workspace": workspace,
            "repo": repo_slug,
            "branch": branch or None,
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
        commits = git_ops.git_recent_commits(path=cwd, count=count, ref=ref)
        return {"ok": True, "path": cwd, "ref": ref, "commits": commits}
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
    """Report the resolved Bitbucket user, workspace, API base, and the
    auto-detected git context for the current working directory.

    Useful as a connectivity / config smoke test before more invasive
    operations. Does NOT echo the token.
    """
    out: dict[str, Any] = {"ok": True}
    try:
        client = _get_client()
        out["user"] = client.config.user
        out["workspace"] = client.config.workspace
        out["api_base"] = client.config.api_base
    except _TOOL_EXPECTED_EXCEPTIONS as e:
        # Config error is half-fatal — the user still wants to know what
        # we tried to load. Report partial state with kind/message.
        err = _error_dict(e)
        out.update({"ok": False, **err})

    # Best-effort git context. Failures here don't flip ok=False (the
    # MCP server is useful even outside a git repo).
    cwd = _default_repo_path()
    out["cwd"] = cwd
    try:
        out["git_branch"] = git_ops.git_current_branch(path=cwd)
    except git_ops.GitOpError as e:
        out["git_branch_error"] = str(e)
    try:
        ws, slug = git_ops.git_remote_repo(path=cwd)
        out["git_workspace"] = ws
        out["git_repo"] = slug
    except git_ops.GitOpError as e:
        out["git_remote_error"] = str(e)

    return out


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
