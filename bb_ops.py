"""
bb_ops — Bitbucket REST operations grouped by resource.

The MCP server (mcp_server.py, future PR) wires each function here to a
tool. Each function takes a BBClient as its first positional argument and
returns native Python data (dicts, lists, strings) — no terminal-style
formatting, no colour codes, no parsing of bash output.

`bb` (bash) and bb_ops (Python) are parallel implementations of the same
Bitbucket REST contract. See CONTRIBUTING.md for the parity rule: when a
defect surfaces in either side, the fix lands in both code paths.

Current scope: pipelines, pull requests, repos/branches/vars/downloads/
commits. The companion git_ops module provides the git-context wrappers
the MCP server uses to resolve "current branch" / "remote workspace"
before invoking these ops.
"""

from __future__ import annotations

import re

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
# months" for an active repo. The bash script's inline lookup is a single
# 100-pipeline page; this MCP-side scan trades a few extra API calls for
# the ability to address older builds by number.
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
    variables: dict[str, str]
    | Iterable[tuple[str, str] | dict[str, Any]]
    | None = None,
) -> dict[str, Any]:
    """Trigger a new pipeline run.

    Without `pattern`, runs the branch's default pipeline.
    With `pattern`, runs the named custom pipeline (must be defined in
    bitbucket-pipelines.yml under `custom:`).

    `variables` is the set of per-run pipeline variables to pass: a dict
    {name: value}, an iterable of (name, value) tuples, or an iterable of
    Bitbucket wire-shape dicts `{"key": ..., "value": ..., "secured"?: bool}`.
    Values must be strings; Bitbucket does not accept other JSON types for
    variables. Variables need not be declared in bitbucket-pipelines.yml;
    the API accepts arbitrary per-run keys.

    Returns the new pipeline's record (includes build_number, uuid, etc.).
    """
    if not branch or not isinstance(branch, str):
        raise ValueError(f"branch is required and must be a string, got {branch!r}")

    # `type: "pipeline_ref_target"` is REQUIRED on the target. Without it
    # Bitbucket can't classify the reference and 400s with "Unsupported
    # reference target provided 'pipeline_unknown_target'". This bites the
    # custom-pattern path hardest (verified live); the default-branch path
    # happened to be accepted without it, but the field is correct for
    # both, so send it unconditionally for one consistent shape.
    target: dict[str, Any] = {
        "type": "pipeline_ref_target",
        "ref_type": "branch",
        "ref_name": branch,
    }
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"pattern must be a non-empty string, got {pattern!r}")
        target["selector"] = {"type": "custom", "pattern": pattern}

    payload: dict[str, Any] = {"target": target}

    if variables is not None:
        # Normalise to a list of {"key": k, "value": v} dicts — Bitbucket's
        # contract. Accept a dict, an iterable of pairs, or wire-shape
        # dicts at the Python boundary so MCP tool args can use any form.
        if isinstance(variables, dict):
            items: list[Any] = list(variables.items())
        else:
            items = list(variables)
        normalised: list[dict[str, Any]] = []
        for item in items:
            secured: bool | None = None
            if isinstance(item, dict):
                # Bitbucket's wire shape. This MUST be an explicit case:
                # tuple-unpacking a dict iterates its KEY NAMES, so a
                # wire-shape item would silently become the literal
                # variable key="key", value="value", corrupting the run
                # instead of erroring.
                extra = set(item) - {"key", "value", "secured"}
                if extra or "key" not in item or "value" not in item:
                    raise ValueError(
                        "variable item dicts must have keys 'key' and 'value' "
                        f"(optionally 'secured'), got {sorted(item)!r}"
                    )
                k, v = item["key"], item["value"]
                if "secured" in item:
                    if not isinstance(item["secured"], bool):
                        raise ValueError(
                            f"variable 'secured' for {k!r} must be a bool, "
                            f"got {type(item['secured']).__name__}"
                        )
                    secured = item["secured"]
            elif isinstance(item, str):
                # A bare string would tuple-unpack character-wise (a
                # 2-char string "AB" unpacks to ("A", "B") without error).
                raise ValueError(
                    f"variable item must be a (key, value) pair or a "
                    f"{{'key', 'value'}} dict, got the string {item!r}"
                )
            else:
                try:
                    k, v = item
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"variable item must be a (key, value) pair or a "
                        f"{{'key', 'value'}} dict, got {item!r}"
                    ) from exc
            if not isinstance(k, str) or not k:
                raise ValueError(f"variable key must be a non-empty string, got {k!r}")
            if not isinstance(v, str):
                raise ValueError(
                    f"variable value for {k!r} must be a string, got {type(v).__name__}"
                )
            entry: dict[str, Any] = {"key": k, "value": v}
            if secured is not None:
                entry["secured"] = secured
            normalised.append(entry)
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


# --- Pipelines configuration (enable / disable / status) ---
#
# A repo's Pipelines feature is toggled via the pipelines_config resource:
#   GET /repositories/{ws}/{slug}/pipelines_config   → {"enabled": bool, ...}
#   PUT  ...                          {"enabled": true|false} → updated config
# Pipelines must be ENABLED before repo pipeline variables, custom
# pipelines, or builds work at all — this is the "CI won't run / vars
# won't take" gap. Note (verified live): the GET 404s when Pipelines has
# never been configured on the repo (the pre-enable state), rather than
# returning {"enabled": false}. pipelines_config_show translates that 404
# into a clean {"enabled": false, "configured": false} so callers get a
# definite answer instead of an exception for the common "never enabled"
# case.


def pipelines_config_show(
    client: BBClient, workspace: str, repo: str
) -> dict[str, Any]:
    """Return the repo's Pipelines configuration: `{"enabled": bool, ...}`.

    GET /repositories/{ws}/{slug}/pipelines_config. When Pipelines has
    never been configured the API 404s; this is the normal "never enabled"
    state, so it's translated to `{"enabled": False, "configured": False}`
    rather than propagating BBApiError. When the config exists, the raw
    record is returned with a `"configured": True` marker added.
    """
    path = f"{repo_path(workspace, repo)}/pipelines_config"
    try:
        result = client.get(path)
    except BBApiError as e:
        if e.status == 404:
            return {"enabled": False, "configured": False}
        raise
    if isinstance(result, dict):
        result.setdefault("configured", True)
    return result


def pipelines_config_set(
    client: BBClient, workspace: str, repo: str, *, enabled: bool
) -> dict[str, Any]:
    """Enable or disable Pipelines on a repo.

    PUT /repositories/{ws}/{slug}/pipelines_config with
    `{"enabled": true|false}`. Returns the updated configuration record.

    Requires `admin:pipeline:bitbucket` scope on the token (toggling the
    Pipelines feature is a pipeline-admin operation, same scope family as
    `vars_set`). `write:pipeline:bitbucket` alone is insufficient; a 403
    names the missing scope under `error.detail.required`.
    """
    if not isinstance(enabled, bool):
        raise ValueError(f"enabled must be a bool, got {type(enabled).__name__}")
    path = f"{repo_path(workspace, repo)}/pipelines_config"
    return client.put(path, json_body={"enabled": enabled})


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


# Fields stripped from each PR object in the LIST view (prs_list) by
# default. Bitbucket PR objects carry the full rendered description +
# summary (raw / html / markup variants) and the participants array,
# which together push even a 3-PR list past the MCP 25k-token response
# cap on repos with rich PR bodies (observed: johnny-server, 3 open PRs
# = ~70 KB). The list/triage workflow (prs_list -> pick one -> pr_show)
# only needs identity + state + branches + author + links; the full
# body is one pr_show away. pr_show is intentionally NOT slimmed — it's
# the drill-down where you WANT the whole object.
_PR_LIST_BULKY_FIELDS = ("description", "summary", "rendered", "participants")


def _slim_pr_list_item(pr: dict[str, Any]) -> dict[str, Any]:
    """Drop the bulky fields from one PR list object. Shallow copy so
    the caller's source dict is untouched. `reviewers`, when present,
    is projected down to uuid + display_name per reviewer (the full
    account blobs are the other big contributor) while preserving the
    count and identities a triage view needs."""
    slim = {k: v for k, v in pr.items() if k not in _PR_LIST_BULKY_FIELDS}
    reviewers = pr.get("reviewers")
    if isinstance(reviewers, list):
        slim["reviewers"] = [
            {
                "uuid": r.get("uuid"),
                "display_name": r.get("display_name"),
            }
            for r in reviewers
            if isinstance(r, dict)
        ]
    return slim


def prs_list(
    client: BBClient,
    workspace: str,
    repo: str,
    *,
    state: str = "OPEN",
    count: int = 25,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """List pull requests filtered by state. Defaults match bash:
    state=OPEN, count=25. Walks pages as needed to honour `count`.

    By default each PR is slimmed (see _slim_pr_list_item) so the list
    fits the MCP response cap on rich-PR repos. Pass verbose=True to get
    the full Bitbucket PR objects (description, summary, rendered,
    participants intact) — useful when a caller genuinely needs the
    bodies and isn't going through the MCP transport."""
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")
    if not isinstance(state, str) or not state:
        raise ValueError(f"state must be a non-empty string, got {state!r}")
    # _KNOWN_PR_STATES is the boundary check. Without it, typos like
    # state="OPENED", case bugs like state="open", and unsupported
    # compound forms like state="OPEN,MERGED" would burn an API call
    # before failing (Bitbucket returns 400 or empty results). For
    # compound filtering use a `?q=` query via client.paginate directly.
    if state not in _KNOWN_PR_STATES:
        raise ValueError(
            f"state must be one of {sorted(_KNOWN_PR_STATES)}, got {state!r}"
        )

    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    query: dict[str, Any] = {"state": state, "pagelen": pagelen}

    out: list[dict[str, Any]] = []
    for pr in client.paginate(_prs_root(workspace, repo), query=query):
        out.append(pr if verbose else _slim_pr_list_item(pr))
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
    close_source_branch: bool = False,
    reviewers: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Create a pull request.

    `reviewers` is an iterable of Bitbucket account UUIDs (the API expects
    `[{"uuid": "..."}, ...]`). Discover them with `members_list`, whose
    `.user.uuid` is exactly this value, braces included. Parity with bash
    `bb pr-create --reviewer <uuid>` (repeatable).

    `close_source_branch` defaults to False: deleting the source branch on
    merge is a destructive action, so it is opt-in, never automatic (the
    same stance `gh pr merge` takes with `--delete-branch`). Pass
    `close_source_branch=True` to have the branch deleted when the PR
    merges. Parity with bash `bb pr-create --close-source-branch`.
    """
    for label, value in (
        ("title", title),
        ("source_branch", source_branch),
        ("destination_branch", destination_branch),
    ):
        # Strip-check rather than truthiness so " " / "\n\t" don't slip
        # through. A whitespace-only PR title is technically accepted by
        # Bitbucket but visually meaningless in any PR list view.
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"{label} must be a non-empty, non-whitespace string, got {value!r}"
            )
    if not isinstance(description, str):
        raise ValueError(
            f"description must be a string, got {type(description).__name__}"
        )
    if not isinstance(close_source_branch, bool):
        raise ValueError(
            f"close_source_branch must be a bool, "
            f"got {type(close_source_branch).__name__}"
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
        # Same helper pr_update uses, so both paths validate identically
        # AND canonicalise a bare uuid to the braced form the API returns.
        uuids = _normalise_reviewer_uuids("reviewers", reviewers)
        if uuids:
            payload["reviewers"] = [{"uuid": u} for u in uuids]

    return client.post(_prs_root(workspace, repo), json_body=payload)


# Bitbucket account UUIDs, as returned by members_list and on a PR's
# reviewers/participants, are BRACED: {8-4-4-4-12}. Callers routinely strip
# the braces when copying one by hand.
# Braces must be BALANCED. `\{?...\}?` would also match a half-braced
# `{aaaa…` (leading brace, no closer), which then fails the matched-pair
# strip below and gets re-wrapped into `{{aaaa…}` — mangling input the
# docstring promises to return untouched.
_UUID_CORE = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_UUID_RE = re.compile(r"^(?:\{" + _UUID_CORE + r"\}|" + _UUID_CORE + r")$")


def _canonical_reviewer_uuid(value: str) -> str:
    """Return a uuid in the braced form the API uses.

    pr_update's reviewer arithmetic compares caller-supplied uuids against
    `.reviewers[].uuid` and `.participants[].user.uuid` from a live
    response, all of which are braced. A bare uuid would therefore match
    nothing: the removal would silently no-op and an add would append a
    duplicate. Converging both forms here is what makes accepting either
    one safe.

    Anything that is not uuid-shaped is returned untouched — this layer
    does not validate format (the bash `_require_*` sibling does), and
    wrapping an unrecognised string in braces would corrupt it.
    """
    stripped = value.strip()
    if not _UUID_RE.match(stripped):
        return value
    core = stripped[1:-1] if stripped.startswith("{") and stripped.endswith("}") else stripped
    return "{" + core + "}"


def _normalise_reviewer_uuids(label: str, values: Iterable[str]) -> list[str]:
    """Validate a reviewer-uuid iterable and return it as a list.

    Rejects a bare string for the same reason pr_create does: a str is an
    Iterable[str] that yields CHARACTERS, so `add_reviewers="{abc}"` would
    silently become one reviewer per character.
    """
    if isinstance(values, str):
        raise ValueError(
            f"{label} must be a list/tuple of uuids, not a bare string. "
            f"Got {values!r}; did you mean [{values!r}]?"
        )
    out: list[str] = []
    for uuid in values:
        if not isinstance(uuid, str) or not uuid.strip():
            raise ValueError(
                f"{label} uuids must be non-empty strings, got {uuid!r}"
            )
        out.append(_canonical_reviewer_uuid(uuid))
    return out


def pr_update(
    client: BBClient,
    workspace: str,
    repo: str,
    pr_id: int,
    *,
    title: str | None = None,
    description: str | None = None,
    add_reviewers: Iterable[str] | None = None,
    remove_reviewers: Iterable[str] | None = None,
    drop_approvals: bool = False,
) -> dict[str, Any]:
    """Update an OPEN pull request's title and/or description.

    Bitbucket's PR mutation endpoint is a PUT to the SAME path pr_show GETs:
    PUT /repositories/{ws}/{slug}/pullrequests/{id}. Only open pull requests
    can be mutated. Only the fields present in the body change — the PUT
    merges them into the existing PR, preserving the source/destination
    branches and reviewers (Bitbucket keeps omitted PR fields on this
    endpoint, so a title-only or description-only update is safe).

    Note the HTTP method is PUT, not PATCH: Bitbucket Cloud has no PATCH for
    the pullrequests resource. This mirrors repo_update, which PUTs the same
    path repo_show GETs for the analogous repo mutation.

    `title`: None (omit) = leave unchanged; any non-empty, non-whitespace
    string = set it. An empty/whitespace title is INVALID (a PR must have a
    title — Bitbucket rejects a blank one and it's meaningless in any PR
    list view), so it's rejected at the boundary rather than sent.

    `description`: None (omit) = leave unchanged; "" = intentionally CLEAR
    the body; any other string = set it. This three-way distinction matches
    repo_update's description contract so a deliberate clear isn't collapsed
    into a no-op.

    `add_reviewers` / `remove_reviewers` change who is asked to review an
    EXISTING PR. Bitbucket has no add-a-reviewer endpoint: the PUT REPLACES
    the whole `reviewers` array, so sending just the person being added
    would silently unassign everyone else. Both options therefore read the
    PR first and send the full resulting list.

    That read-modify-write has a race: a reviewer added by someone else
    between the GET and the PUT is lost. Bitbucket exposes no ETag or
    if-match on this endpoint, so the window cannot be closed here; it is
    narrow and the operation is trivially repeatable.

    Deliberately absent: a wholesale "set the reviewers to exactly this"
    option. Replace is the operation that silently discards other people's
    work, and add/remove composes to the same result with each change
    stated explicitly. Adding someone already on the PR is a no-op rather
    than a duplicate, so both are idempotent.

    `drop_approvals` guards the destructive half. Removing a reviewer who
    has already APPROVED discards that approval, and re-adding them does
    not bring it back — Bitbucket resets their participant state. That is
    not recoverable from the CLI, so it is refused unless the caller opts
    in explicitly, matching the repo-wide rule that a destructive action is
    never a default. Removing a reviewer who has NOT approved needs no
    opt-in.

    At least one of `title` / `description` / `add_reviewers` /
    `remove_reviewers` must be supplied; a PUT with an empty body would be
    a no-op round-trip, so it's rejected at the boundary before burning an
    API call.

    Returns the updated PR record.
    """
    _validate_pr_id(pr_id)

    payload: dict[str, Any] = {}
    if title is not None:
        if not isinstance(title, str) or not title.strip():
            raise ValueError(
                f"title must be a non-empty, non-whitespace string when "
                f"provided, got {title!r}"
            )
        payload["title"] = title
    if description is not None:
        if not isinstance(description, str):
            raise ValueError(
                f"description must be a string when provided, got "
                f"{type(description).__name__}"
            )
        payload["description"] = description

    to_add = (
        _normalise_reviewer_uuids("add_reviewers", add_reviewers)
        if add_reviewers is not None
        else None
    )
    to_remove = (
        _normalise_reviewer_uuids("remove_reviewers", remove_reviewers)
        if remove_reviewers is not None
        else None
    )
    # An empty list is a caller mistake rather than a meaningful request:
    # "add nobody" / "remove nobody" would fall through to the
    # at-least-one-field error below and report something confusing.
    for label, values in (("add_reviewers", to_add), ("remove_reviewers", to_remove)):
        if values is not None and not values:
            raise ValueError(f"{label} was empty; nothing to change")

    if to_add is not None or to_remove is not None:
        pr_path = f"{_prs_root(workspace, repo)}/{pr_id}"
        current = client.get(pr_path)
        current_uuids = [
            r["uuid"]
            for r in (current.get("reviewers") or [])
            if isinstance(r, dict) and r.get("uuid")
        ]

        removing = set(to_remove or [])
        if removing:
            # An approval is only visible on `participants`, not on
            # `reviewers`, so the check reads the participant record for
            # each person being removed.
            approved_uuids = {
                p["user"]["uuid"]
                for p in (current.get("participants") or [])
                if isinstance(p, dict)
                and p.get("approved")
                and isinstance(p.get("user"), dict)
                and p["user"].get("uuid")
            }
            dropping_approvals = sorted(removing & approved_uuids)
            if dropping_approvals and not drop_approvals:
                raise ValueError(
                    "refusing to remove reviewer(s) who have already approved: "
                    f"{', '.join(dropping_approvals)}. Removing them discards "
                    "the approval and re-adding them does not restore it. Pass "
                    "drop_approvals=True to do it anyway."
                )

        # Order is deliberate: survivors keep their existing position and
        # additions append, so a repeated call produces a stable list.
        new_uuids = [u for u in current_uuids if u not in removing]
        for uuid in to_add or []:
            if uuid not in new_uuids:
                new_uuids.append(uuid)

        payload["reviewers"] = [{"uuid": u} for u in new_uuids]

    if not payload:
        raise ValueError(
            "pr_update requires at least one field to change "
            "(title, description, add_reviewers and/or remove_reviewers)"
        )

    return client.put(f"{_prs_root(workspace, repo)}/{pr_id}", json_body=payload)


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
    close_source_branch: bool = False,
    message: str | None = None,
) -> dict[str, Any]:
    """Merge a pull request.

    Bitbucket Cloud's documented strategies: `merge_commit` (default),
    `squash`, `fast_forward`. We validate at the boundary so a typo
    fails locally rather than burning an API call to get a 400.

    `message` overrides the default merge-commit message.

    `close_source_branch` defaults to False and is always sent
    explicitly in the merge payload. Deleting the source branch on merge
    is a destructive action, so it is opt-in, never automatic (the same
    stance `gh pr merge` takes with `--delete-branch`). Sending False
    explicitly also OVERRIDES whatever `close_source_branch` the PR was
    stored with at creation — the merge API's value wins over the PR's —
    so a PR created by an older `bb`, or by the Bitbucket UI, with the
    box checked will still keep its source branch here unless the caller
    opts in. Pass `close_source_branch=True` to delete on merge. Parity
    with bash `bb pr-merge --close-source-branch`.
    """
    _validate_pr_id(pr_id)
    # isinstance gate before the membership test: a non-hashable strategy
    # (list, dict, set) would otherwise raise TypeError from the frozenset
    # `in` check rather than the documented ValueError, breaking the
    # "every boundary failure is ValueError" convention this file follows.
    if not isinstance(strategy, str) or strategy not in _VALID_MERGE_STRATEGIES:
        raise ValueError(
            f"strategy must be one of {sorted(_VALID_MERGE_STRATEGIES)}, "
            f"got {strategy!r}"
        )
    if not isinstance(close_source_branch, bool):
        raise ValueError(
            f"close_source_branch must be a bool, "
            f"got {type(close_source_branch).__name__}"
        )
    # Symmetric with pr_comment_add's body validation: empty (or
    # whitespace-only) message would produce a blank merge-commit
    # subject line, visually empty in any `git log --oneline` view.
    # Reject at the boundary so every invalid input costs zero network IO.
    if message is not None and (not isinstance(message, str) or not message.strip()):
        raise ValueError(
            f"message must be a non-empty, non-whitespace string "
            f"when provided, got {message!r}"
        )
    payload: dict[str, Any] = {
        "type": "pullrequest",
        "merge_strategy": strategy,
        "close_source_branch": close_source_branch,
    }
    if message is not None:
        payload["message"] = message
    # Bitbucket's PR merge endpoint is POST per the REST docs. An earlier
    # version of this op (and bash cmd_pr_merge) used PUT on the
    # historical assumption that Bitbucket accepted either; that's no
    # longer true. PUT now returns HTTP 403 + "This endpoint does not
    # support token-based authentication" (an unhelpful error that
    # actually means "wrong method here"). Confirmed against
    # dreamfacesbir/ryan-os PR#2 (2026-06-28) where direct POST with the
    # same API token merged cleanly. Keep POST; do not "improve" back.
    return client.post(
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
    if not isinstance(body, str) or not body.strip():
        raise ValueError(
            f"body must be a non-empty, non-whitespace string, got {body!r}"
        )
    return client.post(
        f"{_prs_root(workspace, repo)}/{pr_id}/comments",
        json_body={"content": {"raw": body}},
    )


# ===========================================================================
#  WORKSPACES
# ===========================================================================


def workspaces_list(
    client: BBClient,
    *,
    count: int = 100,
) -> list[dict[str, Any]]:
    """List the Bitbucket workspaces the authenticated user belongs to.

    Uses `GET /2.0/user/workspaces` — the CHANGE-3022 replacement for
    the cross-workspace listing endpoints removed under CHANGE-2770
    (effective 2026-04-14). The old `/2.0/workspaces` and
    `/2.0/user/permissions/workspaces` both now return CHANGE-2770
    errors regardless of token shape.

    Requires `read:workspace:bitbucket` scope on the API token. A token
    granted only repository/pullrequest/pipeline scopes will surface
    Bitbucket's "credentials lack one or more required privilege
    scopes" 403 verbatim through the BBApiError path — the agent /
    user sees exactly which scope to add when rotating.

    Each value is a `workspace_access` envelope with the new sparse
    schema: `.administrator` (bool), `.workspace.slug`, `.workspace.uuid`,
    `.workspace.links` (no `name` / no `permission` string — those were
    legacy fields not carried into the new endpoint). Callers should
    branch on `administrator` (bool) rather than expecting a role
    string.
    """
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")

    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    q: dict[str, Any] = {"pagelen": pagelen}

    out: list[dict[str, Any]] = []
    for w in client.paginate("/user/workspaces", query=q):
        out.append(w)
        if len(out) >= count:
            break
    return out


def projects_list(
    client: BBClient,
    workspace: str | None = None,
    *,
    count: int = 100,
) -> list[dict[str, Any]]:
    """List the projects in a workspace.

    Uses `GET /2.0/workspaces/{workspace}/projects` (paginated). Each
    value is a project record carrying `.key` (the short project key used
    in repo bodies, e.g. `WID`), `.name`, `.uuid`, and `.links`.

    `workspace=None` defaults to the client's configured workspace
    (`client.config.workspace`); pass an explicit workspace to query a
    different one.

    Requires the `read:project:bitbucket` scope on the API token. A token
    without it surfaces Bitbucket's "credentials lack one or more required
    privilege scopes" 403 verbatim through the BBApiError path; the exact
    missing scope is recoverable from the error body under
    `error.detail.required`.
    """
    ws = workspace if workspace is not None else client.config.workspace
    # Mirror repos_list: this op doesn't route through repo_path (it's a
    # workspace-level listing, no slug), so the workspace contract is
    # validated inline — empty/whitespace AND embedded `/`, `.`, `..`.
    if not isinstance(ws, str) or not ws.strip():
        raise ValueError(f"workspace must be a non-empty string, got {ws!r}")
    if "/" in ws:
        raise ValueError(f"workspace must not contain '/', got {ws!r}")
    if ws in (".", ".."):
        raise ValueError(f"workspace must not be '.' or '..', got {ws!r}")
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")

    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    q: dict[str, Any] = {"pagelen": pagelen}

    out: list[dict[str, Any]] = []
    for p in client.paginate(f"/workspaces/{ws}/projects", query=q):
        out.append(p)
        if len(out) >= count:
            break
    return out


def members_list(
    client: BBClient,
    workspace: str | None = None,
    *,
    count: int = 100,
) -> list[dict[str, Any]]:
    """List the members of a workspace.

    Uses `GET /2.0/workspaces/{workspace}/members` (paginated). Each value
    is a `workspace_membership` envelope whose `.user` carries the fields
    that identify a person: `.uuid`, `.display_name`, `.nickname`, and
    `.account_id`.

    This is the lookup that makes `pr_create(reviewers=[...])` usable. The
    PR API identifies reviewers ONLY by account UUID, so without a member
    listing there is no supported way to discover the value to pass, and
    callers were forced to hand-roll the API call. The `.user.uuid` here
    is exactly what `reviewers` expects, braces included.

    `workspace=None` defaults to the client's configured workspace
    (`client.config.workspace`); pass an explicit workspace to query a
    different one.
    """
    ws = workspace if workspace is not None else client.config.workspace
    # Mirror projects_list / repos_list: a workspace-level listing does not
    # route through repo_path, so the workspace contract is validated
    # inline — empty/whitespace AND embedded `/`, `.`, `..`.
    if not isinstance(ws, str) or not ws.strip():
        raise ValueError(f"workspace must be a non-empty string, got {ws!r}")
    if "/" in ws:
        raise ValueError(f"workspace must not contain '/', got {ws!r}")
    if ws in (".", ".."):
        raise ValueError(f"workspace must not be '.' or '..', got {ws!r}")
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")

    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    q: dict[str, Any] = {"pagelen": pagelen}

    out: list[dict[str, Any]] = []
    for m in client.paginate(f"/workspaces/{ws}/members", query=q):
        out.append(m)
        if len(out) >= count:
            break
    return out


# ===========================================================================
#  REPOSITORY / BRANCH / VARIABLES / DOWNLOADS / COMMITS
# ===========================================================================


def repos_list(
    client: BBClient,
    workspace: str | None = None,
    *,
    count: int = 100,
    sort: str = "-updated_on",
    query: str | None = None,
) -> list[dict[str, Any]]:
    """List repositories in a workspace.

    `workspace=None` defaults to the client's configured workspace
    (`client.config.workspace`); pass an explicit workspace to query
    a different one (the bash equivalent only ever uses BB_WORKSPACE).

    `query` is a Bitbucket BBQL filter string passed via `?q=`, e.g.
    `'name ~ "widget"'`. Bash doesn't expose this; it's a 4.7 parity
    gap for the agent's filtered-list workflows.
    """
    ws = workspace if workspace is not None else client.config.workspace
    # Symmetric with bb_api.repo_path: reject empty/whitespace AND
    # embedded `/`, `.`, `..`. Without this, `workspace="acme/widget"`
    # would silently build `/repositories/acme/widget` (a single-repo
    # endpoint), then paginate against a response that lacks `values`
    # — a confusing failure mode the boundary validator exists to
    # prevent everywhere else in this file. repos_list is the only op
    # that doesn't route through repo_path, so the check is duplicated
    # here rather than central.
    if not isinstance(ws, str) or not ws.strip():
        raise ValueError(f"workspace must be a non-empty string, got {ws!r}")
    if "/" in ws:
        raise ValueError(f"workspace must not contain '/', got {ws!r}")
    if ws in (".", ".."):
        raise ValueError(f"workspace must not be '.' or '..', got {ws!r}")
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")

    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    q: dict[str, Any] = {"sort": sort, "pagelen": pagelen}
    if query is not None:
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"query must be a non-empty, non-whitespace string when provided, "
                f"got {query!r}"
            )
        q["q"] = query

    out: list[dict[str, Any]] = []
    for r in client.paginate(f"/repositories/{ws}", query=q):
        out.append(r)
        if len(out) >= count:
            break
    return out


def repo_show(
    client: BBClient, workspace: str, repo: str
) -> dict[str, Any]:
    """Fetch repository metadata: language, size, clone URLs, mainbranch,
    privacy, etc."""
    return client.get(repo_path(workspace, repo))


def repo_create(
    client: BBClient,
    workspace: str,
    repo: str,
    *,
    is_private: bool = True,
    project_key: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a new repository via POST /repositories/{workspace}/{repo}.

    Bitbucket's create endpoint is a POST to the SAME path repo_show GETs;
    the repo slug is in the URL and the body carries the settings. The
    `scm` field is always "git" (Bitbucket Cloud dropped Mercurial), and
    `is_private` defaults to true so a forgotten flag never publishes a
    repo by accident.

    `project_key` maps to the body's `{"project": {"key": ...}}`. A
    workspace that has any projects REQUIRES one; Bitbucket 400s a
    create with no project on such a workspace, so the caller surfaces
    the error rather than this function inventing a default.

    Returns the created repo record (includes slug, full_name, links —
    the clone URLs are under `.links.clone`).
    """
    # repo_path validates workspace + slug at the boundary (empty,
    # whitespace, embedded '/', '.', '..') — reuse it so create enforces
    # the exact same slug contract as every other repo op.
    path = repo_path(workspace, repo)

    payload: dict[str, Any] = {"scm": "git", "is_private": bool(is_private)}
    if project_key is not None:
        if not isinstance(project_key, str) or not project_key.strip():
            raise ValueError(
                f"project_key must be a non-empty, non-whitespace string when "
                f"provided, got {project_key!r}"
            )
        payload["project"] = {"key": project_key.strip()}
    if description is not None:
        if not isinstance(description, str):
            raise ValueError(
                f"description must be a string when provided, got "
                f"{type(description).__name__}"
            )
        payload["description"] = description

    return client.post(path, json_body=payload)


def repo_update(
    client: BBClient,
    workspace: str,
    repo: str,
    *,
    project_key: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Update an existing repository via PUT /repositories/{workspace}/{repo}.

    Bitbucket's update endpoint is a PUT to the SAME path repo_show GETs
    and repo_create POSTs; only the fields present in the body change. The
    dominant use is moving a repo between projects (repo_create takes a
    project but nothing could change it afterward — this closes that gap).

    `project_key` maps to the body's `{"project": {"key": ...}}` — that's
    how Bitbucket reassigns a repo's project. `description` updates the
    repo description. At least one field must be supplied; a PUT with an
    empty body would be a no-op round-trip, so it's rejected at the
    boundary rather than burning an API call.

    Requires `admin:repository:bitbucket` scope on the token (same as
    repo_create — changing repo settings is an admin operation).
    `write:repository:bitbucket` alone returns 403, whose body names the
    missing scope under `error.detail.required`.

    Returns the updated repo record.
    """
    # repo_path validates workspace + slug at the boundary (empty,
    # whitespace, embedded '/', '.', '..') — reuse it so update enforces
    # the exact same slug contract as every other repo op.
    path = repo_path(workspace, repo)

    payload: dict[str, Any] = {}
    if project_key is not None:
        if not isinstance(project_key, str) or not project_key.strip():
            raise ValueError(
                f"project_key must be a non-empty, non-whitespace string when "
                f"provided, got {project_key!r}"
            )
        payload["project"] = {"key": project_key.strip()}
    if description is not None:
        if not isinstance(description, str):
            raise ValueError(
                f"description must be a string when provided, got "
                f"{type(description).__name__}"
            )
        payload["description"] = description

    if not payload:
        raise ValueError(
            "repo_update requires at least one field to change "
            "(project_key and/or description)"
        )

    return client.put(path, json_body=payload)


# --- Deployment environments ---
#
# Deployment environments are the named targets a pipeline deploys to
# (Test / Staging / Production), each carrying its own deployment
# variables (covered separately by vars_set --deployment). These ops
# manage the environments themselves:
#   GET    /repositories/{ws}/{slug}/environments/            → list
#   POST   ...   {"name", "environment_type": {"name"}}       → create
#   DELETE ...   /{env_uuid}/                                 → delete
# Body shape + 201/204 responses verified against the live API. The
# environment_type.name is one of Bitbucket's deployment categories:
# Test, Staging, Production.

# Bitbucket's deployment environment categories. The POST body's
# environment_type.name must be one of these (verified live: "Test"
# accepted). Compared case-insensitively, sent in Bitbucket's canonical
# capitalisation.
_ENVIRONMENT_TYPES = {"test": "Test", "staging": "Staging", "production": "Production"}


def environments_list(
    client: BBClient, workspace: str, repo: str, *, count: int = 100
) -> list[dict[str, Any]]:
    """List the repo's deployment environments.

    GET /repositories/{ws}/{slug}/environments/ (paginated). Each record
    carries `.name`, `.slug`, `.uuid`, and `.environment_type`.
    """
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")
    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    out: list[dict[str, Any]] = []
    for env in client.paginate(
        f"{repo_path(workspace, repo)}/environments/",
        query={"pagelen": pagelen},
    ):
        out.append(env)
        if len(out) >= count:
            break
    return out


def environment_create(
    client: BBClient,
    workspace: str,
    repo: str,
    name: str,
    *,
    environment_type: str = "Test",
) -> dict[str, Any]:
    """Create a deployment environment.

    POST /repositories/{ws}/{slug}/environments/ with
    `{"name": ..., "environment_type": {"name": ...}}`. `environment_type`
    is one of Test / Staging / Production (case-insensitive; sent in
    canonical capitalisation). Returns the created environment record
    (includes its `uuid`, needed for delete and for vars --deployment).

    Requires `admin:pipeline:bitbucket` scope on the token (deployment
    environments are part of the Pipelines admin surface). A 403 names the
    missing scope under `error.detail.required`.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"name must be a non-empty, non-whitespace string, got {name!r}"
        )
    if not isinstance(environment_type, str) or not environment_type.strip():
        raise ValueError(
            f"environment_type must be a non-empty string, got {environment_type!r}"
        )
    canonical = _ENVIRONMENT_TYPES.get(environment_type.strip().casefold())
    if canonical is None:
        raise ValueError(
            f"environment_type must be one of Test / Staging / Production, "
            f"got {environment_type!r}"
        )
    path = f"{repo_path(workspace, repo)}/environments/"
    payload = {"name": name.strip(), "environment_type": {"name": canonical}}
    return client.post(path, json_body=payload)


def environment_delete(
    client: BBClient, workspace: str, repo: str, environment: str
) -> Any:
    """Delete a deployment environment by NAME (or slug).

    Resolves the name to its UUID (reusing the same lookup as
    vars --deployment), then DELETEs
    /repositories/{ws}/{slug}/environments/{env_uuid}/. Returns the raw
    response (None on success — Bitbucket returns 204). Raises
    BBOpNotFound if no environment matches the name, so a typo fails
    clearly instead of silently no-op'ing.

    Requires `admin:pipeline:bitbucket` scope on the token (same as
    create). A 403 names the missing scope under `error.detail.required`.
    """
    uuid = _resolve_environment_uuid(client, workspace, repo, environment)
    encoded = quote(uuid, safe="")
    path = f"{repo_path(workspace, repo)}/environments/{encoded}/"
    return client.delete(path)


# --- Branches ---


def branches_list(
    client: BBClient,
    workspace: str,
    repo: str,
    *,
    count: int = 50,
    sort: str = "-target.date",
    query: str | None = None,
) -> list[dict[str, Any]]:
    """List branches in the repo, default sort is most-recently-updated
    first (matches bash). `query` is a Bitbucket BBQL filter for
    name-substring etc.; not exposed by bash."""
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")
    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    q: dict[str, Any] = {"sort": sort, "pagelen": pagelen}
    if query is not None:
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"query must be a non-empty, non-whitespace string when provided, "
                f"got {query!r}"
            )
        q["q"] = query

    out: list[dict[str, Any]] = []
    for br in client.paginate(
        f"{repo_path(workspace, repo)}/refs/branches", query=q
    ):
        out.append(br)
        if len(out) >= count:
            break
    return out


def branch_show(
    client: BBClient, workspace: str, repo: str, name: str
) -> dict[str, Any]:
    """Fetch a single branch by name. Not exposed by bash today — 4.7
    parity gap. Useful for the agent's "does this branch exist?" lookup
    before creating a PR.

    The branch name is URL-encoded; `feat/widget` becomes `feat%2Fwidget`
    in the request path so the slash isn't interpreted as a sub-resource.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"name must be a non-empty, non-whitespace string, got {name!r}"
        )
    # `quote(s, safe="")` URL-encodes `/` to `%2F`. Branch names like
    # `feat/widget` would otherwise be interpreted as a sub-resource
    # path by Bitbucket and 404.
    encoded = quote(name.strip(), safe="")
    return client.get(f"{repo_path(workspace, repo)}/refs/branches/{encoded}")


# --- Variables (pipeline configuration) ---
#
# Bitbucket exposes pipeline variables at THREE scopes, each on a
# different (and inconsistently-named) endpoint. The base paths are
# verified against the live API:
#
#   repo        /repositories/{ws}/{slug}/pipelines_config/variables/
#   workspace   /workspaces/{ws}/pipelines-config/variables/      (HYPHEN)
#   deployment  /repositories/{ws}/{slug}/deployments_config/
#                   environments/{env_uuid}/variables/
#
# Note the workspace scope uses `pipelines-config` (hyphen) where the
# repo scope uses `pipelines_config` (underscore); the underscore form
# 404s at the workspace scope. The deployment scope keys off an
# environment UUID, which the caller supplies by NAME — _resolve_environment_uuid
# maps it. All three share the same body shape ({key, value, secured})
# and the same create-or-update semantics, so the list / find / set
# helpers take a pre-built `base` path and are scope-agnostic.

_VARS_SCOPES = ("repo", "workspace", "deployment")

# Sentinel for vars_set's `existing` param. `None` is a MEANINGFUL value
# there: it's what a caller passes after looking the key up and finding it
# ABSENT (so vars_set should go straight to create, no second lookup).
# This sentinel is the real "caller did not pre-fetch" marker, so vars_set
# only re-runs the lookup when nothing was passed. Without it, a caller
# that pre-fetched and got None would trigger a redundant second
# pagination on every create.
_NOT_PREFETCHED: Any = object()


def _resolve_environment_uuid(
    client: BBClient, workspace: str, repo: str, environment: str
) -> str:
    """Resolve a deployment-environment NAME (or slug) to its UUID.

    The deployment-variable endpoint is keyed by environment UUID, but
    humans refer to environments by name ("Development", "Production").
    GET the environment list and match case-insensitively on `name`
    first, then `slug`. Raise BBOpNotFound if no environment matches so
    the caller surfaces a clear "no such environment" instead of building
    a URL with an empty UUID.

    Returns the brace-wrapped UUID exactly as Bitbucket returns it (the
    `set`/`list` helpers URL-encode it for the path).
    """
    if not isinstance(environment, str) or not environment.strip():
        raise ValueError(
            f"environment must be a non-empty, non-whitespace string, "
            f"got {environment!r}"
        )
    target = environment.strip().casefold()
    base = f"{repo_path(workspace, repo)}/environments/"
    names: list[str] = []
    for env in client.paginate(base, query={"pagelen": _BITBUCKET_MAX_PAGELEN}):
        name = env.get("name")
        slug = env.get("slug")
        if name:
            names.append(name)
        if (isinstance(name, str) and name.casefold() == target) or (
            isinstance(slug, str) and slug.casefold() == target
        ):
            uuid = env.get("uuid")
            if not uuid:
                raise BBOpNotFound(
                    f"environment {environment!r} found but has no uuid"
                )
            return uuid
    raise BBOpNotFound(
        f"no deployment environment named {environment!r} in "
        f"{workspace}/{repo} (available: {names or 'none'})"
    )


def _variables_base(
    client: BBClient,
    workspace: str,
    repo: str | None,
    *,
    scope: str = "repo",
    environment: str | None = None,
) -> str:
    """Build the variables collection base path for the requested scope.

    `repo` is required for the repo and deployment scopes; ignored for
    the workspace scope. `environment` is required for the deployment
    scope (resolved to a UUID here) and rejected for the others.
    """
    if scope not in _VARS_SCOPES:
        raise ValueError(
            f"scope must be one of {_VARS_SCOPES}, got {scope!r}"
        )

    if scope == "workspace":
        if environment is not None:
            raise ValueError("environment is only valid for the deployment scope")
        # Validate the workspace the same way repo_path validates its
        # segments (empty / whitespace / '/' / '.' / '..').
        if not workspace or not workspace.strip():
            raise ValueError(
                f"workspace must be a non-empty, non-whitespace string, "
                f"got {workspace!r}"
            )
        if "/" in workspace:
            raise ValueError(f"workspace must not contain '/', got {workspace!r}")
        if workspace in (".", ".."):
            raise ValueError(f"workspace must not be '.' or '..', got {workspace!r}")
        # HYPHEN form — verified against the live API; the underscore
        # form 404s at the workspace scope.
        return f"/workspaces/{workspace}/pipelines-config/variables/"

    if repo is None:
        raise ValueError(f"repo is required for the {scope} scope")

    if scope == "repo":
        if environment is not None:
            raise ValueError("environment is only valid for the deployment scope")
        return f"{repo_path(workspace, repo)}/pipelines_config/variables/"

    # scope == "deployment"
    if environment is None:
        raise ValueError("environment is required for the deployment scope")
    env_uuid = _resolve_environment_uuid(client, workspace, repo, environment)
    encoded = quote(env_uuid, safe="")
    return (
        f"{repo_path(workspace, repo)}/deployments_config/"
        f"environments/{encoded}/variables/"
    )


def vars_list(
    client: BBClient,
    workspace: str,
    repo: str | None = None,
    *,
    count: int = 100,
    scope: str = "repo",
    environment: str | None = None,
) -> list[dict[str, Any]]:
    """List pipeline configuration variables (key/value pairs, with a
    `secured` flag that masks values for sensitive variables).

    `scope` selects repo (default), workspace, or deployment. The
    deployment scope requires `environment` (an env name/slug, resolved
    to a UUID). See `_variables_base` for the endpoint per scope.

    Bash truncates secured values to `********` in its display layer;
    Python returns the raw dicts (which include `"value": null` for
    secured entries — Bitbucket does NOT echo secured values). The
    MCP agent surfaces the secured flag explicitly so callers don't
    accidentally assume `null` means "unset".
    """
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")
    base = _variables_base(
        client, workspace, repo, scope=scope, environment=environment
    )
    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    out: list[dict[str, Any]] = []
    for v in client.paginate(base, query={"pagelen": pagelen}):
        out.append(v)
        if len(out) >= count:
            break
    return out


def _find_var_by_key_at(
    client: BBClient, base: str, key: str
) -> dict[str, Any] | None:
    """Return the existing variable dict at `base` whose `key` matches,
    or None. Walks the full list (no count cap) so a create-or-update
    never mistakes "not on page 1" for "doesn't exist" and POSTs a
    duplicate (Bitbucket allows duplicate keys via the API)."""
    for v in client.paginate(base, query={"pagelen": _BITBUCKET_MAX_PAGELEN}):
        if v.get("key") == key:
            return v
    return None


def vars_set(
    client: BBClient,
    workspace: str,
    repo: str | None,
    key: str,
    value: str,
    *,
    secured: bool = False,
    scope: str = "repo",
    environment: str | None = None,
    existing: dict[str, Any] | None = _NOT_PREFETCHED,
    base: str | None = None,
) -> dict[str, Any]:
    """Create or update a pipeline variable at the requested scope.

    Finds an existing variable by `key`. If present, PUTs to
    `.../variables/{uuid}` (update); otherwise POSTs to
    `.../variables/` (create). Body is `{"key", "value", "secured"}`.
    Works identically across the repo / workspace / deployment scopes —
    only the base path differs (see `_variables_base`).

    Secret hygiene: this function NEVER logs or echoes `value`. Bitbucket
    does not echo a secured variable's value back on write either (the
    response `value` is null when `secured` is true), so the returned
    dict is safe to surface — but callers must still mask the value they
    PASSED IN. The bash/MCP layers read the value from a file or env var
    so it never lands in argv, and they mask it in any output.

    Returns the created/updated variable record. For a secured variable
    Bitbucket returns `{"key", "uuid", "secured": true, "value": null}` —
    the absence of the value is by design, not an error.

    `existing` lets a caller that already looked the variable up (e.g.
    to report "created" vs "updated") pass the lookup result in so the
    full variable list isn't paginated twice. Pass the looked-up dict to
    update it, or `None` to signal "looked it up, it's absent" (go
    straight to create with no second pagination). Omit the argument
    entirely (the `_NOT_PREFETCHED` default) to have this function do the
    lookup. The distinction matters: `None` is "pre-fetched and absent",
    NOT "not pre-fetched"; conflating them re-paginated on every create.

    `base` lets a caller that already built the collection path (via
    `_variables_base`) pass it in so the deployment scope doesn't resolve
    the environment NAME->UUID twice (once in the caller's find-by-key,
    once here). When `None` the base is built from scope/environment.
    This keeps the deployment path to a single environment lookup,
    matching the bash CLI (which resolves the env once into `$base`).
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError(
            f"key must be a non-empty, non-whitespace string, got {key!r}"
        )
    if not isinstance(value, str):
        raise ValueError(
            f"value must be a string, got {type(value).__name__}"
        )
    key = key.strip()

    if base is None:
        base = _variables_base(
            client, workspace, repo, scope=scope, environment=environment
        )
    else:
        # Validate scope/environment consistency even when `base` is
        # supplied, so a caller passing a base that contradicts
        # scope/environment can't bypass the "environment required for the
        # deployment scope" / "environment only valid for deployment"
        # guards. These checks are pure (no network) for all scopes.
        if scope not in _VARS_SCOPES:
            raise ValueError(
                f"scope must be one of {_VARS_SCOPES}, got {scope!r}"
            )
        if scope == "deployment" and environment is None:
            raise ValueError("environment is required for the deployment scope")
        if scope != "deployment" and environment is not None:
            raise ValueError("environment is only valid for the deployment scope")

    payload: dict[str, Any] = {
        "key": key,
        "value": value,
        "secured": bool(secured),
    }

    # `_NOT_PREFETCHED` means the caller didn't look it up, so do it here.
    # A literal `None` means the caller looked it up and it's absent, so
    # skip straight to create (no redundant second pagination).
    if existing is _NOT_PREFETCHED:
        existing = _find_var_by_key_at(client, base, key)
    if existing is not None:
        uuid = existing.get("uuid")
        if not uuid:
            # An existing entry with no uuid can't be addressed for PUT.
            # Raise rather than fall through to POST (which would create
            # a duplicate-keyed variable).
            raise BBOpNotFound(
                f"variable {key!r} found but has no uuid; cannot update"
            )
        # uuid comes back from the API already brace-wrapped (`{...}`);
        # it must be URL-encoded for the path segment so the braces don't
        # break the URL.
        encoded_uuid = quote(uuid, safe="")
        return client.put(f"{base}{encoded_uuid}", json_body=payload)

    return client.post(base, json_body=payload)


def vars_delete(
    client: BBClient,
    workspace: str,
    repo: str | None,
    key: str,
    *,
    scope: str = "repo",
    environment: str | None = None,
) -> dict[str, Any]:
    """Delete a pipeline configuration variable by key.

    Resolves the variable by `key` to its UUID (walking all pages, same
    as `vars_set` — so "not on page 1" is never mistaken for "absent"),
    then DELETEs `.../variables/{uuid}`. Works identically across the
    repo / workspace / deployment scopes; only the base path differs (see
    `_variables_base`).

    Raises `BBOpNotFound` if no variable with that key exists at the scope,
    BEFORE issuing any DELETE — so a typo'd key is a clean not-found, not a
    silent no-op or a spurious network write.

    Requires `admin:pipeline:bitbucket` scope on the token (same as
    `vars_set` — they're the same pipeline-config resource);
    `write:pipeline:bitbucket` alone returns 403, whose body names the
    missing scope under `error.detail.required`.

    Returns `{"key", "scope", "environment", "uuid"}` so the caller can
    confirm exactly what was removed. The MCP/bash layers add
    `action: "deleted"`.
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError(
            f"key must be a non-empty, non-whitespace string, got {key!r}"
        )
    key = key.strip()

    base = _variables_base(
        client, workspace, repo, scope=scope, environment=environment
    )
    existing = _find_var_by_key_at(client, base, key)
    if existing is None:
        raise BBOpNotFound(
            f"no variable named {key!r} at the {scope} scope"
            + (f" (environment {environment!r})" if environment else "")
        )
    uuid = existing.get("uuid")
    if not uuid:
        # An entry with no uuid can't be addressed for DELETE — surface it
        # rather than building a `.../variables/` (collection) DELETE that
        # would 405 or, worse, do something unexpected.
        raise BBOpNotFound(
            f"variable {key!r} found but has no uuid; cannot delete"
        )
    encoded_uuid = quote(uuid, safe="")
    client.delete(f"{base}{encoded_uuid}")
    return {"key": key, "scope": scope, "environment": environment, "uuid": uuid}


# --- Downloads (release artifacts) ---


def downloads_list(
    client: BBClient,
    workspace: str,
    repo: str,
    *,
    count: int = 25,
) -> list[dict[str, Any]]:
    """List repository download artifacts (the Bitbucket "Downloads" tab
    — release binaries, install bundles, etc.)."""
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")
    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    out: list[dict[str, Any]] = []
    for d in client.paginate(
        f"{repo_path(workspace, repo)}/downloads",
        query={"pagelen": pagelen},
    ):
        out.append(d)
        if len(out) >= count:
            break
    return out


# --- Commits ---


def commits_list(
    client: BBClient,
    workspace: str,
    repo: str,
    *,
    branch: str | None = None,
    count: int = 10,
) -> list[dict[str, Any]]:
    """List recent commits.

    With `branch=None`, lists across all branches (Bitbucket's
    `/commits` endpoint).

    With `branch="feat/widget"`, lists commits reachable from that
    branch (`/commits/{branch}`). Branch names are URL-encoded for
    the same slash-as-sub-resource reason as branch_show.

    Not exposed by the bash CLI today — 4.7 parity gap. Useful for
    the agent's "what shipped recently?" / "what's in this branch
    that isn't in main?" workflows.
    """
    if not _is_positive_int(count):
        raise ValueError(f"count must be a positive int, got {count!r}")
    if branch is not None and (not isinstance(branch, str) or not branch.strip()):
        raise ValueError(
            f"branch must be a non-empty, non-whitespace string when provided, "
            f"got {branch!r}"
        )

    pagelen = min(count, _BITBUCKET_MAX_PAGELEN)
    if branch is None:
        path = f"{repo_path(workspace, repo)}/commits"
    else:
        encoded = quote(branch.strip(), safe="")
        path = f"{repo_path(workspace, repo)}/commits/{encoded}"

    out: list[dict[str, Any]] = []
    for c in client.paginate(path, query={"pagelen": pagelen}):
        out.append(c)
        if len(out) >= count:
            break
    return out
