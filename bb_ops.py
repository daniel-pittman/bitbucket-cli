"""
bb_ops — Bitbucket REST operations grouped by resource.

The MCP server (mcp_server.py, future PR) wires each function here to a
tool. Each function takes a BBClient as its first positional argument and
returns native Python data (dicts, lists, strings) — no terminal-style
formatting, no colour codes, no parsing of bash output.

`bb` (bash) and bb_ops (Python) are parallel implementations of the same
Bitbucket REST contract. See CONTRIBUTING.md for the parity rule: when a
defect surfaces in either side, the fix lands in both code paths.

This PR (Phase 4.2) adds pipeline operations. Subsequent PRs add PRs,
repos, branches, and git context.
"""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import quote

from bb_api import BBApiError, BBClient, repo_path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Bitbucket Cloud caps pagelen at 100 server-side. Asking for more is
# silently truncated, which would create a confusing partial result. Clamp
# explicitly so the caller's intent (cap N items, paginate if needed) is
# preserved.
_BITBUCKET_MAX_PAGELEN = 100

# When resolving a build_number -> uuid, we walk the pipelines list sorted
# by most-recent-first. This cap bounds how far back we look before giving
# up. 2000 = 20 pages of 100 = "any pipeline triggered in the last few
# months" for an active repo. The bash script's inline lookup is limited
# to a single 50-pipeline page, so this is a substantive parity improvement
# (parity follow-up for 4.7: fix bash to paginate the same way).
_PIPELINE_SCAN_LIMIT = 2000


class BBOpNotFound(LookupError):
    """A requested resource (pipeline build_number, step index, etc.) was
    not present in the responses we walked. Distinct from BBApiError so
    callers can render "no such pipeline" vs "API failure" differently."""


def _wrap_uuid(uuid: str) -> str:
    """Bitbucket's URL contract uses `{uuid}` with the literal braces,
    URL-encoded as `%7B...%7D`. The bash script does this by interpolating
    `%7B${uuid}%7D` directly into curl URLs; mirror that. The UUID itself
    is alphanumeric+hyphens so `quote` would no-op on it, but we route
    through it to defend against a UUID that ever contains characters
    that would otherwise need encoding."""
    inner = uuid.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    return f"%7B{quote(inner, safe='-')}%7D"


def _pipelines_root(workspace: str, repo: str) -> str:
    """Common URL prefix for the pipelines API. Centralising this means a
    future API-version bump touches one place."""
    return f"{repo_path(workspace, repo)}/pipelines/"


def _strip_uuid_braces(uuid: str | None) -> str:
    """Bitbucket returns pipeline UUIDs in two shapes depending on endpoint:
    bare ('a1b2-...') and brace-wrapped ('{a1b2-...}'). Normalise to bare
    so callers don't have to care."""
    if not uuid:
        raise BBApiError(0, "", "response missing uuid")
    s = uuid.strip()
    if s.startswith("{") and s.endswith("}"):
        return s[1:-1]
    return s


# ---------------------------------------------------------------------------
# Resolution helpers (build_number -> uuid, step_index -> uuid)
# ---------------------------------------------------------------------------


def _resolve_pipeline_uuid(
    client: BBClient,
    workspace: str,
    repo: str,
    build_number: int,
    *,
    scan_limit: int = _PIPELINE_SCAN_LIMIT,
) -> str:
    """Resolve a pipeline's UUID by walking pipelines/ sorted by most-recent.

    Bitbucket Cloud's API does not expose a direct `GET /pipelines/{build_number}`
    endpoint, only `GET /pipelines/{uuid}`. The CLI passes a build_number
    because that's what's user-visible (and what the API echoes in payloads).
    This helper paginates the listing until it finds the matching build or
    `scan_limit` items have been examined.

    Raises BBOpNotFound if the build_number isn't found within the scan
    window. Distinct from a network/API failure so callers can render
    "no such pipeline #N" naturally.
    """
    if not isinstance(build_number, int) or build_number < 1:
        raise ValueError(f"build_number must be a positive int, got {build_number!r}")

    seen = 0
    path = _pipelines_root(workspace, repo)
    query = {"sort": "-created_on", "pagelen": _BITBUCKET_MAX_PAGELEN}
    for pipeline in client.paginate(path, query=query):
        seen += 1
        if pipeline.get("build_number") == build_number:
            return _strip_uuid_braces(pipeline.get("uuid"))
        if seen >= scan_limit:
            break
    raise BBOpNotFound(
        f"pipeline #{build_number} not found within the {scan_limit} most-recent "
        f"pipelines of {workspace}/{repo}"
    )


def _resolve_step_uuid(
    client: BBClient,
    workspace: str,
    repo: str,
    pipeline_uuid: str,
    step_index: int,
) -> tuple[str, str]:
    """Return (step_uuid, step_name) for the step at the given 0-based index.

    The bash script's `bb logs` uses the same 0-based indexing into the
    steps list; mirror that contract so the user-facing index numbers
    match across both surfaces.
    """
    if not isinstance(step_index, int) or step_index < 0:
        raise ValueError(f"step_index must be a non-negative int, got {step_index!r}")

    steps = pipeline_steps_raw(client, workspace, repo, pipeline_uuid)
    if step_index >= len(steps):
        raise BBOpNotFound(
            f"step index {step_index} out of range "
            f"(pipeline has {len(steps)} step{'s' if len(steps) != 1 else ''})"
        )
    step = steps[step_index]
    return _strip_uuid_braces(step.get("uuid")), step.get("name", "")


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def pipelines_list(
    client: BBClient,
    workspace: str,
    repo: str,
    *,
    count: int = 10,
    sort: str = "-created_on",
    branch: str | None = None,
) -> list[dict[str, Any]]:
    """List recent pipelines, most-recent first by default.

    `count` is the upper bound on returned items. We always honour it
    even if it exceeds Bitbucket's per-page cap (100): the function
    paginates as needed.

    `branch` filters to pipelines triggered against a specific branch via
    Bitbucket's `target.ref_name` query (the API supports this without a
    `?q=` filter shape).
    """
    if not isinstance(count, int) or count < 1:
        raise ValueError(f"count must be a positive int, got {count!r}")

    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    query: dict[str, Any] = {"sort": sort, "pagelen": pagelen}
    if branch is not None:
        query["target.ref_name"] = branch

    out: list[dict[str, Any]] = []
    for pipeline in client.paginate(_pipelines_root(workspace, repo), query=query):
        out.append(pipeline)
        if len(out) >= count:
            break
    return out


def pipeline_show(
    client: BBClient, workspace: str, repo: str, build_number: int
) -> dict[str, Any]:
    """Fetch full pipeline detail for the given build_number."""
    uuid = _resolve_pipeline_uuid(client, workspace, repo, build_number)
    return client.get(f"{_pipelines_root(workspace, repo)}{_wrap_uuid(uuid)}")


def pipeline_steps_raw(
    client: BBClient, workspace: str, repo: str, pipeline_uuid: str
) -> list[dict[str, Any]]:
    """Internal: list steps when you already have a pipeline UUID. Used by
    `_resolve_step_uuid` (which already paid the build_number→uuid lookup)
    to avoid a second list-pipelines walk."""
    uuid = _strip_uuid_braces(pipeline_uuid)
    path = f"{_pipelines_root(workspace, repo)}{_wrap_uuid(uuid)}/steps/"
    return list(client.paginate(path, query={"pagelen": _BITBUCKET_MAX_PAGELEN}))


def pipeline_steps(
    client: BBClient, workspace: str, repo: str, build_number: int
) -> list[dict[str, Any]]:
    """List the steps of a pipeline by build_number."""
    uuid = _resolve_pipeline_uuid(client, workspace, repo, build_number)
    return pipeline_steps_raw(client, workspace, repo, uuid)


def pipeline_trigger(
    client: BBClient,
    workspace: str,
    repo: str,
    *,
    branch: str,
    pattern: str | None = None,
    variables: dict[str, str] | Iterable[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Trigger a new pipeline run.

    Without `pattern`, runs the branch's default pipeline.
    With `pattern`, runs the named custom pipeline (must be defined in
    bitbucket-pipelines.yml under `custom:`).

    `variables` is the set of pipeline variables to pass — a dict
    {name: value} or an iterable of (name, value) tuples. Values must
    be strings; Bitbucket does not accept other JSON types for variables.

    Returns the new pipeline's record (includes build_number, uuid, etc.).
    """
    if not branch or not isinstance(branch, str):
        raise ValueError(f"branch is required and must be a string, got {branch!r}")

    target: dict[str, Any] = {"ref_name": branch, "ref_type": "branch"}
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"pattern must be a non-empty string, got {pattern!r}")
        target["selector"] = {"type": "custom", "pattern": pattern}

    payload: dict[str, Any] = {"target": target}

    if variables is not None:
        # Normalise to a list of {"key": k, "value": v} dicts — Bitbucket's
        # contract. Accept both dict and iterable-of-pairs at the Python
        # boundary so MCP tool args can use either.
        if isinstance(variables, dict):
            items = list(variables.items())
        else:
            items = list(variables)
        normalised: list[dict[str, str]] = []
        for k, v in items:
            if not isinstance(k, str) or not k:
                raise ValueError(f"variable key must be a non-empty string, got {k!r}")
            if not isinstance(v, str):
                raise ValueError(
                    f"variable value for {k!r} must be a string, got {type(v).__name__}"
                )
            normalised.append({"key": k, "value": v})
        if normalised:
            payload["variables"] = normalised

    return client.post(_pipelines_root(workspace, repo), json_body=payload)


def pipeline_stop(
    client: BBClient, workspace: str, repo: str, build_number: int
) -> Any:
    """Stop a running pipeline. Returns the raw API response (typically
    None on success — Bitbucket returns 204). The bash script discards
    this response with `> /dev/null`; we return it so the MCP tool can
    surface a structured outcome (parity follow-up for 4.7)."""
    uuid = _resolve_pipeline_uuid(client, workspace, repo, build_number)
    path = f"{_pipelines_root(workspace, repo)}{_wrap_uuid(uuid)}/stopPipeline"
    return client.post(path)


def pipeline_logs(
    client: BBClient,
    workspace: str,
    repo: str,
    build_number: int,
    step_index: int,
    *,
    timeout: float = 120.0,
) -> str:
    """Fetch raw log text for a pipeline step (0-based step index).

    Bitbucket returns either the log body inline (200) or a 307 redirect
    to an S3 signed URL. The fetch helper follows redirects while
    stripping the Authorization header on cross-host hops so the
    Bitbucket credential is never sent to S3. Default timeout is 120s
    because log payloads can be large and the bash equivalent uses
    no timeout cap.
    """
    pipeline_uuid = _resolve_pipeline_uuid(client, workspace, repo, build_number)
    step_uuid, _step_name = _resolve_step_uuid(
        client, workspace, repo, pipeline_uuid, step_index
    )
    path = (
        f"{_pipelines_root(workspace, repo)}"
        f"{_wrap_uuid(pipeline_uuid)}/steps/{_wrap_uuid(step_uuid)}/log"
    )
    return client.fetch_redirected_text(path, timeout=timeout)
