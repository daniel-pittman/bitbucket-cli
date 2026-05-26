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


def _is_positive_int(value: Any) -> bool:
    """True iff `value` is an int (NOT a bool) >= 1.

    `bool` is a subclass of `int` in Python, so `isinstance(True, int)` is
    True and `True < 1` is False — meaning a bare `isinstance(x, int) and
    x >= 1` check happily accepts `True` as `1`. That then propagates
    through f-string interpolation into URLs as the literal `"True"`, and
    through `urlencode({"pagelen": True})` as `"pagelen=True"` — both
    failure modes the boundary validator exists to prevent.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1

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
    return f"%7B{quote(inner)}%7D"


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
    if not _is_positive_int(build_number):
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
) -> str:
    """Return the step UUID for the step at the given 0-based index.

    The bash script's `bb logs` uses the same 0-based indexing into the
    steps list; mirror that contract so the user-facing index numbers
    match across both surfaces.

    Returns just the uuid (not the name) — callers that need the name
    should fetch the steps list themselves via `pipeline_steps()`. The
    MCP step-logs tool wraps the log payload in its own response shape
    and can surface the name there.
    """
    if (
        not isinstance(step_index, int)
        or isinstance(step_index, bool)
        or step_index < 0
    ):
        raise ValueError(f"step_index must be a non-negative int, got {step_index!r}")

    steps = _pipeline_steps_by_uuid(client, workspace, repo, pipeline_uuid)
    if step_index >= len(steps):
        raise BBOpNotFound(
            f"step index {step_index} out of range "
            f"(pipeline has {len(steps)} step{'s' if len(steps) != 1 else ''})"
        )
    return _strip_uuid_braces(steps[step_index].get("uuid"))


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
    if not _is_positive_int(count):
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


def _pipeline_steps_by_uuid(
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
    return _pipeline_steps_by_uuid(client, workspace, repo, uuid)


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
    step_uuid = _resolve_step_uuid(
        client, workspace, repo, pipeline_uuid, step_index
    )
    path = (
        f"{_pipelines_root(workspace, repo)}"
        f"{_wrap_uuid(pipeline_uuid)}/steps/{_wrap_uuid(step_uuid)}/log"
    )
    return client.fetch_redirected_text(path, timeout=timeout)


# ===========================================================================
#  PULL REQUEST OPERATIONS
# ===========================================================================

# Bitbucket's documented merge strategies. Validating at the boundary
# means the MCP tool fails fast on a typo rather than waiting for the
# server's 400.
_VALID_MERGE_STRATEGIES = frozenset({"merge_commit", "squash", "fast_forward"})

# PR `state` filter values the Bitbucket API accepts on the simple
# `?state=` query parameter. For multi-state filtering, Bitbucket requires
# the BBQL `q` parameter (e.g. `?q=state="OPEN" OR state="MERGED"`); the
# `?state=OPEN,MERGED` shape returns 400 / empty results. We validate the
# scalar form against this set when prs_list is called with `state=`;
# callers needing compound filtering should construct a `q=` query and
# call `client.paginate` directly.
_KNOWN_PR_STATES = frozenset({"OPEN", "MERGED", "DECLINED", "SUPERSEDED"})


def _prs_root(workspace: str, repo: str) -> str:
    """Common URL prefix for the pull-requests API."""
    return f"{repo_path(workspace, repo)}/pullrequests"


def _validate_pr_id(pr_id: int) -> None:
    """PR IDs are positive integers. The bash script passes them as bare
    strings and lets Bitbucket reject malformed values; we fail at the
    boundary so the MCP tool surfaces a clear error before any network
    call burns API budget.

    Rejects bool explicitly (`True`/`False` are subclass-of-int in Python
    but stringify to `"True"`/`"False"` in URLs, not `"1"`/`"0"`).
    """
    if not _is_positive_int(pr_id):
        raise ValueError(f"pr_id must be a positive int, got {pr_id!r}")


def prs_list(
    client: BBClient,
    workspace: str,
    repo: str,
    *,
    state: str = "OPEN",
    count: int = 25,
) -> list[dict[str, Any]]:
    """List pull requests filtered by state. Defaults match bash:
    state=OPEN, count=25. Walks pages as needed to honour `count`."""
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")
    if not isinstance(state, str) or not state:
        raise ValueError(f"state must be a non-empty string, got {state!r}")

    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    query: dict[str, Any] = {"state": state, "pagelen": pagelen}

    out: list[dict[str, Any]] = []
    for pr in client.paginate(_prs_root(workspace, repo), query=query):
        out.append(pr)
        if len(out) >= count:
            break
    return out


def pr_show(
    client: BBClient, workspace: str, repo: str, pr_id: int
) -> dict[str, Any]:
    """Fetch a pull request by its numeric ID."""
    _validate_pr_id(pr_id)
    return client.get(f"{_prs_root(workspace, repo)}/{pr_id}")


def pr_activity(
    client: BBClient,
    workspace: str,
    repo: str,
    pr_id: int,
    *,
    count: int = 50,
) -> list[dict[str, Any]]:
    """List the activity stream on a PR (approvals, comment events, state
    transitions). Used by the bash `bb pr` to surface approver names;
    surfaced separately as an op so the MCP agent can render its own view
    of the activity timeline."""
    _validate_pr_id(pr_id)
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")
    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    out: list[dict[str, Any]] = []
    for entry in client.paginate(
        f"{_prs_root(workspace, repo)}/{pr_id}/activity",
        query={"pagelen": pagelen},
    ):
        out.append(entry)
        if len(out) >= count:
            break
    return out


def pr_create(
    client: BBClient,
    workspace: str,
    repo: str,
    *,
    title: str,
    source_branch: str,
    destination_branch: str = "main",
    description: str = "",
    close_source_branch: bool = True,
    reviewers: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Create a pull request.

    `reviewers` is an iterable of Bitbucket account UUIDs (the API expects
    `[{"uuid": "..."}, ...]`). The bash script doesn't expose reviewers
    at create-time — that's a 4.7 parity gap, not a Python bug.

    `close_source_branch=True` matches bash's default (it hardcodes that
    flag in the create payload). If you don't want the branch deleted on
    merge, pass False explicitly.
    """
    for label, value in (
        ("title", title),
        ("source_branch", source_branch),
        ("destination_branch", destination_branch),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} must be a non-empty string, got {value!r}")
    if not isinstance(description, str):
        raise ValueError(
            f"description must be a string, got {type(description).__name__}"
        )
    if not isinstance(close_source_branch, bool):
        raise ValueError(
            f"close_source_branch must be a bool, got {type(close_source_branch).__name__}"
        )

    payload: dict[str, Any] = {
        "title": title,
        "source": {"branch": {"name": source_branch}},
        "destination": {"branch": {"name": destination_branch}},
        "close_source_branch": close_source_branch,
    }
    # Bash includes an empty description string ALWAYS; Python omits
    # when the description is empty or whitespace-only so the API payload
    # stays meaningful. Parity item: bash should align on omission.
    if description.strip():
        payload["description"] = description
    if reviewers is not None:
        # A bare string is technically an Iterable[str] (yields characters),
        # which would silently produce `[{"uuid":"a"}, {"uuid":"l"}, ...]`
        # from `reviewers="alice-uuid"`. Reject explicitly so the typo
        # fails locally rather than as a 400 from Bitbucket.
        if isinstance(reviewers, str):
            raise ValueError(
                f"reviewers must be a list/tuple of uuids, not a bare string. "
                f"Got {reviewers!r}; did you mean [{reviewers!r}]?"
            )
        normalised: list[dict[str, str]] = []
        for uuid in reviewers:
            if not isinstance(uuid, str) or not uuid:
                raise ValueError(
                    f"reviewer uuids must be non-empty strings, got {uuid!r}"
                )
            normalised.append({"uuid": uuid})
        if normalised:
            payload["reviewers"] = normalised

    return client.post(_prs_root(workspace, repo), json_body=payload)


def pr_approve(
    client: BBClient, workspace: str, repo: str, pr_id: int
) -> Any:
    """Approve a pull request as the authenticated user. Returns the
    approval record; the bash equivalent discards it with `> /dev/null`."""
    _validate_pr_id(pr_id)
    return client.post(f"{_prs_root(workspace, repo)}/{pr_id}/approve")


def pr_unapprove(
    client: BBClient, workspace: str, repo: str, pr_id: int
) -> Any:
    """Remove the authenticated user's approval from a PR.

    Not exposed by the bash CLI today — this is one of the parity gaps
    that 4.7 will fill. The Bitbucket REST contract is a DELETE against
    the same /approve subpath that POST uses for approval.
    """
    _validate_pr_id(pr_id)
    return client.delete(f"{_prs_root(workspace, repo)}/{pr_id}/approve")


def pr_merge(
    client: BBClient,
    workspace: str,
    repo: str,
    pr_id: int,
    *,
    strategy: str = "merge_commit",
    close_source_branch: bool = True,
    message: str | None = None,
) -> dict[str, Any]:
    """Merge a pull request.

    Bitbucket Cloud's documented strategies: `merge_commit` (default),
    `squash`, `fast_forward`. We validate at the boundary so a typo
    fails locally rather than burning an API call to get a 400.

    `message` overrides the default merge-commit message. `close_source_branch`
    matches bash's default of True.
    """
    _validate_pr_id(pr_id)
    if strategy not in _VALID_MERGE_STRATEGIES:
        raise ValueError(
            f"strategy must be one of {sorted(_VALID_MERGE_STRATEGIES)}, "
            f"got {strategy!r}"
        )
    if not isinstance(close_source_branch, bool):
        raise ValueError(
            f"close_source_branch must be a bool, got {type(close_source_branch).__name__}"
        )
    payload: dict[str, Any] = {
        "type": "pullrequest",
        "merge_strategy": strategy,
        "close_source_branch": close_source_branch,
    }
    if message is not None:
        # Symmetric with pr_comment_add's body validation: empty message
        # would produce `"message": ""` in the payload, leading to an
        # empty merge-commit subject line. Reject at the boundary.
        if not isinstance(message, str) or not message:
            raise ValueError(
                f"message must be a non-empty string when provided, "
                f"got {message!r}"
            )
        payload["message"] = message
    # Mirror bash's PUT verb (cmd_pr_merge uses bb_put). Bitbucket Cloud
    # has historically accepted both PUT and POST for this endpoint, and
    # the bash side is the verified-working contract. Flagged as a 4.7
    # investigation: verify against current Bitbucket docs and align on
    # one verb (POST is the modern documented shape per their REST docs
    # at time of writing).
    return client.put(
        f"{_prs_root(workspace, repo)}/{pr_id}/merge",
        json_body=payload,
    )


def pr_decline(
    client: BBClient, workspace: str, repo: str, pr_id: int
) -> Any:
    """Decline (close without merging) a pull request."""
    _validate_pr_id(pr_id)
    return client.post(f"{_prs_root(workspace, repo)}/{pr_id}/decline")


def pr_diff(
    client: BBClient,
    workspace: str,
    repo: str,
    pr_id: int,
    *,
    timeout: float = 120.0,
) -> str:
    """Fetch the unified diff text for a pull request.

    Bitbucket returns plain text (not JSON), so we route through
    `fetch_redirected_text`. Today the diff endpoint does NOT redirect,
    so this is functionally equivalent to a direct GET. If Bitbucket
    ever introduces a redirect, the cross-host-auth-strip protection
    kicks in — but the returned body would then be whatever the redirect
    target serves (a behavioural divergence from bash, which uses
    `curl -sf` without `-L` and would fail visibly on any 3xx). Until
    that happens, the two surfaces produce identical text.
    """
    _validate_pr_id(pr_id)
    return client.fetch_redirected_text(
        f"{_prs_root(workspace, repo)}/{pr_id}/diff",
        timeout=timeout,
    )


def pr_comments_list(
    client: BBClient,
    workspace: str,
    repo: str,
    pr_id: int,
    *,
    count: int = 100,
) -> list[dict[str, Any]]:
    """List comments on a pull request."""
    _validate_pr_id(pr_id)
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")
    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    out: list[dict[str, Any]] = []
    for comment in client.paginate(
        f"{_prs_root(workspace, repo)}/{pr_id}/comments",
        query={"pagelen": pagelen},
    ):
        out.append(comment)
        if len(out) >= count:
            break
    return out


def pr_comment_add(
    client: BBClient,
    workspace: str,
    repo: str,
    pr_id: int,
    body: str,
) -> dict[str, Any]:
    """Add a top-level comment to a pull request.

    Not exposed by the bash CLI today — 4.7 parity gap. The Bitbucket
    contract is `POST /pullrequests/{id}/comments` with payload
    `{"content": {"raw": "<text>"}}`.
    """
    _validate_pr_id(pr_id)
    if not isinstance(body, str) or not body:
        raise ValueError(f"body must be a non-empty string, got {body!r}")
    return client.post(
        f"{_prs_root(workspace, repo)}/{pr_id}/comments",
        json_body={"content": {"raw": body}},
    )
