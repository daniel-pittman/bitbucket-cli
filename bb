#!/usr/bin/env bash
#
# bb - Bitbucket CLI wrapper
#
# A local CLI for Bitbucket Cloud that wraps the REST API.
# Requires: curl, jq
#
# Configuration:
#   Set BB_USER, BB_TOKEN, and BB_WORKSPACE in ~/.config/bb/config
#   or as environment variables.
#
#   BB_WORKSPACE must be set (no default).
#
# Token setup:
#   1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
#   2. Create an API token
#   3. Set BB_USER to your Bitbucket email address
#   4. Set BB_TOKEN to the generated token
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BB_API="https://api.bitbucket.org/2.0"

# --- Config loading ---

load_config() {
    # Snapshot env-provided values BEFORE sourcing the config files.
    # `source ~/.config/bb/config` runs `BB_USER=...` etc., which would
    # otherwise clobber values the user exported in their shell —
    # inverting the documented precedence (env vars are meant to be
    # highest priority) and diverging from the Python bb_api.load_config,
    # which resolves env first. We re-apply these snapshots after
    # sourcing so the env still wins.
    local _env_user="${BB_USER:-}" _env_token="${BB_TOKEN:-}"
    local _env_ws="${BB_WORKSPACE:-}" _env_api="${BB_API_BASE:-}"

    if [[ -f "$HOME/.config/bb/config" ]]; then
        # shellcheck source=/dev/null
        source "$HOME/.config/bb/config"
    fi
    if [[ -f "$SCRIPT_DIR/.env" ]]; then
        # shellcheck source=/dev/null
        source "$SCRIPT_DIR/.env"
    fi

    # Re-apply env snapshots (highest priority), matching the documented
    # order and the Python resolve() behaviour.
    [[ -n "$_env_user" ]] && BB_USER="$_env_user"
    [[ -n "$_env_token" ]] && BB_TOKEN="$_env_token"
    [[ -n "$_env_ws" ]] && BB_WORKSPACE="$_env_ws"
    [[ -n "$_env_api" ]] && BB_API_BASE="$_env_api"

    # Wire BB_API_BASE into the base URL every curl call uses. Without
    # this the variable was snapshotted + re-applied but never consulted,
    # so `BB_API_BASE=https://staging... bb prs` silently hit production
    # — and bb_api.py honours it, so the CLI was the odd one out. Strip a
    # trailing slash to match Python's api_base.rstrip("/") normalisation
    # (avoids "//path").
    if [[ -n "${BB_API_BASE:-}" ]]; then
        BB_API="${BB_API_BASE%/}"
    fi

    # BB_WORKSPACE is now OPTIONAL: when running inside a Bitbucket git
    # checkout, resolve_repo auto-detects the workspace from the origin
    # remote, and the -w/--workspace flag or a "workspace/slug" argument
    # can supply it per-command. Only BB_USER + BB_TOKEN are mandatory
    # (auth). A command that needs a workspace but can't resolve one
    # from any source fails at that point (resolve_repo / repo_path)
    # with a clear, actionable message.
    if [[ -z "${BB_USER:-}" || -z "${BB_TOKEN:-}" ]]; then
        echo "Error: BB_USER and BB_TOKEN must be set." >&2
        echo "" >&2
        echo "Quick setup:" >&2
        echo "  mkdir -p ~/.config/bb" >&2
        echo "  cat > ~/.config/bb/config <<EOF" >&2
        echo "BB_USER=your-email@example.com" >&2
        echo "BB_TOKEN=your-api-token" >&2
        echo "BB_WORKSPACE=your-workspace   # optional — auto-detected in a git checkout" >&2
        echo "EOF" >&2
        echo "" >&2
        echo "Create an API token at:" >&2
        echo "  https://id.atlassian.com/manage-profile/security/api-tokens" >&2
        echo "" >&2
        echo "BB_USER is your Bitbucket account email address." >&2
        exit 1
    fi
}

# --- API helpers ---
#
# curl's `-f` is deliberately absent from every request below. `-f`
# suppresses the response body on 4xx/5xx, and Bitbucket's error body is
# the only place the actual cause appears: a 403 names the exact scope
# the token is missing AND the scopes it carries, under
# `error.detail.{required,granted}`. Discarding that leaves a bare exit
# code, which is why commands used to compensate with hardcoded guesses
# ("if this is a 403, the token probably lacks X") that were speculative
# and wrong whenever the real cause differed (expired token, wrong
# workspace slug, deleted resource, rate limit).
#
# `-f`'s exit-code contract is preserved so callers do not have to
# change: HTTP >= 400 returns 22, transport failures (DNS / TLS /
# connection refused) return curl's own exit code. Only stderr gains the
# real message.
#
# --fail-with-body would do this in one flag but needs curl 7.76+ (2021);
# the status is captured via `-w` instead so the floor stays where the
# rest of the script's (bash 3.2 / macOS system tooling) floor is.

# Redact credential-shaped substrings from anything echoed to the
# terminal: the token itself, and URL-embedded `user:secret@host` forms
# an upstream proxy or redirect target could echo back. Mirrors the
# Python `_safe_text` chokepoint, so a leak through an error body needs
# a new vector on both surfaces rather than just one.
_redact() {
    local text="$1"
    if [[ -n "${BB_TOKEN:-}" ]]; then
        text="${text//"$BB_TOKEN"/[redacted]}"
    fi
    printf '%s' "$text" | sed -E 's#([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@[:space:]]+:[^/@[:space:]]*@#\1[redacted]@#g'
}

# Print the API's own explanation of a failed request to stderr.
# Bitbucket's envelope is {"type":"error","error":{"message":…,"detail":…}}
# where `detail` is either a string or, for a scope denial, the object
# {"required":[…],"granted":[…]} — the single most actionable payload the
# API returns, and the reason this function exists.
_print_api_error() {
    local status="$1" body="$2" method="$3" path="$4"
    echo "Error: HTTP ${status} on ${method} ${path}" >&2

    local msg detail required granted excerpt
    if msg=$(printf '%s' "$body" | jq -re '.error.message' 2>/dev/null); then
        echo "  $(_redact "$msg")" >&2

        detail=$(printf '%s' "$body" \
            | jq -r 'if (.error.detail | type) == "string" then .error.detail else empty end' \
            2>/dev/null) || detail=""
        if [[ -n "$detail" ]]; then
            echo "  $(_redact "$detail")" >&2
        fi

        required=$(printf '%s' "$body" \
            | jq -r '(.error.detail.required // []) | join(", ")' 2>/dev/null) || required=""
        granted=$(printf '%s' "$body" \
            | jq -r '(.error.detail.granted // []) | join(", ")' 2>/dev/null) || granted=""
        if [[ -n "$required" ]]; then
            echo "  required scopes: ${required}" >&2
        fi
        if [[ -n "$granted" ]]; then
            echo "  granted scopes:  ${granted}" >&2
        fi
    elif [[ -n "$body" ]]; then
        # Not a Bitbucket error envelope (an HTML error page from a proxy,
        # a gateway timeout, ...). A bounded excerpt still beats silence.
        excerpt="${body:0:500}"
        echo "  $(_redact "$excerpt")" >&2
        if [[ "${#body}" -gt 500 ]]; then
            echo "  ... (body truncated at 500 characters)" >&2
        fi
    fi

    # Scopes are fixed when a token is issued: granting one later does not
    # apply to an already-issued token, so a 401/403 always ends at the
    # same place. This is the only non-API text printed here, and it is
    # not a guess about the cause — the cause is quoted above it.
    if [[ "$status" == "401" ]]; then
        echo "  The token is invalid, expired, or revoked. Issue a new one at" >&2
        echo "  https://id.atlassian.com/manage-profile/security/api-tokens" >&2
    elif [[ "$status" == "403" ]]; then
        echo "  A token's scopes are fixed at creation. To add one, create or" >&2
        echo "  rotate the token at" >&2
        echo "  https://id.atlassian.com/manage-profile/security/api-tokens" >&2
    fi
    return 0
}

# Perform a request and echo the response body with the HTTP status
# appended on its own line. Prints no diagnosis of its own (beyond the
# BB_DEBUG trace) so callers can decide what a given status means —
# `bb pipelines-status`, for instance, treats 404 as "never configured"
# rather than an error. Returns curl's exit code on a transport failure.
#
# Body and status are recovered with the suffix/prefix split below rather
# than by reading two streams. The status is appended LAST because an
# empty body (a 204, say) then still yields a parseable "\n204" — leading
# a response with the status would collapse to an unsplittable "204".
_bb_http() {
    local method="$1" path="$2" data="${3:-}"
    shift 3
    local -a args
    args=(-s -w '\n%{http_code}' -u "${BB_USER}:${BB_TOKEN}")
    if [[ "$method" != "GET" ]]; then
        args+=(-X "$method")
    fi
    if [[ -n "$data" ]]; then
        args+=(-H "Content-Type: application/json" -d "$data")
    fi

    local raw rc=0
    raw=$(curl "${args[@]}" "${BB_API}${path}" "$@") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        echo "Error: request failed before any HTTP response (curl exit ${rc})." >&2
        echo "  ${method} ${path}" >&2
        echo "  This is a connectivity error, not an API rejection." >&2
        return "$rc"
    fi

    if [[ -n "${BB_DEBUG:-}" ]]; then
        # Endpoint + status only. The token is never part of a URL (it
        # rides in the Basic auth header), so nothing here is sensitive.
        echo "[bb] ${method} ${path} -> ${raw##*$'\n'}" >&2
    fi

    printf '%s' "$raw"
}

# The default policy over _bb_http: 2xx writes the body to stdout,
# anything else reports the API's own error and returns 22.
_bb_request() {
    local method="$1" path="$2" data="${3:-}"
    shift 3

    local raw rc=0
    raw=$(_bb_http "$method" "$path" "$data" "$@") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        return "$rc"
    fi

    local status="${raw##*$'\n'}"
    local body="${raw%$'\n'*}"

    case "$status" in
        2*)
            printf '%s' "$body"
            ;;
        *)
            _print_api_error "$status" "$body" "$method" "$path"
            return 22
            ;;
    esac
}

bb_get() {
    local path="$1"
    shift
    _bb_request GET "$path" "" "$@"
}

bb_post() {
    local path="$1"
    local data="${2:-}"
    _bb_request POST "$path" "$data"
}

bb_put() {
    local path="$1"
    local data="${2:-}"
    _bb_request PUT "$path" "$data"
}

bb_delete() {
    local path="$1"
    _bb_request DELETE "$path" ""
}

# Resolve the (workspace, repo-slug) pair for a command from its
# optional repo argument, and publish the result by SETTING two
# variables in the CALLER's scope:
#   repo          — the repo slug
#   BB_WORKSPACE  — the workspace to operate in
#
# This is called WITHOUT command substitution (i.e. `resolve_repo "$1"`,
# not `repo=$(resolve_repo "$1")`) precisely so it runs in the caller's
# shell and its assignments to `repo` (a caller `local`, reached via
# bash dynamic scope) and `BB_WORKSPACE` (global) actually propagate.
# A subshell — which is what the old `detect_repo` ran in — cannot set
# the parent's workspace, which is why workspace auto-detect needs this
# shape.
#
# Resolution precedence (highest first) — mirrors the Python
# _resolve_repo contract in mcp_server.py:
#   1. -w/--workspace flag        (BB_WORKSPACE_OVERRIDE, set pre-dispatch)
#   2. explicit "workspace/slug"  (overrides workspace for this call)
#   3. git origin auto-detect     (workspace + slug from the remote URL)
#   4. BB_WORKSPACE default       (bare "slug" arg, or env/config default)
#   5. error                      (nothing resolved a workspace)
resolve_repo() {
    local arg="${1:-}"
    # A -w/--workspace flag locks the workspace: it beats both the git
    # origin and any "ws/slug" arg. Detected by BB_WORKSPACE_OVERRIDE
    # being set (the dispatcher records it before load_config).
    local ws_locked=""
    [[ -n "${BB_WORKSPACE_OVERRIDE:-}" ]] && ws_locked=1

    if [[ -z "$arg" ]]; then
        # (3) Auto-detect from the git origin remote.
        local remote_url
        remote_url=$(git remote get-url origin 2>/dev/null || true)
        if [[ -z "$remote_url" ]]; then
            echo "Error: no repo specified and not in a git repository." >&2
            echo "  Pass a repo (bb <cmd> myrepo) or workspace/repo" >&2
            echo "  (bb <cmd> acme/myrepo), or run inside a git checkout." >&2
            exit 1
        fi
        # Strip one trailing slash so `.../repo/` parses, then match the
        # tail. Greedy [^/]+ is fine here because we strip `.git`
        # afterward (bash ERE has no non-greedy quantifier, unlike the
        # Python _REMOTE_TAIL regex — same end result via the %.git
        # parameter expansion below).
        remote_url="${remote_url%/}"
        if [[ "$remote_url" =~ [:/]([^/:]+)/([^/]+)$ ]]; then
            local detected_ws="${BASH_REMATCH[1]}"
            local detected_repo="${BASH_REMATCH[2]%.git}"
            repo="$detected_repo"
            # Flag wins over git-detected workspace; else use git's.
            [[ -z "$ws_locked" ]] && BB_WORKSPACE="$detected_ws"
        else
            echo "Error: could not parse workspace/repo from origin URL." >&2
            exit 1
        fi
    elif [[ "$arg" == */* ]]; then
        # (2) Explicit "workspace/slug" override.
        local arg_ws="${arg%%/*}"
        local arg_repo="${arg#*/}"
        if [[ "$arg_repo" == */* ]]; then
            echo "Error: repo must be 'slug' or 'workspace/slug' (one '/'), got '$arg'." >&2
            exit 1
        fi
        repo="$arg_repo"
        # The -w flag still wins over an inline ws/slug (flag is the
        # most explicit, per-invocation signal).
        [[ -z "$ws_locked" ]] && BB_WORKSPACE="$arg_ws"
    else
        # (4) Bare slug → use whatever BB_WORKSPACE already resolved to
        # (flag override, else env/config default). repo_path enforces
        # that BB_WORKSPACE is actually set and well-formed.
        repo="$arg"
    fi
}

# Resolve JUST the workspace for workspace-level commands that take no
# repo argument (e.g. `bb repos`). Sets BB_WORKSPACE in the caller's
# scope. Same precedence as resolve_repo, minus the slug:
#   1. -w/--workspace flag      (BB_WORKSPACE_OVERRIDE locks it)
#   2. git origin auto-detect   (workspace from the remote URL)
#   3. BB_WORKSPACE default     (env / config)
#   4. error
resolve_workspace() {
    # -w flag locks the workspace (most explicit signal).
    [[ -n "${BB_WORKSPACE_OVERRIDE:-}" ]] && return

    # git origin wins over the config default — "operate on where I am".
    local remote_url
    remote_url=$(git remote get-url origin 2>/dev/null || true)
    if [[ -n "$remote_url" ]]; then
        remote_url="${remote_url%/}"
        if [[ "$remote_url" =~ [:/]([^/:]+)/([^/]+)$ ]]; then
            BB_WORKSPACE="${BASH_REMATCH[1]}"
            return
        fi
    fi

    # Fall back to env/config BB_WORKSPACE; error if nothing resolved one.
    if [[ -z "${BB_WORKSPACE:-}" ]]; then
        echo "Error: no workspace resolved." >&2
        echo "  Set BB_WORKSPACE (env or ~/.config/bb/config), pass" >&2
        echo "  -w <workspace>, or run inside a Bitbucket git checkout." >&2
        exit 1
    fi
}

# Resolve positional args for PR commands of shape `bb <verb> [repo] <id> [extras...]`.
# Sets caller-scope:
#   repo                   -- via resolve_repo (caller must `local repo`)
#   pr_id                  -- the PR id (caller must `local pr_id`)
#   pr_args_consumed       -- 1 or 2 (caller `shift $pr_args_consumed` to reach extras)
#
# Heuristic: if $1 is purely digits, treat it as the id and auto-detect the
# repo from git. Otherwise $1 is the [repo] slot and $2 is the id (the
# existing positional form, unchanged). This makes `bb pr 42` Just Work
# from inside a checkout instead of treating 42 as a repo slug. Mirrors
# the state-recognition heuristic in cmd_pr_list (bb prs MERGED).
#
# Tradeoff: a repo literally named with pure digits (e.g. slug "42") would
# be shadowed by the heuristic. Acceptable: Bitbucket slugs by convention
# are lowercase-hyphenated, pure-digit slugs are vanishingly rare, and the
# explicit "workspace/42" form remains as a clean escape hatch (the
# heuristic only triggers on the bare-digits form).
_resolve_pr_args() {
    case "${1:-}" in
        ''|*[!0-9]*)
            resolve_repo "${1:-}"
            pr_id="${2:-}"
            pr_args_consumed=2
            ;;
        *)
            resolve_repo ""
            pr_id="$1"
            pr_args_consumed=1
            ;;
    esac
    # Validate non-empty ids here so every PR command inherits the guard.
    # An empty pr_id is left to each command's own usage-error check;
    # a non-empty one must be a positive integer before it reaches a URL.
    [[ -n "$pr_id" ]] && _require_pr_id "$pr_id"
}

repo_path() {
    local repo="$1"
    # Validate inputs at the boundary so a malformed slug doesn't
    # silently construct a wrong URL (/repositories//foo or
    # /repositories/../foo). Mirrors the bb_api.repo_path Python
    # validation — both surfaces enforce the same contract.
    #
    # Whitespace check: reject if the value EQUALS its whitespace-
    # stripped form's emptiness. Catches all-whitespace AND mixed-
    # whitespace (e.g. ` acme ` which the previous `^[[:space:]]+$`
    # regex let through). `tr -d '[:space:]'` is true parity with
    # Python's `.strip()`.
    local _ws_stripped _repo_stripped
    _ws_stripped="$(printf '%s' "$BB_WORKSPACE" | tr -d '[:space:]')"
    _repo_stripped="$(printf '%s' "$repo" | tr -d '[:space:]')"
    if [[ -z "$_ws_stripped" || "$_ws_stripped" != "$BB_WORKSPACE" ]]; then
        echo "Error: BB_WORKSPACE must be a non-empty, non-whitespace string." >&2
        kill -TERM $$
    fi
    if [[ -z "$_repo_stripped" || "$_repo_stripped" != "$repo" ]]; then
        echo "Error: repo must be a non-empty, non-whitespace string." >&2
        kill -TERM $$
    fi
    if [[ "$BB_WORKSPACE" == *"/"* || "$repo" == *"/"* ]]; then
        echo "Error: workspace and repo must not contain '/'." >&2
        kill -TERM $$
    fi
    if [[ "$BB_WORKSPACE" == "." || "$BB_WORKSPACE" == ".." ]]; then
        echo "Error: workspace must not be '.' or '..'." >&2
        kill -TERM $$
    fi
    if [[ "$repo" == "." || "$repo" == ".." ]]; then
        echo "Error: repo must not be '.' or '..'." >&2
        kill -TERM $$
    fi
    # `exit 1` inside a `$(repo_path ...)` command substitution only
    # terminates the subshell — the caller would proceed with an
    # empty path. `kill -TERM $$` terminates the parent script so
    # the validation actually halts execution.
    echo "/repositories/${BB_WORKSPACE}/${repo}"
}

# Validate a build_number argument is a positive integer before
# splicing into a jq filter or URL. jq treats unquoted non-numeric
# identifiers as undefined-function references (e.g. `select(.x == abc)`
# becomes `abc/0 is not defined`), aborting under `set -e` with no
# curated error. A crafted value like `1) | $ENV.BB_TOKEN, .uuid` can
# also exfil environment via the jq filter's $ENV — validate the shape
# at the boundary so neither failure mode can fire.
_require_build_number() {
    if ! [[ "$1" =~ ^[0-9]+$ ]]; then
        echo "Error: build_number must be a positive integer (got ${1:-empty})." >&2
        exit 1
    fi
}

# Validate a PR id before it is interpolated into a request URL path
# (pullrequests/{id}, .../merge, .../approve, .../decline). Mirrors the
# Python _validate_pr_id / _is_positive_int contract (positive integer).
# Without this, a non-numeric id manipulates the URL path on mutation
# endpoints — the bash side previously trusted it while Python did not.
_require_pr_id() {
    if ! [[ "$1" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: pr-id must be a positive integer (got ${1:-empty})." >&2
        exit 1
    fi
}

# Validate a pipeline step index before it is interpolated into a jq
# PROGRAM (.values[${step_index}].uuid). This is a security boundary,
# NOT just a usability check: an unvalidated step index is a jq injection
# surface — a value like `0].uuid,$ENV.BB_TOKEN,.values[0` makes jq emit
# the token, breaking the "BB_TOKEN never echoed" posture. Sibling of
# _require_build_number (which closed the same class for build_number in
# PR #8); step_index was missed. Mirrors the Python step_index guard
# (non-negative int, used as a list index, never interpolated).
_require_step_index() {
    if ! [[ "$1" =~ ^[0-9]+$ ]]; then
        echo "Error: step-index must be a non-negative integer (got ${1:-empty})." >&2
        exit 1
    fi
}

# Validate a user-supplied result count before it is interpolated into a
# query string (pagelen=${count}). Same query-param-injection class as
# _require_pr_state (which closed `bb prs 'OPEN&pagelen=1000'`): a count
# like `10&role=admin` would smuggle extra params. Mirrors the Python
# _is_positive_int guard on count.
_require_count() {
    if ! [[ "$1" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: count must be a positive integer (got ${1:-empty})." >&2
        exit 1
    fi
}

# Validate a resolved workspace slug at the boundary before it's
# interpolated into a workspace-level request URL (e.g.
# /workspaces/{ws}/projects). Mirrors the inline contract in
# bb_ops.projects_list / repos_list: reject empty / whitespace, embedded
# '/', and '.' / '..'. The repo-level commands get this for free via
# repo_path, but the workspace-only commands (cmd_projects) don't route
# through it, so the check is centralised here instead of duplicated.
#
# The whitespace check uses `tr -d '[:space:]'` (the same idiom repo_path
# uses for BB_WORKSPACE) and rejects if the stripped form is empty OR
# differs from the input. Note this is STRICTER than Python's `.strip()`:
# it rejects ANY whitespace including interior (`a b`), not just
# leading/trailing. That's intentional and safe for a workspace slug,
# which can never legitimately contain a space — unlike a `--project KEY`,
# where cmd_repo_update strips leading/trailing only (true `.strip()`).
_require_reviewer_uuid() {
    # Bitbucket identifies PR reviewers ONLY by account UUID. A display
    # name or nickname is accepted by nothing, and sending one costs a
    # round trip to learn that; reject it here with a pointer to the
    # lookup instead. `bb members` prints the UUID column this wants.
    #
    # Both brace forms are accepted: `bb members` and the API emit the
    # braced `{8-4-4-4-12}`, but users routinely strip the braces when
    # copying. Exactly one MATCHED pair is stripped before the check, so a
    # half-brace (`{abc…` with no closer) still fails.
    local raw="${1:-}"
    local core="$raw"
    if [[ "$raw" == "{"*"}" ]]; then
        core="${raw#\{}"
        core="${core%\}}"
    fi
    if [[ ! "$core" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
        echo "Error: --reviewer must be a Bitbucket account UUID (got '$raw')." >&2
        echo "  Reviewers are identified by UUID, not by name or nickname." >&2
        echo "  Run 'bb members' to list workspace members and their UUIDs." >&2
        exit 1
    fi
    # Canonicalise to the BRACED form, which is what the API returns. The
    # reviewer-set arithmetic in pr-update compares these strings against
    # `.reviewers[].uuid` and `.participants[].user.uuid` from a live
    # response, so an accepted-but-bare value would match nothing: the
    # removal would silently no-op and an add would append a duplicate.
    # Accepting both forms only works if they converge here.
    #
    # Set in the CALLER's scope rather than echoed: `exit 1` inside a
    # `$( )` command substitution kills only the subshell, so a rejected
    # value would slip through as an empty string. Same reason resolve_repo
    # assigns instead of printing.
    REVIEWER_UUID="{${core}}"
}

_require_workspace() {
    local ws="$1"
    local stripped
    stripped="$(printf '%s' "$ws" | tr -d '[:space:]')"
    if [[ -z "$stripped" || "$stripped" != "$ws" ]]; then
        echo "Error: workspace must be a non-empty, non-whitespace string (got '$ws')." >&2
        exit 1
    fi
    if [[ "$ws" == */* ]]; then
        echo "Error: workspace must not contain '/' (got '$ws')." >&2
        exit 1
    fi
    if [[ "$ws" == "." || "$ws" == ".." ]]; then
        echo "Error: workspace must not be '.' or '..' (got '$ws')." >&2
        exit 1
    fi
}

# Allowlist the PR state before it's interpolated into the request URL.
# Mirrors the Python _KNOWN_PR_STATES boundary check (bb_ops.py) — both
# surfaces reject anything outside the four valid states. Also closes a
# query-param injection surface: without this, `bb prs my-repo
# 'OPEN&pagelen=1000'` would smuggle extra query params into the URL.
_require_pr_state() {
    case "$1" in
        OPEN|MERGED|DECLINED|SUPERSEDED) ;;
        *)
            echo "Error: state must be one of OPEN, MERGED, DECLINED, SUPERSEDED (got '$1')." >&2
            exit 1
            ;;
    esac
}

# --- Formatting helpers ---

format_state() {
    local state="$1"
    case "$state" in
        COMPLETED)  echo "DONE" ;;
        SUCCESSFUL) echo "PASS" ;;
        RUNNING)    echo "RUN " ;;
        PENDING)    echo "WAIT" ;;
        FAILED)     echo "FAIL" ;;
        ERROR)      echo "ERR " ;;
        STOPPED)    echo "STOP" ;;
        PAUSED)     echo "HOLD" ;;
        HALTED)     echo "HALT" ;;
        OPEN)       echo "OPEN" ;;
        MERGED)     echo "MRGD" ;;
        DECLINED)   echo "DECL" ;;
        SUPERSEDED) echo "SUPD" ;;
        *)          echo "$state" ;;
    esac
}

format_duration() {
    local seconds="$1"
    if [[ "$seconds" -lt 60 ]]; then
        echo "${seconds}s"
    elif [[ "$seconds" -lt 3600 ]]; then
        echo "$((seconds / 60))m $((seconds % 60))s"
    else
        echo "$((seconds / 3600))h $((seconds % 3600 / 60))m"
    fi
}

# =========================================================================
#  PIPELINE COMMANDS
# =========================================================================

cmd_pipelines() {
    local repo
    resolve_repo "${1:-}"
    local count="${2:-10}"
    _require_count "$count"

    echo "Pipelines for ${BB_WORKSPACE}/${repo}:"
    echo ""

    local response
    response=$(bb_get "$(repo_path "$repo")/pipelines/?sort=-created_on&pagelen=${count}")

    printf "  %-7s %-6s %-22s %-18s %-12s %s\n" "BUILD" "STATE" "BRANCH" "TRIGGER" "DATE" "DURATION"
    printf "  %-7s %-6s %-22s %-18s %-12s %s\n" "-----" "-----" "------" "-------" "----" "--------"

    echo "$response" | jq -r '
        .values[] |
        [
            (.build_number | tostring),
            .state.name,
            (.state.result.name // .state.stage.name // "-"),
            (.target.ref_name // "n/a"),
            (.target.selector.pattern // .trigger.name // "-"),
            (.created_on | split("T") | .[0]),
            (.duration_in_seconds // 0 | tostring)
        ] | join("\t")
    ' | while IFS=$'\t' read -r num state result ref trigger date duration; do
        local display_state
        if [[ -n "$result" ]]; then
            display_state=$(format_state "$result")
        else
            display_state=$(format_state "$state")
        fi

        local dur_str="-"
        if [[ "$duration" != "0" && "$duration" != "null" ]]; then
            dur_str=$(format_duration "$duration")
        fi

        printf "  #%-6s %-6s %-22s %-18s %-12s %s\n" \
            "$num" "$display_state" "$ref" "$trigger" "$date" "$dur_str"
    done
}

cmd_pipeline() {
    local repo
    resolve_repo "${1:-}"
    local build_number="${2:-}"

    if [[ -z "$build_number" ]]; then
        echo "Usage: bb pipeline [repo] <build-number>" >&2
        exit 1
    fi
    _require_build_number "$build_number"

    # Parity fix: bumped pagelen 50→100 (Bitbucket's max). Older
    # pipelines still unfindable beyond 100; full pagination is
    # the Python-side improvement.
    local response
    response=$(bb_get "$(repo_path "$repo")/pipelines/?sort=-created_on&pagelen=100")

    local pipeline_uuid
    pipeline_uuid=$(echo "$response" | jq -r ".values[] | select(.build_number == ${build_number}) | .uuid" | tr -d '{}')

    if [[ -z "$pipeline_uuid" ]]; then
        echo "Pipeline #${build_number} not found." >&2
        exit 1
    fi

    local pipeline
    pipeline=$(bb_get "$(repo_path "$repo")/pipelines/%7B${pipeline_uuid}%7D")

    echo "Pipeline #${build_number} - ${BB_WORKSPACE}/${repo}"
    echo ""
    echo "$pipeline" | jq -r '
        "  Branch:   " + (.target.ref_name // "n/a"),
        "  Trigger:  " + (.target.selector.pattern // .trigger_name // "n/a"),
        "  State:    " + .state.name + (if .state.result then " / " + .state.result.name else "" end),
        "  Created:  " + .created_on,
        "  Duration: " + (if .duration_in_seconds then (.duration_in_seconds | tostring) + "s" else "in progress" end)
    '

    echo ""
    echo "  Steps:"

    local steps
    steps=$(bb_get "$(repo_path "$repo")/pipelines/%7B${pipeline_uuid}%7D/steps/?pagelen=50")

    echo "$steps" | jq -r '
        .values[] |
        "    " +
        (if .state.result then .state.result.name else .state.name end) +
        "  " + .name +
        (if .duration_in_seconds then " (" + (.duration_in_seconds | tostring) + "s)" else "" end)
    '
}

cmd_watch() {
    local repo
    resolve_repo "${1:-}"
    local build_number="${2:-}"
    local poll_interval="${3:-15}"

    if [[ -z "$build_number" ]]; then
        local latest
        latest=$(bb_get "$(repo_path "$repo")/pipelines/?sort=-created_on&pagelen=1")
        build_number=$(echo "$latest" | jq -r '.values[0].build_number')
        echo "Watching most recent pipeline: #${build_number}"
    fi
    _require_build_number "$build_number"

    echo "Watching pipeline #${build_number} on ${BB_WORKSPACE}/${repo} (every ${poll_interval}s)..."
    echo ""

    while true; do
        # Parity fix: bumped pagelen 50 → 100, symmetric with
        # cmd_pipeline / cmd_pipeline_stop / cmd_logs. Without this
        # bump, a pipeline at positions 51-100 in the recent list
        # would never match here and the watch loop would spin
        # forever printing blanks.
        local response
        response=$(bb_get "$(repo_path "$repo")/pipelines/?sort=-created_on&pagelen=100")

        local state result duration ref
        IFS=$'\t' read -r state result duration ref < <(echo "$response" | jq -r "
            .values[] | select(.build_number == ${build_number}) |
            [.state.name, (.state.result.name // \"-\"), (.duration_in_seconds // 0 | tostring), (.target.ref_name // \"n/a\")] | join(\"\t\")
        ")

        local display_state
        if [[ -n "$result" && "$result" != "-" ]]; then
            display_state=$(format_state "$result")
        else
            display_state=$(format_state "$state")
        fi

        local dur_str=""
        if [[ "$duration" =~ ^[0-9]+$ && "$duration" != "0" ]]; then
            dur_str=" ($(format_duration "$duration"))"
        fi

        printf "\r  #%-6s %-6s %-22s%s    " "$build_number" "$display_state" "$ref" "$dur_str"

        if [[ "$state" == "COMPLETED" ]]; then
            echo ""
            echo ""
            echo "Pipeline finished: ${result}"
            cmd_pipeline "$repo" "$build_number" 2>/dev/null | grep -A 100 "Steps:" || true
            return 0
        fi

        sleep "$poll_interval"
    done
}

cmd_logs() {
    local repo
    resolve_repo "${1:-}"
    local build_number="${2:-}"
    local step_index="${3:-}"

    if [[ -z "$build_number" ]]; then
        echo "Usage: bb logs [repo] <build-number> [step-index]" >&2
        exit 1
    fi
    _require_build_number "$build_number"
    # Validate step_index at the boundary — before any network call and
    # before the jq interpolation below (jq injection / token-exfil
    # boundary; see _require_step_index). Empty step_index takes the
    # list-steps path, so only guard the non-empty case here. This keeps
    # "zero network IO on bad input" parity with the Python side.
    [[ -n "$step_index" ]] && _require_step_index "$step_index"

    # Parity fix: bumped pagelen 50→100 (Bitbucket's max). Symmetric
    # with cmd_pipeline_stop / cmd_pipeline.
    local response
    response=$(bb_get "$(repo_path "$repo")/pipelines/?sort=-created_on&pagelen=100")

    local pipeline_uuid
    pipeline_uuid=$(echo "$response" | jq -r ".values[] | select(.build_number == ${build_number}) | .uuid" | tr -d '{}')

    if [[ -z "$pipeline_uuid" ]]; then
        echo "Pipeline #${build_number} not found." >&2
        exit 1
    fi

    local steps
    steps=$(bb_get "$(repo_path "$repo")/pipelines/%7B${pipeline_uuid}%7D/steps/?pagelen=50")

    if [[ -z "$step_index" ]]; then
        echo "Steps for pipeline #${build_number}:"
        echo ""
        echo "$steps" | jq -r '
            .values | to_entries[] |
            "  [" + (.key | tostring) + "] " + .value.name +
            " (" + (if .value.state.result then .value.state.result.name else .value.state.name end) + ")"
        '
        echo ""
        echo "Usage: bb logs ${repo} ${build_number} <step-index>"
        return
    fi

    # step_index is validated at the top (before any network call), so
    # the interpolation below is safe.
    local step_uuid
    step_uuid=$(echo "$steps" | jq -r ".values[${step_index}].uuid" | tr -d '{}')

    if [[ "$step_uuid" == "null" || -z "$step_uuid" ]]; then
        echo "Step index ${step_index} not found." >&2
        exit 1
    fi

    local step_name
    step_name=$(echo "$steps" | jq -r ".values[${step_index}].name")
    echo "Logs for step [${step_index}] ${step_name}:"
    echo ""

    # -L because a log can be served as a 307 to a signed object-store URL.
    # `curl -L -u` does not resend credentials to a different host, so the
    # Bitbucket Basic header never reaches the redirect target.
    #
    # A failure here used to be swallowed (`2>/dev/null` plus a fallback
    # line), which reported "no log output" for a missing scope, an
    # expired token, and a genuinely empty log alike. bb_get now prints
    # the API's own reason first, and the fallback line still marks the
    # request as having produced nothing. It fires only on failure: a
    # successful but empty log exits 0 and prints nothing at all.
    bb_get "$(repo_path "$repo")/pipelines/%7B${pipeline_uuid}%7D/steps/%7B${step_uuid}%7D/log" -L \
        || echo "(no log output available)"
}

cmd_pipeline_trigger() {
    # bb trigger [repo] [branch] [pattern] [--var KEY=VALUE ...] [KEY=VALUE ...]
    #
    # Per-run pipeline variables arrive two ways, combined in order:
    #   --var/-v KEY=VALUE   repeatable, position-independent (preferred)
    #   trailing KEY=VALUE   positional pairs after [pattern] (legacy form)
    # The variables need not be declared in bitbucket-pipelines.yml; the
    # API accepts arbitrary per-run keys alongside the target selector.
    local -a var_pairs=() positionals=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --var|-v)  _require_flag_value "$@"; var_pairs+=("$2"); shift 2 ;;
            --var=*)   var_pairs+=("${1#*=}"); shift ;;
            -*)
                echo "Error: unknown flag for trigger: $1" >&2
                echo "Usage: bb trigger [repo] [branch] [pattern] [--var KEY=VALUE ...]" >&2
                exit 1 ;;
            *)         positionals+=("$1"); shift ;;
        esac
    done

    local repo
    resolve_repo "${positionals[0]:-}"
    local branch="${positionals[1]:-}"
    local pattern="${positionals[2]:-}"

    # Positionals past [pattern] are legacy KEY=VALUE pairs (the
    # pre-flag form, still supported so documented invocations keep
    # working). They merge after the --var pairs.
    local _i
    for (( _i=3; _i < ${#positionals[@]}; _i++ )); do
        var_pairs+=("${positionals[$_i]}")
    done

    # Validate every pair BEFORE any API call. A pair with no '=' or a
    # malformed key must fail loudly here; otherwise it is silently
    # sent as {"key": <arg>, "value": ""} and Bitbucket runs the build
    # with a garbage variable. The key charset is Bitbucket's documented
    # rule for variable names.
    local _pair _key
    for _pair in ${var_pairs[@]+"${var_pairs[@]}"}; do
        if [[ "$_pair" != *=* ]]; then
            echo "Error: pipeline variable must be KEY=VALUE, got '$_pair'." >&2
            exit 1
        fi
        _key="${_pair%%=*}"
        if ! [[ "$_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            echo "Error: invalid pipeline variable name '$_key'." >&2
            echo "  Names use letters, digits, and underscores, and must not start with a digit." >&2
            exit 1
        fi
    done

    # Build the variables array via `jq` so values containing `"`, `\`,
    # newlines, or tabs are correctly JSON-escaped. Each pair rides in as
    # its OWN jq argument ($ARGS.positional, jq >= 1.6), never as a
    # delimited stream. A delimiter cannot work here: bash arguments are
    # C strings, so a NUL delimiter embedded in the jq program is dropped
    # by the shell (jq then sees split("") and shreds the stream into
    # per-character ghost variables), and any printable delimiter could
    # collide with a variable's value.
    local variables="[]"
    if [[ ${#var_pairs[@]} -gt 0 ]]; then
        # Per-pair shape: split on the FIRST `=` only so values
        # containing `=` survive intact.
        variables=$(jq -n '
            [$ARGS.positional[] | split("=") | {
                key: .[0],
                value: (.[1:] | join("="))
            }]' --args "${var_pairs[@]}")
    fi

    if [[ -z "$branch" ]]; then
        branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
        # git rev-parse exits 0 with literal "HEAD" on detached HEAD —
        # Bitbucket would 400 on ref_name=HEAD. Surface a clean error.
        if [[ "$branch" == "HEAD" ]]; then
            echo "Error: detached HEAD detected. Pass an explicit branch." >&2
            exit 1
        fi
    fi

    # Parity fix: previously the no-pattern branch built `{target: {...}}`
    # WITHOUT the variables field, silently dropping any VAR=value args
    # the user passed. Build the payload incrementally so variables always
    # land in the request when provided, regardless of pattern.
    #
    # The target MUST carry `type: "pipeline_ref_target"`. Without it
    # Bitbucket 400s with "Unsupported reference target provided
    # 'pipeline_unknown_target'" — fatal on the custom-pattern path
    # (verified live), and the field is correct for the default-branch path
    # too, so it's sent unconditionally for parity with bb_ops.
    local payload
    if [[ "$variables" != "[]" ]]; then
        if [[ -n "$pattern" ]]; then
            payload=$(jq -n \
                --arg ref "$branch" \
                --arg pat "$pattern" \
                --argjson vars "$variables" \
                '{target: {type: "pipeline_ref_target", ref_type: "branch", ref_name: $ref, selector: {type: "custom", pattern: $pat}}, variables: $vars}')
        else
            payload=$(jq -n \
                --arg ref "$branch" \
                --argjson vars "$variables" \
                '{target: {type: "pipeline_ref_target", ref_type: "branch", ref_name: $ref}, variables: $vars}')
        fi
    else
        # No variables → omit the key entirely (matches Python's
        # omit-when-empty contract).
        if [[ -n "$pattern" ]]; then
            payload=$(jq -n \
                --arg ref "$branch" \
                --arg pat "$pattern" \
                '{target: {type: "pipeline_ref_target", ref_type: "branch", ref_name: $ref, selector: {type: "custom", pattern: $pat}}}')
        else
            payload=$(jq -n --arg ref "$branch" '{target: {type: "pipeline_ref_target", ref_type: "branch", ref_name: $ref}}')
        fi
    fi

    echo "Triggering pipeline on ${BB_WORKSPACE}/${repo} branch ${branch}..."
    if [[ -n "$pattern" ]]; then
        echo "  Custom pipeline: ${pattern}"
    fi
    if [[ "$variables" != "[]" ]]; then
        # Echo variable KEYS only — values may be secrets (API tokens,
        # deploy creds) that the user passed as --var KEY=value. Mask
        # per ELEMENT, not per line: a line-oriented sed would print any
        # text after an embedded newline in a value unmasked (KEY=$'a\nb'
        # masks the KEY=a line but leaks the b line).
        local masked="" _mp
        for _mp in "${var_pairs[@]}"; do
            masked+="${_mp%%=*}=*** "
        done
        echo "  Variables: ${masked%% }"
    fi

    # rc-capture pattern: capture the exit code so a 4xx (protected
    # branch, custom pipeline name not found, invalid variable shape)
    # surfaces as a labelled error instead of `set -e` silently
    # aborting after the "Triggering pipeline..." banner.
    # Capture rc via `|| rc=$?`, not `if ! cmd; then rc=$?` — the
    # latter sets $? to the negation (always 0), so the real exit code
    # was being lost and `exit $rc` exited 0 on failure. Verified on
    # bash 3.2 and 5.x.
    local response rc=0
    response=$(bb_post "$(repo_path "$repo")/pipelines/" "$payload") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi

    local build_num
    build_num=$(echo "$response" | jq -r '.build_number')
    echo "Started pipeline #${build_num}"
    echo ""
    echo "Watch with: bb watch ${repo} ${build_num}"
}

cmd_pipeline_stop() {
    local repo
    resolve_repo "${1:-}"
    local build_number="${2:-}"

    if [[ -z "$build_number" ]]; then
        echo "Usage: bb pipeline-stop [repo] <build-number>" >&2
        exit 1
    fi
    _require_build_number "$build_number"

    # Parity fix: previously scanned only 50 most-recent pipelines, so
    # older builds were unfindable. Bumped to 100 (Bitbucket's max
    # pagelen) which covers ~2x more in a single page without
    # implementing full pagination in bash. The Python side paginates
    # up to 2000 — this is a partial parity step.
    local response
    response=$(bb_get "$(repo_path "$repo")/pipelines/?sort=-created_on&pagelen=100")

    local pipeline_uuid
    pipeline_uuid=$(echo "$response" | jq -r ".values[] | select(.build_number == ${build_number}) | .uuid" | tr -d '{}')

    if [[ -z "$pipeline_uuid" ]]; then
        echo "Pipeline #${build_number} not found in the 100 most-recent pipelines." >&2
        exit 1
    fi

    # Parity fix: previously discarded the API response with > /dev/null.
    # Now capture exit code so a stop failure (already-stopped pipeline,
    # 4xx, etc.) surfaces to the user instead of being masked.
    if bb_post "$(repo_path "$repo")/pipelines/%7B${pipeline_uuid}%7D/stopPipeline" > /dev/null; then
        echo "Stopped pipeline #${build_number}"
    else
        local rc=$?
        echo "Stop request failed for pipeline #${build_number} (exit $rc)." >&2
        exit $rc
    fi
}

cmd_pipeline_approve() {
    local repo
    resolve_repo "${1:-}"
    local build_number="${2:-}"

    if [[ -z "$build_number" ]]; then
        echo "Usage: bb approve [repo] <build-number>" >&2
        exit 1
    fi

    echo "Manual step approval is not supported by the Bitbucket public API."
    echo "Opening pipeline in browser..."
    echo ""

    local url="https://bitbucket.org/${BB_WORKSPACE}/${repo}/addon/pipelines/home#!/results/${build_number}"
    echo "  ${url}"
    open "$url" 2>/dev/null || xdg-open "$url" 2>/dev/null || true
}

# Pipelines must be ENABLED on a repo before pipeline variables, custom
# pipelines, or builds work. Toggle via the pipelines_config resource:
#   GET /repositories/{ws}/{slug}/pipelines_config  → {"enabled": bool}
#   PUT  ...  {"enabled": true|false}
# Verified live: the GET 404s when Pipelines has never been configured
# (the pre-enable state), so cmd_pipelines_status reports "disabled (never
# configured)" on a 404 rather than erroring.

cmd_pipelines_status() {
    local repo
    resolve_repo "${1:-}"

    # The status command must distinguish a 404 ("never configured" — the
    # normal pre-enable state, reported as disabled) from a 403 or any
    # other error, which must be surfaced. The Python side
    # (bb_ops.pipelines_config_show) translates ONLY a 404 and re-raises
    # everything else. `bb_get` applies the default policy (any non-2xx is
    # an error), so this reads the status directly via `_bb_http` and
    # decides for itself. One request (no re-probe), so there's no window
    # where the state changes between two calls.
    local path raw code body rc=0
    path="$(repo_path "$repo")/pipelines_config"
    # `|| rc=$?` so a transport-level curl failure (DNS, TLS, connection
    # refused — NOT an HTTP status) exits with curl's code; _bb_http has
    # already explained it.
    raw=$(_bb_http GET "$path" "") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi
    code="${raw##*$'\n'}"     # last line: the HTTP status code
    body="${raw%$'\n'*}"      # everything before it: the response body

    case "$code" in
        2*)
            local enabled
            enabled=$(echo "$body" | jq -r '.enabled // false')
            echo "Pipelines: $([[ "$enabled" == "true" ]] && echo enabled || echo disabled) for ${BB_WORKSPACE}/${repo}"
            ;;
        404)
            echo "Pipelines: disabled (never configured) for ${BB_WORKSPACE}/${repo}"
            ;;
        *)
            _print_api_error "$code" "$body" "GET" "$path"
            exit 22
            ;;
    esac
}

# Shared enable/disable body. Args: <repo-arg> <true|false>.
_pipelines_set_enabled() {
    local repo
    resolve_repo "${1:-}"
    local enabled="$2"

    local payload
    payload=$(jq -n --argjson e "$enabled" '{enabled: $e}')

    local response rc=0
    response=$(bb_put "$(repo_path "$repo")/pipelines_config" "$payload") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi
    local now
    now=$(echo "$response" | jq -r '.enabled // false')
    echo "Pipelines $([[ "$now" == "true" ]] && echo enabled || echo disabled) for ${BB_WORKSPACE}/${repo}"
}

cmd_pipelines_enable() {
    _pipelines_set_enabled "${1:-}" true
}

cmd_pipelines_disable() {
    _pipelines_set_enabled "${1:-}" false
}

# =========================================================================
#  PULL REQUEST COMMANDS
# =========================================================================

cmd_pr_list() {
    local repo state
    # State-recognition: `bb prs MERGED` from inside a checkout means
    # "MERGED PRs in this repo", not "a repo named MERGED". If the first
    # arg is a bare state name (matched case-INSENSITIVELY, so `merged`
    # works too), treat it as the state and auto-detect the repo.
    # Otherwise the first arg is the repo ([repo] [state] positional
    # form, unchanged).
    #
    # Tradeoff: because the match is case-insensitive, a repo literally
    # named "open"/"merged"/"declined"/"superseded" (any case) would be
    # read as a state and shadowed — you'd reach it via the explicit
    # `bb prs <workspace>/open` form. Accepted: such a repo name is
    # vanishingly unlikely, and the explicit form is always available
    # as an escape hatch.
    local _arg1_upper
    _arg1_upper="$(printf '%s' "${1:-}" | tr '[:lower:]' '[:upper:]')"
    case "$_arg1_upper" in
        OPEN|MERGED|DECLINED|SUPERSEDED)
            resolve_repo ""
            state="$_arg1_upper"
            ;;
        *)
            resolve_repo "${1:-}"
            state="$(printf '%s' "${2:-OPEN}" | tr '[:lower:]' '[:upper:]')"
            ;;
    esac
    _require_pr_state "$state"

    echo "Pull requests for ${BB_WORKSPACE}/${repo} [${state}]:"
    echo ""

    local response
    response=$(bb_get "$(repo_path "$repo")/pullrequests?state=${state}&pagelen=25")

    local count
    count=$(echo "$response" | jq '.size')

    if [[ "$count" == "0" ]]; then
        # Use `tr` instead of `${state,,}` for bash 3.x compatibility.
        # macOS ships bash 3.2 at /bin/bash and `#!/usr/bin/env bash`
        # finds it before Homebrew bash on the default PATH, so the
        # lowercase-substitution syntax would fail this branch only
        # (when count==0) — and only on macOS where it bites hardest.
        # Use printf '%s' rather than echo to neutralize leading-dash
        # values (echo would interpret `-n` / `-e` / `-E` as flags); not
        # a real risk for Bitbucket states (OPEN/MERGED/DECLINED/...)
        # but bulletproof and only marginally longer.
        echo "  No $(printf '%s' "$state" | tr '[:upper:]' '[:lower:]') pull requests."
        return
    fi

    printf "  %-6s %-6s %-30s %-20s %s\n" "PR" "STATE" "TITLE" "BRANCH" "AUTHOR"
    printf "  %-6s %-6s %-30s %-20s %s\n" "--" "-----" "-----" "------" "------"

    echo "$response" | jq -r '
        .values[] |
        [
            .id,
            .state,
            (.title | if length > 28 then .[:28] + ".." else . end),
            (.source.branch.name | if length > 18 then .[:18] + ".." else . end),
            .author.display_name
        ] | @tsv
    ' | while IFS=$'\t' read -r id state title branch author; do
        local display_state
        display_state=$(format_state "$state")
        printf "  #%-5s %-6s %-30s %-20s %s\n" "$id" "$display_state" "$title" "$branch" "$author"
    done
}

cmd_pr_view() {
    local repo pr_id pr_args_consumed
    _resolve_pr_args "$@"

    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-view [repo] <pr-id>   (or: bb pr <id> from inside a checkout)" >&2
        exit 1
    fi

    local response
    response=$(bb_get "$(repo_path "$repo")/pullrequests/${pr_id}")

    echo "$response" | jq -r '
        "PR #" + (.id | tostring) + " - " + .title,
        "",
        "  State:       " + .state,
        "  Author:      " + .author.display_name,
        "  Source:      " + .source.branch.name,
        "  Destination: " + .destination.branch.name,
        "  Created:     " + .created_on,
        "  Updated:     " + .updated_on,
        "  Link:        " + .links.html.href,
        "",
        "  Description:",
        "  " + (.description // "(none)")
    '

    echo ""
    echo "  Reviewers:"
    # UUID alongside the name: display names are not unique in a workspace
    # (two accounts can share name AND nickname), and the uuid is what
    # --reviewer / --remove-reviewer take, so printing it here makes the
    # list directly actionable instead of requiring a second `bb members`.
    echo "$response" | jq -r '
        if ((.reviewers // []) | length) == 0 then "    (none)"
        else .reviewers[] | "    - " + (.display_name // "?") + "  " + (.uuid // "?")
        end
    '

    # Show approval status
    local activity
    activity=$(bb_get "$(repo_path "$repo")/pullrequests/${pr_id}/activity?pagelen=50" 2>/dev/null || echo '{"values":[]}')

    echo ""
    echo "  Approvals:"
    local approvals
    approvals=$(echo "$activity" | jq -r '
        [.values[] | select(.approval) | .approval.user.display_name] | unique | .[]
    ' 2>/dev/null || true)

    if [[ -z "$approvals" ]]; then
        echo "    (none)"
    else
        echo "$approvals" | while read -r name; do
            echo "    + ${name}"
        done
    fi
}

cmd_pr_create() {
    # bb pr-create [--repo REPO] <title> [dest-branch] [--close-source-branch]
    #              [--description TEXT | --description-file PATH]
    #              [--reviewer UUID ...]
    #
    # <title> is the FIRST positional and dest the SECOND, so the common
    # `bb pr-create "<title>" <dest>` (repo auto-detected from git origin)
    # parses correctly. A repo is selected with --repo (slug or ws/slug);
    # a leading "workspace/slug" positional is still accepted for
    # back-compat. There is deliberately NO bare-slug leading positional:
    # it made a two-positional `pr-create "<title>" <dest>` mis-read the
    # title as the repo. The Python/MCP pr_create takes named args, so this
    # positional ambiguity is bash-only (no parity change on that side).
    #
    # close_source_branch defaults to false: deleting the source branch
    # on merge is destructive, so it is opt-in (--close-source-branch),
    # never automatic. Same stance `gh pr merge` takes with
    # --delete-branch. Parity with bb_ops.pr_create.
    #
    # --reviewer takes a Bitbucket account UUID and is repeatable. UUIDs
    # are the only reviewer identifier the PR API accepts; `bb members`
    # lists them. Parity with bb_ops.pr_create(reviewers=[...]).
    local close_source_branch="false"
    local repo_flag="" have_repo_flag=""
    local description="" desc_file=""
    local have_description="" have_desc_file=""
    local -a positionals=()
    local -a reviewers=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --close-source-branch) close_source_branch="true"; shift ;;
            --repo)               _require_flag_value "$@"; repo_flag="$2"; have_repo_flag=1; shift 2 ;;
            --repo=*)             repo_flag="${1#*=}"; have_repo_flag=1; shift ;;
            --description)        _require_flag_value "$@"; description="$2"; have_description=1; shift 2 ;;
            --description=*)      description="${1#*=}"; have_description=1; shift ;;
            --description-file)   _require_flag_value "$@"; desc_file="$2"; have_desc_file=1; shift 2 ;;
            --description-file=*) desc_file="${1#*=}"; have_desc_file=1; shift ;;
            --reviewer)           _require_flag_value "$@"; _require_reviewer_uuid "$2"; reviewers+=("$REVIEWER_UUID"); shift 2 ;;
            --reviewer=*)         _require_reviewer_uuid "${1#*=}"; reviewers+=("$REVIEWER_UUID"); shift ;;
            -*)
                echo "Error: unknown flag for pr-create: $1" >&2
                exit 1
                ;;
            *) positionals+=("$1"); shift ;;
        esac
    done

    # --description and --description-file set the same field; supplying
    # both is ambiguous. Reject rather than silently pick (parity pr-update).
    if [[ -n "$have_description" && -n "$have_desc_file" ]]; then
        echo "Error: --description and --description-file are mutually exclusive." >&2
        exit 1
    fi

    # Resolve repo + title + dest.
    #   --repo REPO       → all positionals are <title> [dest]
    #   leading ws/slug   → back-compat repo positional, then <title> [dest]
    #   otherwise         → <title> [dest], repo auto-detected from git origin
    # The ws/slug back-compat form requires EXACTLY one slash and no
    # whitespace, so a multi-word title (which contains a space) is never
    # mistaken for a repo. Residual caveat: a single-token title that is
    # itself shaped like ws/slug (one slash, no spaces, e.g. "fix/typo") is
    # still consumed as the repo. That case is rare, and --repo is the
    # escape hatch — with --repo set, every positional is a title/dest, so
    # `bb pr-create --repo <ws/slug> "fix/typo" <dest>` titles it correctly.
    # The `+` expansion keeps the array read safe under `set -u` on bash 3.2
    # when no positionals were given.
    local repo title dest
    local -a pos=( "${positionals[@]+"${positionals[@]}"}" )
    local first="${pos[0]:-}"
    if [[ -n "$have_repo_flag" ]]; then
        resolve_repo "$repo_flag"
        title="${pos[0]:-}"
        dest="${pos[1]:-main}"
    elif [[ "$first" =~ ^[^[:space:]/]+/[^[:space:]/]+$ ]]; then
        resolve_repo "$first"
        title="${pos[1]:-}"
        dest="${pos[2]:-main}"
    else
        resolve_repo ""
        title="${pos[0]:-}"
        dest="${pos[1]:-main}"
    fi

    local source_branch
    source_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)

    if [[ -z "$source_branch" || "$source_branch" == "HEAD" ]]; then
        echo "Error: Not on a branch. Check out a branch first." >&2
        exit 1
    fi

    if [[ -z "$title" ]]; then
        echo "Usage: bb pr-create [--repo REPO] <title> [dest-branch] [--close-source-branch]" >&2
        echo "                    [--description TEXT | --description-file PATH]" >&2
        echo "                    [--reviewer UUID ...]" >&2
        echo "" >&2
        echo "  Creates a PR from current branch (${source_branch}) to dest (default: main)." >&2
        echo "  <title> is the first positional; a repo is given with --repo" >&2
        echo "  (slug or workspace/slug) or auto-detected from the git origin." >&2
        echo "  The source branch is kept on merge by default; pass" >&2
        echo "  --close-source-branch to have it deleted when the PR merges." >&2
        echo "  Description: --description TEXT, --description-file PATH, or a" >&2
        echo "  file redirect (bb pr-create \"title\" dest < body.md)." >&2
        echo "  Reviewers: --reviewer UUID, repeatable. Run 'bb members' for UUIDs." >&2
        exit 1
    fi

    # Resolve the PR description. Precedence: --description, then
    # --description-file, then a `< body.md` regular-file redirect on stdin.
    # The implicit stdin path reads ONLY a regular file (-f): a pipe, char
    # device, or terminal is never auto-read. Reading a non-closing stdin
    # (no controlling tty, no piped data, no EOF) is exactly the `cat` that
    # blocked forever and orphaned pr-create processes; a regular file
    # always reaches EOF, so restricting the implicit read to regular files
    # makes that hang impossible. Pipe / interactive callers pass an
    # explicit --description or --description-file.
    #
    # The regular-file test probes BOTH /dev/stdin and /dev/fd/0: Linux
    # resolves /dev/stdin to /proc/self/fd/0, but on platforms where that
    # path is absent or its stat() doesn't reflect the redirect, /dev/fd/0
    # is the fallback. A false negative on both only SKIPS the convenience
    # read (callers fall back to --description-file); it can never turn a
    # pipe/tty into a regular file, so the no-hang guarantee holds either
    # way. --description-file is the fully portable path (README).
    if [[ -n "$have_desc_file" ]]; then
        if [[ ! -f "$desc_file" ]]; then
            echo "Error: --description-file not found: $desc_file" >&2
            exit 1
        fi
        local _dfrc=0
        description="$(cat "$desc_file")" || _dfrc=$?
        if [[ "$_dfrc" -ne 0 ]]; then
            echo "Error: failed to read --description-file: $desc_file" >&2
            exit 1
        fi
    elif [[ -z "$have_description" ]] && { [[ -f /dev/stdin ]] || [[ -f /dev/fd/0 ]]; }; then
        description="$(cat)"
    fi

    # Build the base payload, then add the optional fields. Adding them
    # incrementally keeps ONE definition of the required shape: the
    # earlier form duplicated the whole jq program per description branch,
    # and a second optional field (reviewers) would have squared that into
    # four copies of the same object.
    #
    # Parity fix: `description` is omitted when empty so it matches the
    # Python omit-when-empty contract (bb_ops.pr_create).
    # close_source_branch is a JSON bool via --argjson, not a string.
    local payload
    payload=$(jq -n \
        --arg title "$title" \
        --arg src "$source_branch" \
        --arg dst "$dest" \
        --argjson close "$close_source_branch" \
        '{
            title: $title,
            source: {branch: {name: $src}},
            destination: {branch: {name: $dst}},
            close_source_branch: $close
        }')
    if [[ -n "$description" ]]; then
        payload=$(echo "$payload" | jq --arg desc "$description" '. + {description: $desc}')
    fi
    # Each UUID rides in as its OWN jq argument ($ARGS.positional, jq >=
    # 1.6) rather than inside a delimited string — same discipline as the
    # trigger command's variable pairs, for the same reason. The key is
    # omitted entirely when no --reviewer was passed, matching
    # bb_ops.pr_create, which only sets it for a non-empty list.
    if [[ "${#reviewers[@]}" -gt 0 ]]; then
        payload=$(echo "$payload" | jq \
            '. + {reviewers: [$ARGS.positional[] | {uuid: .}]}' \
            --args "${reviewers[@]}")
    fi

    echo "Creating PR: ${source_branch} -> ${dest}"
    echo "  Title: ${title}"
    if [[ "${#reviewers[@]}" -gt 0 ]]; then
        echo "  Reviewers requested: ${#reviewers[@]}"
    fi
    if [[ "$close_source_branch" == "true" ]]; then
        echo "  Source '${source_branch}' will be deleted when the PR merges."
    fi

    # rc-capture pattern: a 400 (typo in dest branch, duplicate PR
    # already open, source branch not pushed, etc.) makes bb_post
    # exit non-zero and `set -e` would silently abort after the
    # "Creating PR:" banner. Without a labelled error, a user
    # retrying assuming a network blip might create a duplicate.
    # Capture rc via `|| rc=$?`, not `if ! cmd; then rc=$?` — the
    # latter sets $? to the negation (always 0), so the real exit code
    # was being lost and `exit $rc` exited 0 on failure. Verified on
    # bash 3.2 and 5.x.
    local response rc=0
    response=$(bb_post "$(repo_path "$repo")/pullrequests" "$payload") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi

    local pr_id pr_url
    pr_id=$(echo "$response" | jq -r '.id')
    pr_url=$(echo "$response" | jq -r '.links.html.href')

    echo "Created PR #${pr_id}: ${title}"
    echo "  ${pr_url}"
}

cmd_pr_update() {
    # bb pr-update [repo] <pr-id> [--title TEXT] [--description TEXT | --description-file PATH]
    #              [--reviewer UUID ...] [--remove-reviewer UUID ...] [--drop-approvals]
    #
    # Updates an OPEN pull request via PUT to
    # /repositories/{ws}/{slug}/pullrequests/{id} — the same path `bb pr`
    # GETs. Only the fields supplied go in the body; the PUT merges them
    # into the existing PR, preserving source/destination branches
    # (Bitbucket keeps omitted PR fields on this endpoint). The method is
    # PUT, not PATCH: Bitbucket Cloud has no PATCH for the pullrequests
    # resource. Parity with bb_ops.pr_update.
    #
    # Reviewers are the exception to that merge: the PUT REPLACES the whole
    # reviewers array, so --reviewer / --remove-reviewer read the PR first
    # and send the full resulting list. Sending only the person being added
    # would silently unassign everyone else.
    #
    # There is deliberately no "set the reviewers to exactly this" flag:
    # replace is the operation that silently discards other people's
    # approvals, and add/remove composes to the same result with each
    # change stated explicitly.
    #
    # [repo] accepts the same shapes as every other PR command (bare slug,
    # ws/slug, or omitted for git-origin auto-detect). At least one of
    # --title / --description / --description-file / --reviewer /
    # --remove-reviewer must be supplied.
    local title="" description="" desc_file=""
    local have_title="" have_description="" have_desc_file=""
    local drop_approvals=""
    local -a positionals=()
    local -a add_reviewers=()
    local -a remove_reviewers=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --title)              _require_flag_value "$@"; title="$2"; have_title=1; shift 2 ;;
            --title=*)            title="${1#*=}"; have_title=1; shift ;;
            --description)        _require_flag_value "$@"; description="$2"; have_description=1; shift 2 ;;
            --description=*)      description="${1#*=}"; have_description=1; shift ;;
            --description-file)   _require_flag_value "$@"; desc_file="$2"; have_desc_file=1; shift 2 ;;
            --description-file=*) desc_file="${1#*=}"; have_desc_file=1; shift ;;
            --reviewer)           _require_flag_value "$@"; _require_reviewer_uuid "$2"; add_reviewers+=("$REVIEWER_UUID"); shift 2 ;;
            --reviewer=*)         _require_reviewer_uuid "${1#*=}"; add_reviewers+=("$REVIEWER_UUID"); shift ;;
            --remove-reviewer)    _require_flag_value "$@"; _require_reviewer_uuid "$2"; remove_reviewers+=("$REVIEWER_UUID"); shift 2 ;;
            --remove-reviewer=*)  _require_reviewer_uuid "${1#*=}"; remove_reviewers+=("$REVIEWER_UUID"); shift ;;
            --drop-approvals)     drop_approvals=1; shift ;;
            -*)
                echo "Error: unknown flag for pr-update: $1" >&2
                exit 1 ;;
            *)
                positionals+=("$1"); shift ;;
        esac
    done

    # --description and --description-file are two ways to set the same
    # field; supplying both is ambiguous. Reject rather than silently pick.
    if [[ -n "$have_description" && -n "$have_desc_file" ]]; then
        echo "Error: --description and --description-file are mutually exclusive." >&2
        exit 1
    fi

    # Resolve the body from a file when requested. Read it verbatim (no
    # trailing-newline trimming via "$(...)" is acceptable: a PR body's
    # trailing blank line is not meaningful, and command substitution's
    # single-trailing-newline strip matches how the body renders). A
    # missing/unreadable file fails HERE, before any API call.
    if [[ -n "$have_desc_file" ]]; then
        if [[ ! -f "$desc_file" ]]; then
            echo "Error: --description-file not found: $desc_file" >&2
            exit 1
        fi
        local _dfrc=0
        description="$(cat "$desc_file")" || _dfrc=$?
        if [[ "$_dfrc" -ne 0 ]]; then
            echo "Error: failed to read --description-file: $desc_file" >&2
            exit 1
        fi
        have_description=1
    fi

    # At least one field must change; a PUT with an empty body is a no-op
    # round-trip. Gate on the have_* flags (not `-n "$value"`) so an
    # intentional clear (`--description ""`) still counts as a change.
    # Reject BEFORE resolving the repo so the usage error needs no API call.
    if [[ -z "$have_title" && -z "$have_description" \
          && "${#add_reviewers[@]}" -eq 0 && "${#remove_reviewers[@]}" -eq 0 ]]; then
        echo "Usage: bb pr-update [repo] <pr-id> [--title TEXT] [--description TEXT | --description-file PATH]" >&2
        echo "                    [--reviewer UUID ...] [--remove-reviewer UUID ...] [--drop-approvals]" >&2
        echo "" >&2
        echo "  Updates an OPEN pull request's title, description and/or reviewers." >&2
        echo "  At least one of --title / --description / --description-file /" >&2
        echo "  --reviewer / --remove-reviewer is required." >&2
        echo "  Reviewer UUIDs come from 'bb members'." >&2
        echo "  [repo] is auto-detected from git origin if omitted." >&2
        exit 1
    fi

    # When a title is supplied, it must be non-empty/non-whitespace — a PR
    # needs a title and Bitbucket rejects a blank one (parity with the
    # bb_ops.pr_update boundary check). Only checked when --title was given;
    # omitting --title leaves the existing title untouched.
    if [[ -n "$have_title" ]]; then
        local _t_stripped
        _t_stripped="$(printf '%s' "$title" | tr -d '[:space:]')"
        if [[ -z "$_t_stripped" ]]; then
            echo "Error: --title requires a non-empty, non-whitespace value." >&2
            exit 1
        fi
    fi

    # Resolve [repo] <pr-id> from the collected positionals via the same
    # heuristic every other PR command uses. The `+` expansion keeps this
    # safe under `set -u` on bash 3.2 when no positionals were given.
    local repo pr_id pr_args_consumed
    _resolve_pr_args "${positionals[@]+"${positionals[@]}"}"

    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-update [repo] <pr-id> [--title TEXT] [--description TEXT | --description-file PATH]" >&2
        exit 1
    fi

    # Build the body with jq so values are escaped. Only the supplied
    # fields go in — parity with the Python omit-when-absent contract.
    local payload="{}"
    if [[ -n "$have_title" ]]; then
        payload=$(echo "$payload" | jq --arg t "$title" '. + {title: $t}')
    fi
    if [[ -n "$have_description" ]]; then
        payload=$(echo "$payload" | jq --arg d "$description" '. + {description: $d}')
    fi

    # Reviewer changes need the CURRENT list: the PUT replaces the whole
    # array, so the request has to carry every reviewer who should remain.
    # This read-modify-write has a race (a reviewer added by someone else
    # between the GET and the PUT is lost). Bitbucket exposes no ETag or
    # if-match on this endpoint, so the window cannot be closed here; it is
    # narrow and the operation is trivially repeatable. Parity with
    # bb_ops.pr_update, which reads the same way.
    if [[ "${#add_reviewers[@]}" -gt 0 || "${#remove_reviewers[@]}" -gt 0 ]]; then
        local current rc_get=0
        current=$(bb_get "$(repo_path "$repo")/pullrequests/${pr_id}") || rc_get=$?
        if [[ "$rc_get" -ne 0 ]]; then
            echo "  Could not read PR #${pr_id}'s current reviewers; nothing was changed." >&2
            exit "$rc_get"
        fi

        # An approval is recorded on `participants`, not on `reviewers`, so
        # the guard reads the participant record for each person being
        # removed. Removing an approver discards the approval and re-adding
        # them does not restore it, so it is refused without an explicit
        # opt-in (the repo-wide rule: a destructive action is never a
        # default).
        if [[ "${#remove_reviewers[@]}" -gt 0 && -z "$drop_approvals" ]]; then
            local approved_hits
            approved_hits=$(echo "$current" | jq -r --args '
                [.participants[]? | select(.approved) | .user.uuid] as $approved
                | [$ARGS.positional[] | select(. as $u | $approved | index($u))]
                | join(", ")
            ' -- "${remove_reviewers[@]}")
            if [[ -n "$approved_hits" ]]; then
                echo "Error: refusing to remove reviewer(s) who have already approved:" >&2
                echo "  ${approved_hits}" >&2
                echo "  Removing them discards the approval, and re-adding them does" >&2
                echo "  not restore it. Pass --drop-approvals to do it anyway." >&2
                exit 1
            fi
        fi

        # Survivors keep their existing order and additions append, so a
        # repeated call produces a stable list. Adding someone already on
        # the PR is a no-op rather than a duplicate.
        local merged
        merged=$(echo "$current" | jq -c --args \
            --argjson nremove "${#remove_reviewers[@]}" '
            [.reviewers[]?.uuid] as $current
            | ($ARGS.positional[:$nremove]) as $remove
            | ($ARGS.positional[$nremove:]) as $add
            | [$current[] | select(. as $u | ($remove | index($u)) | not)] as $kept
            | $kept + [$add[] | select(. as $u | ($kept | index($u)) | not)]
            | map({uuid: .})
        ' -- "${remove_reviewers[@]+"${remove_reviewers[@]}"}" "${add_reviewers[@]+"${add_reviewers[@]}"}")
        payload=$(echo "$payload" | jq --argjson r "$merged" '. + {reviewers: $r}')
    fi

    echo "Updating PR #${pr_id}..."

    # rc-capture pattern (same as cmd_pr_create): a 4xx (wrong id, PR not
    # open, empty title, missing scope) makes bb_put exit non-zero and
    # `set -e` would silently abort after the banner without this guard.
    # bb_put has already printed the API's reason.
    local response rc=0
    response=$(bb_put "$(repo_path "$repo")/pullrequests/${pr_id}" "$payload") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi

    local new_title pr_url
    new_title=$(echo "$response" | jq -r '.title // "(unknown)"')
    pr_url=$(echo "$response" | jq -r '.links.html.href // empty')

    echo "Updated PR #${pr_id}: ${new_title}"
    # Echo the resulting reviewer list back. The whole point of the flags is
    # who is on the PR now, and the response already carries it — reading it
    # back is what turns "the call succeeded" into "the right people are
    # assigned". Names can collide in a workspace, so print the uuid too.
    if [[ "${#add_reviewers[@]}" -gt 0 || "${#remove_reviewers[@]}" -gt 0 ]]; then
        echo "  Reviewers now:"
        echo "$response" | jq -r '
            if ((.reviewers // []) | length) == 0 then "    (none)"
            else .reviewers[] | "    - " + (.display_name // "?") + "  " + (.uuid // "?")
            end
        '
    fi
    [[ -n "$pr_url" ]] && echo "  ${pr_url}"
}

cmd_pr_approve() {
    local repo pr_id pr_args_consumed
    _resolve_pr_args "$@"

    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-approve [repo] <pr-id>" >&2
        exit 1
    fi

    # Parity fix: capture exit code so an approve failure (already
    # approved, 4xx, etc.) surfaces to the user instead of being
    # masked by the unconditional success print.
    if bb_post "$(repo_path "$repo")/pullrequests/${pr_id}/approve" > /dev/null; then
        echo "Approved PR #${pr_id}"
    else
        local rc=$?
        echo "Approve request failed for PR #${pr_id} (exit $rc)." >&2
        exit $rc
    fi
}

cmd_pr_unapprove() {
    local repo pr_id pr_args_consumed
    _resolve_pr_args "$@"

    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-unapprove [repo] <pr-id>" >&2
        exit 1
    fi

    # Bitbucket's contract: DELETE the same /approve subpath that
    # POST uses for approval. Parity with bb_ops.pr_unapprove.
    if bb_delete "$(repo_path "$repo")/pullrequests/${pr_id}/approve" > /dev/null; then
        echo "Removed approval on PR #${pr_id}"
    else
        local rc=$?
        echo "Unapprove request failed for PR #${pr_id} (exit $rc)." >&2
        exit $rc
    fi
}

cmd_pr_merge() {
    # bb pr-merge [repo] <pr-id> [strategy] [--close-source-branch]
    #
    # close_source_branch defaults to false and is always sent explicitly
    # in the merge payload. Deleting the source branch on merge is
    # destructive, so it is opt-in (--close-source-branch), never
    # automatic. Sending false explicitly also OVERRIDES whatever value
    # the PR was created with — the merge API's value wins over the PR's —
    # so a PR made by an older bb, or the Bitbucket UI, with the box
    # checked still keeps its source branch here unless --close-source-branch
    # is passed. Parity with bb_ops.pr_merge.
    local close_source_branch="false"
    local -a positionals=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --close-source-branch) close_source_branch="true"; shift ;;
            -*)
                echo "Error: unknown flag for pr-merge: $1" >&2
                exit 1
                ;;
            *) positionals+=("$1"); shift ;;
        esac
    done

    local repo pr_id pr_args_consumed
    # Pass the two leading positional slots explicitly — `:-` indexing
    # sidesteps the empty-array-under-set-u expansion trap on bash 3.2.
    _resolve_pr_args "${positionals[0]:-}" "${positionals[1]:-}"
    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-merge [repo] <pr-id> [strategy] [--close-source-branch]" >&2
        echo "       (or: bb pr-merge <id> [strategy] from inside a checkout)" >&2
        echo "  Strategies: merge_commit (default), squash, fast_forward" >&2
        echo "  The source branch is kept on merge by default; pass" >&2
        echo "  --close-source-branch to have it deleted." >&2
        exit 1
    fi
    # Extras start after the consumed [repo]/<id> slots; index with a
    # default instead of `shift` so a missing strategy stays optional.
    local strategy="${positionals[$pr_args_consumed]:-merge_commit}"

    # Validate strategy against Bitbucket's accepted set so a typo
    # (e.g. "squash_commit") fails locally with a clear message
    # instead of getting an opaque 400 from the API.
    case "$strategy" in
        merge_commit|squash|fast_forward) ;;
        *)
            echo "Error: invalid strategy '${strategy}'." >&2
            echo "  Valid: merge_commit, squash, fast_forward." >&2
            exit 1
            ;;
    esac

    local payload
    payload=$(jq -n --arg strategy "$strategy" --argjson close "$close_source_branch" \
        '{type: "pullrequest", merge_strategy: $strategy, close_source_branch: $close}')

    # Parity fix: capture exit code so a merge conflict / failing
    # required check / wrong-strategy-for-repo error surfaces as a
    # labelled error instead of silently aborting under set -e.
    # Bitbucket's PR merge endpoint is POST per the REST docs. An earlier
    # version of this used bb_put (PUT), which Bitbucket rejects with
    # HTTP 403 + "This endpoint does not support token-based authentication"
    # (an unhelpful error that actually meant "wrong method here"). Confirmed
    # against dreamfacesbir/ryan-os PR#2 (2026-06-28) where direct POST with
    # the same API token merged cleanly. Keep POST; do not "improve" back.
    if bb_post "$(repo_path "$repo")/pullrequests/${pr_id}/merge" "$payload" > /dev/null; then
        echo "Merged PR #${pr_id} (${strategy})"
    else
        local rc=$?
        exit $rc
    fi
}

cmd_pr_decline() {
    local repo pr_id pr_args_consumed
    _resolve_pr_args "$@"

    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-decline [repo] <pr-id>" >&2
        exit 1
    fi

    # Parity fix: capture exit code (was previously discarded).
    if bb_post "$(repo_path "$repo")/pullrequests/${pr_id}/decline" > /dev/null; then
        echo "Declined PR #${pr_id}"
    else
        local rc=$?
        echo "Decline request failed for PR #${pr_id} (exit $rc)." >&2
        exit $rc
    fi
}

cmd_pr_diff() {
    local repo pr_id pr_args_consumed
    _resolve_pr_args "$@"

    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-diff [repo] <pr-id>" >&2
        exit 1
    fi

    # Parity fix: -L so we follow any redirect Bitbucket introduces on
    # this endpoint (today it serves inline, but future-proofing).
    # `curl -L -u` does NOT resend credentials to a different host by
    # default, so the cross-host credential-leak concern is already
    # mitigated by curl semantics.
    bb_get "$(repo_path "$repo")/pullrequests/${pr_id}/diff" -L
}

cmd_pr_comments() {
    local repo pr_id pr_args_consumed
    _resolve_pr_args "$@"

    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-comments [repo] <pr-id>" >&2
        exit 1
    fi

    local response
    response=$(bb_get "$(repo_path "$repo")/pullrequests/${pr_id}/comments?pagelen=100")

    echo "Comments on PR #${pr_id}:"
    echo ""

    echo "$response" | jq -r '
        .values[] |
        "  " + .user.display_name + " (" + (.created_on | split("T") | .[0]) + "):",
        "  " + (.content.raw // "(empty)"),
        ""
    '
}

cmd_pr_comment_add() {
    local repo pr_id pr_args_consumed
    _resolve_pr_args "$@"
    # Same ordering rule as cmd_pr_merge: check pr_id BEFORE shift to
    # avoid `shift 2` aborting under `set -euo pipefail` when the user
    # passed only one (non-numeric) arg.
    _usage_pr_comment() {
        echo "Usage: bb pr-comment [repo] <pr-id> <body>   (or: bb pr-comment <id> <body> from inside a checkout)" >&2
        echo "" >&2
        echo "  Add a top-level comment to PR #<pr-id>." >&2
        echo "  Use single quotes around <body> if it contains spaces or shell metacharacters." >&2
        exit 1
    }
    if [[ -z "$pr_id" ]]; then
        _usage_pr_comment
    fi
    shift $pr_args_consumed
    local body="${1:-}"
    if [[ -z "$body" ]]; then
        _usage_pr_comment
    fi

    # Bitbucket's contract: POST {"content": {"raw": "<text>"}}.
    # Parity with bb_ops.pr_comment_add.
    local payload
    payload=$(jq -n --arg body "$body" '{content: {raw: $body}}')

    local response
    response=$(bb_post "$(repo_path "$repo")/pullrequests/${pr_id}/comments" "$payload")

    local comment_id
    comment_id=$(echo "$response" | jq -r '.id // empty')
    if [[ -n "$comment_id" ]]; then
        echo "Posted comment #${comment_id} on PR #${pr_id}"
    else
        echo "Comment posted (response did not include an id)." >&2
        echo "$response" >&2
        exit 1
    fi
}

# =========================================================================
#  BRANCH COMMANDS
# =========================================================================

# URL-encode a single path segment (for branch names containing `/`).
# jq's @uri filter does this correctly; using jq avoids a Python /
# Perl dependency.
_url_encode_segment() {
    printf '%s' "$1" | jq -sRr @uri
}

cmd_branches() {
    local repo
    resolve_repo "${1:-}"

    echo "Branches for ${BB_WORKSPACE}/${repo}:"
    echo ""

    local response
    response=$(bb_get "$(repo_path "$repo")/refs/branches?pagelen=50&sort=-target.date")

    printf "  %-30s %-12s %s\n" "BRANCH" "DATE" "COMMIT"
    printf "  %-30s %-12s %s\n" "------" "----" "------"

    echo "$response" | jq -r '
        .values[] |
        [
            (.name | if length > 28 then .[:28] + ".." else . end),
            (.target.date | split("T") | .[0]),
            .target.hash[:8],
            .target.message[:50]
        ] | @tsv
    ' | while IFS=$'\t' read -r name date hash msg; do
        printf "  %-30s %-12s %s  %s\n" "$name" "$date" "$hash" "$msg"
    done
}

cmd_branch_show() {
    local repo
    resolve_repo "${1:-}"
    local name="${2:-}"

    if [[ -z "$name" ]]; then
        echo "Usage: bb branch [repo] <name>" >&2
        echo "" >&2
        echo "  Show details for a single branch. Branch names with '/'" >&2
        echo "  (e.g. feat/widget) are URL-encoded automatically." >&2
        exit 1
    fi

    # URL-encode the name so feat/widget isn't interpreted as a
    # sub-resource path by Bitbucket. Mirrors bb_ops.branch_show.
    local encoded
    encoded=$(_url_encode_segment "$name")

    local response
    response=$(bb_get "$(repo_path "$repo")/refs/branches/${encoded}")

    echo "Branch ${name} on ${BB_WORKSPACE}/${repo}:"
    echo ""
    echo "$response" | jq -r '
        "  Name:    " + .name,
        "  Hash:    " + .target.hash[:12],
        "  Date:    " + .target.date,
        "  Author:  " + (.target.author.user.display_name // .target.author.raw // "unknown"),
        "  Message: " + (.target.message // "(empty)" | split("\n") | .[0])
    '
}

cmd_commits() {
    local repo
    resolve_repo "${1:-}"
    local branch="${2:-}"
    local count="${3:-10}"
    _require_count "$count"

    local path response
    if [[ -n "$branch" ]]; then
        local encoded
        encoded=$(_url_encode_segment "$branch")
        path="$(repo_path "$repo")/commits/${encoded}?pagelen=${count}"
        echo "Recent commits on ${BB_WORKSPACE}/${repo} branch ${branch}:"
    else
        path="$(repo_path "$repo")/commits?pagelen=${count}"
        echo "Recent commits on ${BB_WORKSPACE}/${repo} (all branches):"
    fi
    echo ""

    response=$(bb_get "$path")

    printf "  %-10s %-12s %-22s %s\n" "HASH" "DATE" "AUTHOR" "MESSAGE"
    printf "  %-10s %-12s %-22s %s\n" "----" "----" "------" "-------"

    echo "$response" | jq -r '
        .values[] |
        [
            .hash[:8],
            (.date | split("T") | .[0]),
            (.author.user.display_name // .author.raw // "unknown" | .[:20]),
            (.message // "(empty)" | split("\n") | .[0] | .[:60])
        ] | @tsv
    ' | while IFS=$'\t' read -r hash date author msg; do
        printf "  %-10s %-12s %-22s %s\n" "$hash" "$date" "$author" "$msg"
    done
}

# =========================================================================
#  REPOSITORY COMMANDS
# =========================================================================

cmd_workspaces() {
    # GET /2.0/user/workspaces — the CHANGE-3022 replacement for the
    # cross-workspace listing endpoints removed under CHANGE-2770
    # (effective 2026-04-14). Workspace-scoped (no BB_WORKSPACE
    # involvement), so no -w override applies here.
    #
    # Requires `read:workspace:bitbucket` scope on the API token. A token
    # granted only repository/pullrequest/pipeline scopes returns 403,
    # whose body names the required and granted scopes; bb_get prints it.
    if [[ $# -gt 0 ]]; then
        echo "Usage: bb workspaces   (takes no arguments)" >&2
        exit 1
    fi

    echo "Workspaces accessible to ${BB_USER}:"
    echo ""

    # Capture rc via `|| rc=$?` rather than `if ! cmd; then rc=$?`.
    # The `!`-negation form sets $? to the LOGICAL NEGATION of the
    # command's status (always 0 for a failing command), so the real
    # exit code is unrecoverable inside an `if !` block — verified on
    # bash 3.2 and 5.x. bb_get exits 22 on an HTTP >=400 response; other
    # codes are transport-level (DNS, connection, TLS).
    #
    # bb_get has already printed the API's own explanation (for a 403,
    # the scope it needs and the ones the token carries), so this only
    # has to preserve the exit code.
    local response rc=0
    response=$(bb_get "/user/workspaces?pagelen=100") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi

    printf "  %-30s %s\n" "SLUG" "ROLE"
    printf "  %-30s %s\n" "----" "----"

    echo "$response" | jq -r '
        .values[] |
        [
            .workspace.slug,
            (if .administrator then "admin" else "member" end)
        ] | @tsv
    ' | while IFS=$'\t' read -r slug role; do
        printf "  %-30s %s\n" "$slug" "$role"
    done

    # Parity guard with bb_ops.workspaces_list, which paginates: bash
    # fetches a single 100-item page (matching cmd_repos convention).
    # >100 workspace memberships is vanishingly rare, but if the API
    # signals more pages, say so rather than silently truncating —
    # direct the user to the paginating MCP tool.
    if [[ "$(echo "$response" | jq -r '.next // empty')" != "" ]]; then
        # All three lines to stderr so the separator stays attached to
        # the hint even when stdout is piped elsewhere.
        echo "" >&2
        echo "  (showing first 100 — you belong to more; use the MCP" >&2
        echo "   workspaces_list tool, which paginates, for the full set)" >&2
    fi
}

cmd_projects() {
    # bb projects [workspace]
    #
    # GET /2.0/workspaces/{ws}/projects — list a workspace's projects.
    # An explicit [workspace] positional argument names the workspace
    # directly (matches the Python projects_list(workspace?) surface and
    # is the natural way to list projects in a workspace you're not
    # checked out in). When omitted, resolve_workspace handles -w / git
    # origin / BB_WORKSPACE just like cmd_repos.
    #
    # Requires the `read:project:bitbucket` scope on the API token. A
    # token without it returns 403, whose body names the required and
    # granted scopes; bb_get prints it.
    # Precedence: a -w flag (BB_WORKSPACE_OVERRIDE) is the most explicit
    # signal and wins; otherwise an explicit positional [workspace] wins
    # over the git-origin / BB_WORKSPACE default. resolve_workspace owns
    # the -w / git-origin / config logic, so set BB_WORKSPACE from the
    # positional ONLY when no -w override is present, then let
    # resolve_workspace fill in any remaining case — keeping the
    # precedence logic in one place.
    local ws_arg="${1:-}"
    if [[ -n "$ws_arg" && -z "${BB_WORKSPACE_OVERRIDE:-}" ]]; then
        BB_WORKSPACE="$ws_arg"
    else
        resolve_workspace
    fi

    # Validate the resolved workspace at the boundary (empty / whitespace /
    # embedded '/' / '.' / '..') — parity with bb_ops.projects_list, which
    # rejects these before any network call.
    _require_workspace "$BB_WORKSPACE"

    echo "Projects in ${BB_WORKSPACE}:"
    echo ""

    # Capture rc via `|| rc=$?` (not `if ! cmd`) so the real exit code
    # survives — same idiom as cmd_workspaces. bb_get exits 22 on HTTP
    # >=400 having already printed the API's explanation; other codes are
    # transport-level.
    local response rc=0
    response=$(bb_get "/workspaces/${BB_WORKSPACE}/projects?pagelen=100") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi

    printf "  %-12s %s\n" "KEY" "NAME"
    printf "  %-12s %s\n" "---" "----"

    echo "$response" | jq -r '
        .values[] |
        [.key, .name] | @tsv
    ' | while IFS=$'\t' read -r key name; do
        printf "  %-12s %s\n" "$key" "$name"
    done

    # Parity guard with bb_ops.projects_list, which paginates: bash fetches
    # a single 100-item page (matching cmd_repos / cmd_workspaces). If the
    # API signals more pages, point the user at the paginating MCP tool
    # rather than silently truncating.
    if [[ "$(echo "$response" | jq -r '.next // empty')" != "" ]]; then
        echo "" >&2
        echo "  (showing first 100 — workspace has more; use the MCP" >&2
        echo "   projects_list tool, which paginates, for the full set)" >&2
    fi
}

cmd_members() {
    # bb members [workspace]
    #
    # GET /2.0/workspaces/{ws}/members — list a workspace's members.
    #
    # This is the lookup that makes `bb pr-create --reviewer` usable: the
    # PR API identifies reviewers ONLY by account UUID, so without a member
    # listing there is no supported way to discover the value to pass. The
    # UUID column is exactly what --reviewer takes, braces included.
    #
    # Workspace precedence mirrors cmd_projects: a -w flag
    # (BB_WORKSPACE_OVERRIDE) wins, then an explicit [workspace]
    # positional, then resolve_workspace's git-origin / BB_WORKSPACE
    # default.
    local ws_arg="${1:-}"
    if [[ -n "$ws_arg" && -z "${BB_WORKSPACE_OVERRIDE:-}" ]]; then
        BB_WORKSPACE="$ws_arg"
    else
        resolve_workspace
    fi

    # Validate the resolved workspace at the boundary (empty / whitespace /
    # embedded '/' / '.' / '..') — parity with bb_ops.members_list, which
    # rejects these before any network call.
    _require_workspace "$BB_WORKSPACE"

    echo "Members of ${BB_WORKSPACE}:"
    echo ""

    # `fields=+values.user.account_status` asks for one extra field on the
    # SAME request rather than a per-member lookup, so marking deactivated
    # accounts costs no extra call. The `+` must be percent-encoded or it
    # is read as a space in the query string.
    local response rc=0
    response=$(bb_get "/workspaces/${BB_WORKSPACE}/members?pagelen=100&fields=%2Bvalues.user.account_status") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi

    # UUID last and unpadded: it is the value users copy into --reviewer,
    # and a trailing column can be selected without picking up padding.
    printf "  %-28s %-20s %s\n" "DISPLAY NAME" "NICKNAME" "UUID"
    printf "  %-28s %-20s %s\n" "------------" "--------" "----"

    # A deactivated account still appears in the member list and can still
    # be sent as a reviewer, where it is dead weight on the PR. Mark it
    # inline rather than adding a column that would read "active" on every
    # row of a healthy workspace. `account_status` is absent when the API
    # does not return it, which reads as unmarked.
    echo "$response" | jq -r '
        .values[] | .user |
        [
          (.display_name // "-") + (if (.account_status // "active") != "active" then " (inactive)" else "" end),
          (.nickname // "-"),
          (.uuid // "-")
        ] | @tsv
    ' | while IFS=$'\t' read -r display nickname uuid; do
        printf "  %-28s %-20s %s\n" "$display" "$nickname" "$uuid"
    done

    # Two accounts can share BOTH display name and nickname, differing only
    # by uuid (observed in a live workspace, and neither was deactivated —
    # so "mark the inactive one" does not disambiguate this). Picking by
    # name is then a coin flip, and the wrong pick assigns review to someone
    # who will never look at it. Say so explicitly instead of leaving two
    # identical-looking rows.
    local collisions
    collisions=$(echo "$response" | jq -r '
        [.values[].user | (.display_name // "-") + "\u0000" + (.nickname // "-")]
        | group_by(.) | map(select(length > 1)) | length
    ')
    if [[ "$collisions" != "0" ]]; then
        echo "" >&2
        echo "  Note: ${collisions} display name/nickname pair(s) are shared by more than" >&2
        echo "  one account above. They differ only by UUID, so confirm which account" >&2
        echo "  you mean before using it as a reviewer." >&2
    fi

    # Same single-page convention (and same honest truncation notice) as
    # cmd_projects / cmd_repos / cmd_workspaces. A workspace with more than
    # 100 members is exactly where a silent cut would hide the person you
    # were looking for.
    if [[ "$(echo "$response" | jq -r '.next // empty')" != "" ]]; then
        echo "" >&2
        echo "  (showing first 100 — workspace has more; use the MCP" >&2
        echo "   members_list tool, which paginates, for the full set)" >&2
    fi
}

cmd_repos() {
    resolve_workspace
    echo "Repositories in ${BB_WORKSPACE}:"
    echo ""

    local response
    response=$(bb_get "/repositories/${BB_WORKSPACE}?pagelen=100&sort=-updated_on")

    printf "  %-35s %-12s %-12s %s\n" "REPO" "UPDATED" "PROJECT" "LANGUAGE"
    printf "  %-35s %-12s %-12s %s\n" "----" "-------" "-------" "--------"

    # PROJECT is the project KEY, which is what `--project` expects on
    # repo-create / repo-update. It rides along on the listing response,
    # so the column costs no extra call.
    echo "$response" | jq -r '
        .values[] |
        [
            .slug,
            (.updated_on | split("T") | .[0]),
            (.project.key // "-"),
            (.language // "-")
        ] | @tsv
    ' | while IFS=$'\t' read -r slug updated project lang; do
        printf "  %-35s %-12s %-12s %s\n" "$slug" "$updated" "$project" "$lang"
    done
}

cmd_repo() {
    local repo
    resolve_repo "${1:-}"

    local response
    response=$(bb_get "$(repo_path "$repo")")

    # `project` comes back on this same GET, so showing it costs no extra
    # call. It is displayed because `bb repo-update --project KEY` can SET
    # a repo's project: a field that can be written but not read makes a
    # wrong value invisible, and project keys are not reliably the obvious
    # abbreviation of the project name. Repos in workspaces that do not
    # use projects have no `project` at all, hence the "(none)" fallback.
    echo "$response" | jq -r '
        .full_name + " - " + (.description // "(no description)"),
        "",
        "  Project:     " + (if .project then (.project.key // "?") + " (" + (.project.name // "?") + ")" else "(none)" end),
        "  Language:    " + (.language // "n/a"),
        "  Created:     " + .created_on,
        "  Updated:     " + .updated_on,
        "  Size:        " + ((.size // 0) / 1024 / 1024 | floor | tostring) + " MB",
        "  Main branch: " + (.mainbranch.name // "n/a"),
        "  Private:     " + (.is_private | tostring),
        "  Clone SSH:   " + ([.links.clone[] | select(.name == "ssh") | .href] | first // "n/a"),
        "  URL:         " + .links.html.href
    '
}

# Guard a value-taking flag against a missing argument BEFORE consuming
# $2. Under `set -u`, a bare `value="$2"` when $2 is unset (the flag was
# the last token) aborts with a raw "$2: unbound variable" bash error
# instead of a curated message. Call as `_require_flag_value "$@"` from
# inside the case arm — $1 is the flag, $2 is its value if present, so a
# count below 2 means the value is missing.
#
# Also reject the case where $2 is ANOTHER flag (starts with `--`): for
# `bb vars set KEY --value --secured`, $# is 2 so a length-only check
# would pass and then assign the literal "--secured" as the value (silent
# data corruption, the wrong string gets uploaded). A `--`-prefixed next
# token almost always means the user forgot the value, so treat it as
# missing. A value that legitimately starts with `--` can still be passed
# via the `--flag=value` form (handled by the separate `--flag=*` arms).
_require_flag_value() {
    if [[ "$#" -lt 2 ]]; then
        echo "Error: $1 requires a value." >&2
        exit 1
    fi
    if [[ "$2" == --* ]]; then
        echo "Error: $1 requires a value, but got the flag '$2'." >&2
        echo "  If the value really starts with '--', use $1=<value>." >&2
        exit 1
    fi
}

cmd_repo_create() {
    # bb repo-create <name> [--private] [--public] [--project KEY] [--description TEXT]
    #
    # Creates a new repo via POST to /repositories/{ws}/{slug} — the same
    # path `bb repo` GETs. The slug is in the URL; the body carries the
    # settings. Workspace resolves from -w / BB_WORKSPACE / git origin.
    #
    # Default is PRIVATE so a forgotten flag never publishes a repo by
    # accident. --public flips it; --private is accepted as an explicit
    # no-op for callers who want to be explicit.
    local name="" is_private="true" project="" description=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --private)        is_private="true"; shift ;;
            --public)         is_private="false"; shift ;;
            --project)        _require_flag_value "$@"; project="$2"; shift 2 ;;
            --project=*)      project="${1#*=}"; shift ;;
            --description)    _require_flag_value "$@"; description="$2"; shift 2 ;;
            --description=*)  description="${1#*=}"; shift ;;
            -*)
                echo "Error: unknown flag for repo-create: $1" >&2
                exit 1 ;;
            *)
                if [[ -z "$name" ]]; then
                    name="$1"
                else
                    echo "Error: unexpected extra argument: $1" >&2
                    exit 1
                fi
                shift ;;
        esac
    done

    if [[ -z "$name" ]]; then
        echo "Usage: bb repo-create <name> [--private|--public] [--project KEY] [--description TEXT]" >&2
        echo "" >&2
        echo "  Creates a repo in the resolved workspace (default: PRIVATE)." >&2
        echo "  Workspace resolves from -w / BB_WORKSPACE / git origin." >&2
        exit 1
    fi

    # Resolve the workspace (this command takes a bare name, not a
    # repo arg, so use the workspace resolver). The slug is `name`.
    resolve_workspace

    # repo_path validates both the workspace and the slug at the boundary
    # (empty / whitespace / embedded '/' / '.' / '..') — same contract as
    # every other repo command. It also builds the POST path.
    local path
    path=$(repo_path "$name")

    # Build the JSON body with jq so values are escaped. Always send scm
    # and is_private; add project / description only when supplied so the
    # body matches the Python omit-when-empty contract (bb_ops.repo_create).
    local payload
    payload=$(jq -n \
        --argjson priv "$is_private" \
        '{scm: "git", is_private: $priv}')
    if [[ -n "$project" ]]; then
        payload=$(echo "$payload" | jq --arg k "$project" '. + {project: {key: $k}}')
    fi
    if [[ -n "$description" ]]; then
        payload=$(echo "$payload" | jq --arg d "$description" '. + {description: $d}')
    fi

    echo "Creating repository ${BB_WORKSPACE}/${name} (private: ${is_private})..."

    local response rc=0
    response=$(bb_post "$path" "$payload") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi

    local full_name clone_https
    full_name=$(echo "$response" | jq -r '.full_name // "(unknown)"')
    clone_https=$(echo "$response" | jq -r '[.links.clone[]? | select(.name == "https") | .href] | first // "n/a"')

    echo "Created ${full_name}"
    echo "  Clone (HTTPS): ${clone_https}"
}

cmd_repo_update() {
    # bb repo-update [repo] --project KEY [--description TEXT]
    #
    # Updates an existing repo via PUT to /repositories/{ws}/{slug} — the
    # same path `bb repo` GETs and `bb repo-create` POSTs. Only the fields
    # in the body change. The dominant use is moving a repo between
    # projects (repo-create takes a project but nothing could change it
    # afterward — this closes that gap).
    #
    # [repo] accepts the same shapes as every other repo command: bare
    # slug, ws/slug, or omitted (auto-detect from git origin). At least
    # one of --project / --description must be supplied.
    local repo_arg="" project="" description="" have_project="" have_description=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --project)        _require_flag_value "$@"; project="$2"; have_project=1; shift 2 ;;
            --project=*)      project="${1#*=}"; have_project=1; shift ;;
            --description)    _require_flag_value "$@"; description="$2"; have_description=1; shift 2 ;;
            --description=*)  description="${1#*=}"; have_description=1; shift ;;
            -*)
                echo "Error: unknown flag for repo-update: $1" >&2
                exit 1 ;;
            *)
                if [[ -z "$repo_arg" ]]; then
                    repo_arg="$1"
                else
                    echo "Error: unexpected extra argument: $1" >&2
                    exit 1
                fi
                shift ;;
        esac
    done

    # When --project is supplied, validate + strip the KEY at the boundary
    # so the two surfaces agree on the contract: bb_ops.repo_update rejects
    # an empty/whitespace project_key and strips a padded one. Without this,
    # `--project ""` would be silently dropped (and `--project "  WID  "`
    # sent verbatim, 400ing at the API) — a parity divergence from Python.
    # Strip first (true `.strip()` parity), then reject if nothing remains.
    if [[ -n "$have_project" ]]; then
        project="$(printf '%s' "$project" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        if [[ -z "$project" ]]; then
            echo "Error: --project requires a non-empty, non-whitespace KEY." >&2
            exit 1
        fi
    fi

    # At least one field must change; a PUT with an empty body is a no-op
    # round-trip. Reject BEFORE resolving the repo so the usage error
    # surfaces without an API call (parity with bb_ops.repo_update).
    # Gate on the `have_*` flags (not `-n "$value"`) so `--description ""`
    # (an intentional clear) counts as a field to change.
    if [[ -z "$have_project" && -z "$have_description" ]]; then
        echo "Usage: bb repo-update [repo] --project KEY [--description TEXT]" >&2
        echo "" >&2
        echo "  Updates an existing repo. At least one of --project /" >&2
        echo "  --description is required." >&2
        echo "  [repo] is auto-detected from the git origin if omitted." >&2
        exit 1
    fi

    local repo
    resolve_repo "$repo_arg"

    # repo_path validates the workspace + slug at the boundary (empty /
    # whitespace / embedded '/' / '.' / '..') and builds the PUT path.
    local path
    path=$(repo_path "$repo")

    # Build the body with jq so values are escaped. Add only the fields
    # supplied so the body matches the Python omit-when-absent contract
    # (bb_ops.repo_update). The `have_*` flags gate each field so an
    # intentional clear (`--description ""`) is still sent.
    local payload="{}"
    if [[ -n "$have_project" ]]; then
        payload=$(echo "$payload" | jq --arg k "$project" '. + {project: {key: $k}}')
    fi
    if [[ -n "$have_description" ]]; then
        payload=$(echo "$payload" | jq --arg d "$description" '. + {description: $d}')
    fi

    echo "Updating repository ${BB_WORKSPACE}/${repo}..."

    local response rc=0
    response=$(bb_put "$path" "$payload") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi

    local full_name new_project
    full_name=$(echo "$response" | jq -r '.full_name // "(unknown)"')
    new_project=$(echo "$response" | jq -r '.project.key // "(none)"')

    echo "Updated ${full_name}"
    echo "  Project: ${new_project}"
}

# =========================================================================
#  DOWNLOADS (deployment artifacts)
# =========================================================================

cmd_downloads() {
    local repo
    resolve_repo "${1:-}"

    echo "Downloads for ${BB_WORKSPACE}/${repo}:"
    echo ""

    local response
    response=$(bb_get "$(repo_path "$repo")/downloads?pagelen=25")

    local count
    count=$(echo "$response" | jq '.size // 0')

    if [[ "$count" == "0" ]]; then
        echo "  No downloads."
        return
    fi

    printf "  %-40s %-10s %s\n" "FILE" "SIZE" "DATE"
    printf "  %-40s %-10s %s\n" "----" "----" "----"

    echo "$response" | jq -r '
        .values[] |
        [
            .name,
            ((.size // 0) / 1024 | floor | tostring + " KB"),
            (.created_on | split("T") | .[0])
        ] | @tsv
    ' | while IFS=$'\t' read -r name size date; do
        printf "  %-40s %-10s %s\n" "$name" "$size" "$date"
    done
}

# =========================================================================
#  DEPLOYMENT ENVIRONMENTS
# =========================================================================
#
# Deployment environments are the named targets a pipeline deploys to
# (Test / Staging / Production); each carries its own deployment variables
# (managed by `bb vars [set] --deployment <env>`). These commands manage
# the environments themselves:
#   GET    $(repo_path)/environments/            list
#   POST   ...   {"name", "environment_type":{"name"}}   create
#   DELETE ...   /{env_uuid}/                    delete
# Body shape + 201/204 responses verified against the live API.

cmd_environments() {
    local repo
    resolve_repo "${1:-}"

    echo "Deployment environments for ${BB_WORKSPACE}/${repo}:"
    echo ""

    # Walk ALL pages, not just the first. The Python side
    # (bb_ops.environments_list) paginates via client.paginate, and the
    # sibling _resolve_env_uuid walks every page too — a single-page read
    # here would be a parity divergence (an environment past page 1 would
    # be invisible via bash but listed via the MCP tool). Accumulate rows
    # across pages, then render once so the header prints only when there's
    # at least one environment.
    local page_url rows="" any=""
    page_url="$(repo_path "$repo")/environments/?pagelen=100"
    while [[ -n "$page_url" ]]; do
        local page page_rows next
        page=$(bb_get "$page_url")
        page_rows=$(echo "$page" | jq -r '
            .values[] | [.name, (.environment_type.name // "-")] | @tsv')
        if [[ -n "$page_rows" ]]; then
            any=1
            rows="${rows}${rows:+$'\n'}${page_rows}"
        fi
        next=$(echo "$page" | jq -r '.next // ""')
        if [[ -z "$next" ]]; then
            page_url=""
        else
            page_url="${next#"${BB_API}"}"
        fi
    done

    if [[ -z "$any" ]]; then
        echo "  No environments."
        return
    fi

    printf "  %-25s %s\n" "NAME" "TYPE"
    printf "  %-25s %s\n" "----" "----"
    printf '%s\n' "$rows" | while IFS=$'\t' read -r name type; do
        printf "  %-25s %s\n" "$name" "$type"
    done
}

cmd_environment_create() {
    # bb environment-create [repo] <name> [--type Test|Staging|Production]
    local repo_arg="" name="" env_type="Test"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --type)    _require_flag_value "$@"; env_type="$2"; shift 2 ;;
            --type=*)  env_type="${1#*=}"; shift ;;
            -*)
                echo "Error: unknown flag for environment-create: $1" >&2
                exit 1 ;;
            *)
                # First positional is the repo IF a second positional (name)
                # follows; otherwise the single positional is the name and
                # the repo auto-detects. Collect positionals, sort out after.
                if [[ -z "$repo_arg" && -z "$name" ]]; then
                    repo_arg="$1"
                elif [[ -z "$name" ]]; then
                    name="$1"
                else
                    echo "Error: unexpected extra argument: $1" >&2
                    exit 1
                fi
                shift ;;
        esac
    done

    # One positional → it's the name (repo auto-detects). Two → repo, name.
    if [[ -z "$name" ]]; then
        name="$repo_arg"
        repo_arg=""
    fi
    if [[ -z "$name" ]]; then
        echo "Usage: bb environment-create [repo] <name> [--type Test|Staging|Production]" >&2
        exit 1
    fi

    # Validate + canonicalise the type (case-insensitive), matching
    # bb_ops.environment_create's _ENVIRONMENT_TYPES contract.
    local canonical
    case "$(printf '%s' "$env_type" | tr '[:upper:]' '[:lower:]')" in
        test)        canonical="Test" ;;
        staging)     canonical="Staging" ;;
        production)  canonical="Production" ;;
        *)
            echo "Error: --type must be one of Test / Staging / Production (got '$env_type')." >&2
            exit 1 ;;
    esac

    local repo
    resolve_repo "$repo_arg"

    local payload
    payload=$(jq -n --arg n "$name" --arg t "$canonical" \
        '{name: $n, environment_type: {name: $t}}')

    local response rc=0
    response=$(bb_post "$(repo_path "$repo")/environments/" "$payload") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi
    local created_name uuid
    created_name=$(echo "$response" | jq -r '.name // "(unknown)"')
    uuid=$(echo "$response" | jq -r '.uuid // "(none)"')
    echo "Created environment '${created_name}' (${canonical}) in ${BB_WORKSPACE}/${repo}"
    echo "  uuid: ${uuid}"
}

cmd_environment_delete() {
    # bb environment-delete [repo] <name>
    local repo_arg="" name=""
    # Same one-or-two positional logic as create.
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -*)
                echo "Error: unknown flag for environment-delete: $1" >&2
                exit 1 ;;
            *)
                if [[ -z "$repo_arg" && -z "$name" ]]; then
                    repo_arg="$1"
                elif [[ -z "$name" ]]; then
                    name="$1"
                else
                    echo "Error: unexpected extra argument: $1" >&2
                    exit 1
                fi
                shift ;;
        esac
    done
    if [[ -z "$name" ]]; then
        name="$repo_arg"
        repo_arg=""
    fi
    if [[ -z "$name" ]]; then
        echo "Usage: bb environment-delete [repo] <name>" >&2
        exit 1
    fi

    local repo
    resolve_repo "$repo_arg"

    # Resolve the NAME to a UUID (walks all pages; kills the script with a
    # clear message if no environment matches). Mirrors the deployment-var
    # path's resolution.
    local uuid encoded
    uuid=$(_resolve_env_uuid "$repo" "$name")
    encoded="${uuid//\{/%7B}"
    encoded="${encoded//\}/%7D}"

    local rc=0
    bb_delete "$(repo_path "$repo")/environments/${encoded}/" > /dev/null || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi
    echo "Deleted environment '${name}' from ${BB_WORKSPACE}/${repo}"
}

# =========================================================================
#  ENVIRONMENT / DEPLOY VARIABLES
# =========================================================================
#
# Pipeline variables live at THREE scopes, each on a differently-named
# endpoint (verified against the live API):
#   repo        $(repo_path)/pipelines_config/variables/        (underscore)
#   workspace   /workspaces/{ws}/pipelines-config/variables/    (HYPHEN)
#   deployment  $(repo_path)/deployments_config/environments/{env_uuid}/variables/
# The workspace scope uses a hyphen where the repo scope uses an
# underscore; the underscore form 404s at the workspace scope. The
# deployment scope is keyed by an environment UUID, which we resolve from
# a human-supplied environment NAME via _resolve_env_uuid.

# Resolve a deployment-environment NAME (or slug) to its brace-wrapped
# UUID. Matches case-insensitively on name then slug. Sets BB_WORKSPACE
# is already done by the caller; this only reads. Echoes the uuid on
# success; exits with a labelled error on no-match or lookup failure.
_resolve_env_uuid() {
    local repo="$1" env_name="$2"
    # Walk ALL pages of environments, not just the first. The Python side
    # (_resolve_environment_uuid uses client.paginate) walks every page, so
    # a single-page read here would be a parity divergence: an environment
    # past page 1 would resolve via the MCP tool but 404-equivalent via the
    # bash CLI. The duplicate-key find-loop in cmd_vars_set follows `next`
    # for the same reason; mirror it here.
    local page_url uuid="" available=""
    page_url="$(repo_path "$repo")/environments/?pagelen=100"
    while [[ -n "$page_url" ]]; do
        local page rc=0
        page=$(bb_get "$page_url") || rc=$?
        if [[ "$rc" -ne 0 ]]; then
            echo "Error: could not list environments for ${BB_WORKSPACE}/${repo} (exit $rc)." >&2
            # This runs inside `$(...)`; a bare `exit` would only terminate
            # the subshell and the caller would proceed with an empty uuid.
            # Kill the parent script (same pattern as repo_path).
            kill -TERM $$
        fi
        # Case-insensitive match on name OR slug; `first` keeps it out of a
        # SIGPIPE-prone head pipeline and emits at most one match. Select
        # the matching ENTRY (not its .uuid) so a matched-but-null-uuid env
        # is distinguished from no-match: `.uuid // empty` alone would
        # collapse a matched env with uuid:null to "" and produce the
        # misleading "no environment named X" error even though X matched.
        # Python (_resolve_environment_uuid) raises "found but has no uuid"
        # for this shape; mirror it (parity).
        local matched uuid_val
        matched=$(echo "$page" | jq -c --arg n "$env_name" '
            ($n | ascii_downcase) as $t
            | first(.values[]
                | select((.name // "" | ascii_downcase) == $t
                      or (.slug // "" | ascii_downcase) == $t)) // empty')
        if [[ -n "$matched" ]]; then
            uuid_val=$(printf '%s' "$matched" | jq -r '.uuid // empty')
            if [[ -z "$uuid_val" ]]; then
                echo "Error: deployment environment '$env_name' found but has no uuid." >&2
                kill -TERM $$
            fi
            printf '%s' "$uuid_val"
            return
        fi
        # Accumulate available names across pages for the not-found message.
        local page_names
        page_names=$(echo "$page" | jq -r '[.values[].name] | join(", ")')
        if [[ -n "$page_names" ]]; then
            [[ -n "$available" ]] && available="${available}, "
            available="${available}${page_names}"
        fi
        # Follow the `next` link (a full URL); strip the API base to a path.
        local next
        next=$(echo "$page" | jq -r '.next // ""')
        if [[ -z "$next" ]]; then
            page_url=""
        else
            page_url="${next#"${BB_API}"}"
        fi
    done
    echo "Error: no deployment environment named '$env_name' in ${BB_WORKSPACE}/${repo}." >&2
    echo "  Available: ${available:-none}" >&2
    kill -TERM $$
}

# Build the variables collection base PATH for the requested scope.
# Args: <scope> <repo> [<env_name>]. Echoes the path (no query string).
# For the deployment scope this resolves the env name to a UUID (one
# extra GET). repo_path validates workspace + slug at the boundary.
_vars_base_path() {
    local scope="$1" repo="$2" env_name="${3:-}"
    case "$scope" in
        repo)
            printf '%s' "$(repo_path "$repo")/pipelines_config/variables/" ;;
        workspace)
            # HYPHEN form, verified live; underscore 404s here. The
            # workspace is validated the same way repo_path validates it,
            # INCLUDING the whitespace-only check (a bare `-z` test passes
            # for "   ", which would build `/workspaces/   /...` and 404
            # with no curated error). `tr -d '[:space:]'` matches repo_path
            # and Python's `.strip()` emptiness check (parity).
            local _ws_stripped
            _ws_stripped="$(printf '%s' "${BB_WORKSPACE:-}" | tr -d '[:space:]')"
            if [[ -z "$_ws_stripped" || "$BB_WORKSPACE" == */* \
                  || "$BB_WORKSPACE" == "." || "$BB_WORKSPACE" == ".." ]]; then
                echo "Error: invalid workspace for workspace-scope variables: '${BB_WORKSPACE:-}'." >&2
                # Inside `$(...)`; kill the parent, not just the subshell.
                kill -TERM $$
            fi
            printf '%s' "/workspaces/${BB_WORKSPACE}/pipelines-config/variables/" ;;
        deployment)
            local env_uuid encoded
            env_uuid=$(_resolve_env_uuid "$repo" "$env_name")
            encoded=$(_url_encode_segment "$env_uuid")
            printf '%s' "$(repo_path "$repo")/deployments_config/environments/${encoded}/variables/" ;;
        *)
            echo "Error: unknown variables scope: $scope" >&2
            kill -TERM $$ ;;
    esac
}

# A human label for the scope, used in list/set output banners.
_vars_scope_label() {
    case "$1" in
        repo)       printf 'Repository' ;;
        workspace)  printf 'Workspace' ;;
        deployment) printf 'Deployment-environment' ;;
    esac
}

cmd_vars() {
    # bb vars [--workspace | --deployment <env>] [repo]
    local scope="repo" environment="" repo_arg=""
    local positionals=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --workspace)      scope="workspace"; shift ;;
            --deployment)     _require_flag_value "$@"; scope="deployment"; environment="$2"; shift 2 ;;
            --deployment=*)   scope="deployment"; environment="${1#*=}"; shift ;;
            -*)
                echo "Error: unknown flag for vars: $1" >&2
                exit 1 ;;
            *)
                positionals+=("$1"); shift ;;
        esac
    done
    case "${#positionals[@]}" in
        0) ;;
        1) repo_arg="${positionals[0]}" ;;
        *)
            echo "Usage: bb vars [--workspace | --deployment <env>] [repo]" >&2
            exit 1 ;;
    esac
    if [[ "$scope" == "deployment" && -z "$environment" ]]; then
        echo "Error: --deployment requires an environment name." >&2
        exit 1
    fi
    # Reject a stale --deployment env left over when a later --workspace
    # (or default repo scope) won the last-flag-wins race. Mirrors the
    # Python boundary ("environment is only valid for the deployment
    # scope") so both surfaces reject the contradictory combination.
    if [[ "$scope" != "deployment" && -n "$environment" ]]; then
        echo "Error: --deployment <env> conflicts with --workspace / repo scope." >&2
        exit 1
    fi

    local repo=""
    if [[ "$scope" == "workspace" ]]; then
        # No repo for workspace scope; borrow a workspace from a repo
        # hint if given, else resolve the workspace from -w / origin /
        # BB_WORKSPACE.
        if [[ -n "$repo_arg" ]]; then
            resolve_repo "$repo_arg"
        else
            resolve_workspace
        fi
    else
        resolve_repo "$repo_arg"
    fi

    local base
    base=$(_vars_base_path "$scope" "$repo" "$environment")

    local target="${BB_WORKSPACE}"
    [[ "$scope" != "workspace" ]] && target="${BB_WORKSPACE}/${repo}"
    [[ "$scope" == "deployment" ]] && target="${target} [env: ${environment}]"
    echo "$(_vars_scope_label "$scope") variables for ${target}:"
    echo ""

    # rc-capture so a 5xx / expired-token on the list exits on the spot
    # rather than falling through to a jq parse of an empty response
    # (parity with cmd_vars_set's lookup-failure handling). bb_get has
    # already printed the API's reason.
    local response rc=0
    response=$(bb_get "${base}?pagelen=100") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi

    printf "  %-30s %-8s %s\n" "KEY" "SECURED" "VALUE"
    printf "  %-30s %-8s %s\n" "---" "-------" "-----"

    echo "$response" | jq -r '
        .values[] |
        [
            .key,
            (.secured | tostring),
            (if .secured then "********" else .value end)
        ] | @tsv
    ' | while IFS=$'\t' read -r key secured value; do
        printf "  %-30s %-8s %s\n" "$key" "$secured" "$value"
    done
}

cmd_vars_set() {
    # bb vars set [--workspace | --deployment <env>] [repo] <KEY> [--secured]
    #             (--value V | --value-file F | --value-env E)
    #
    # Create-or-update a pipeline variable at the chosen scope (repo by
    # default, --workspace, or --deployment <env>). The value is read from
    # a literal flag, a file, or an environment variable. For SECRET values
    # prefer --value-file or --value-env so the secret never lands in argv /
    # the process list / shell history. A secured value is NEVER echoed back.
    #
    # `set` is consumed by the dispatcher before this function is called;
    # the remaining args are [repo] KEY and the flags. The first non-flag
    # positional may be a repo (slug or ws/slug); KEY is the positional
    # that follows. To disambiguate, we collect positionals: 1 → it's the
    # KEY (repo auto-detected); 2 → first is repo, second is KEY. For the
    # workspace scope there is no repo, so a single positional is the KEY
    # and a second positional is treated as a workspace-bearing repo hint.
    local secured="false"
    local value_set="" value="" value_file="" value_env=""
    local scope="repo" environment=""
    local positionals=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --secured)        secured="true"; shift ;;
            --workspace)      scope="workspace"; shift ;;
            --deployment)     _require_flag_value "$@"; scope="deployment"; environment="$2"; shift 2 ;;
            --deployment=*)   scope="deployment"; environment="${1#*=}"; shift ;;
            --value)          _require_flag_value "$@"; value="$2"; value_set="1"; shift 2 ;;
            --value=*)        value="${1#*=}"; value_set="1"; shift ;;
            --value-file)     _require_flag_value "$@"; value_file="$2"; shift 2 ;;
            --value-file=*)   value_file="${1#*=}"; shift ;;
            --value-env)      _require_flag_value "$@"; value_env="$2"; shift 2 ;;
            --value-env=*)    value_env="${1#*=}"; shift ;;
            -*)
                echo "Error: unknown flag for vars set: $1" >&2
                exit 1 ;;
            *)
                positionals+=("$1"); shift ;;
        esac
    done

    if [[ "$scope" == "deployment" && -z "$environment" ]]; then
        echo "Error: --deployment requires an environment name." >&2
        exit 1
    fi
    # Reject a stale --deployment env when --workspace / repo scope won the
    # last-flag-wins race (parity with the Python boundary).
    if [[ "$scope" != "deployment" && -n "$environment" ]]; then
        echo "Error: --deployment <env> conflicts with --workspace / repo scope." >&2
        exit 1
    fi

    local repo_arg="" key=""
    case "${#positionals[@]}" in
        1) key="${positionals[0]}" ;;
        2) repo_arg="${positionals[0]}"; key="${positionals[1]}" ;;
        *)
            echo "Usage: bb vars set [repo] <KEY> [--secured] (--value V | --value-file F | --value-env E)" >&2
            exit 1 ;;
    esac

    if [[ -z "$key" ]]; then
        echo "Usage: bb vars set [repo] <KEY> [--secured] (--value V | --value-file F | --value-env E)" >&2
        exit 1
    fi

    # Strip surrounding whitespace from the key — parity with the Python
    # side (bb_ops.vars_set does `key = key.strip()`). Without this, a
    # copy-paste key like ` AWS_REGION ` would (a) not match the stored
    # `AWS_REGION` in the find step and (b) POST a duplicate variable
    # keyed with the stray spaces, breaking the never-duplicate contract
    # across the two surfaces. Reject a whitespace-only key here too.
    # Trim leading/trailing whitespace only (matches Python's .strip(),
    # which does NOT collapse internal whitespace). `extglob` is not
    # assumed; use two parameter expansions with a [:space:] class.
    key="${key#"${key%%[![:space:]]*}"}"  # strip leading whitespace
    key="${key%"${key##*[![:space:]]}"}"  # strip trailing whitespace
    if [[ -z "$key" ]]; then
        echo "Error: KEY must be a non-empty, non-whitespace string." >&2
        exit 1
    fi

    # Exactly one value source. Count the supplied sources so an
    # ambiguous (two) or empty (none) call is rejected before any value
    # is read or any request is sent.
    local source_count=0
    [[ -n "$value_set" ]] && source_count=$((source_count + 1))
    [[ -n "$value_file" ]] && source_count=$((source_count + 1))
    [[ -n "$value_env" ]] && source_count=$((source_count + 1))
    if [[ "$source_count" -ne 1 ]]; then
        echo "Error: provide exactly one of --value, --value-file, or --value-env." >&2
        exit 1
    fi

    # Resolve the value WITHOUT echoing it.
    local resolved_value=""
    if [[ -n "$value_set" ]]; then
        resolved_value="$value"
    elif [[ -n "$value_file" ]]; then
        if [[ ! -r "$value_file" ]]; then
            echo "Error: cannot read --value-file '$value_file'." >&2
            exit 1
        fi
        # Strip EXACTLY ONE trailing newline (so `echo secret > f` doesn't
        # carry the newline into Bitbucket), matching the Python side
        # (`if resolved_value.endswith("\n"): resolved_value[:-1]`). A
        # bare `$(cat ...)` strips ALL trailing newlines, which would
        # diverge from Python for a file ending in a blank line — read
        # the raw bytes and drop just one '\n' if present.
        resolved_value="$(cat "$value_file"; printf 'x')"
        resolved_value="${resolved_value%x}"   # undo the sentinel that protected trailing newlines
        resolved_value="${resolved_value%$'\n'}"  # strip exactly one trailing newline
    else
        # --value-env: read the named variable. `${!name}` is bash
        # indirect expansion. Guard "set but empty" vs "unset" via the
        # ${var+x} test so an unset var is a clear error, not a silent
        # empty value.
        if [[ -z "${!value_env+x}" ]]; then
            echo "Error: --value-env '$value_env' is not set in the environment." >&2
            exit 1
        fi
        resolved_value="${!value_env}"
    fi

    local repo=""
    if [[ "$scope" == "workspace" ]]; then
        # No repo for workspace scope; borrow a workspace from a repo
        # hint if given, else resolve from -w / origin / BB_WORKSPACE.
        if [[ -n "$repo_arg" ]]; then
            resolve_repo "$repo_arg"
        else
            resolve_workspace
        fi
    else
        resolve_repo "$repo_arg"
    fi
    local base
    base=$(_vars_base_path "$scope" "$repo" "$environment")

    # Find an existing variable by key (walk all pages). Bitbucket allows
    # duplicate keys via the API, so a POST when a PUT was needed creates
    # a second variable with the same key — find-first prevents that.
    local existing_uuid="" page_url="${base}?pagelen=100"
    while [[ -n "$page_url" ]]; do
        # Declare and assign on separate lines: a `local page=$(...)`
        # one-liner masks the command substitution's exit status behind
        # the `local` builtin's own (always 0), so a failed lookup GET
        # would slip past `set -e` and leave `page` empty — which the jq
        # below reads as "key not found", then we'd POST a duplicate. The
        # rc-capture surfaces the auth/network failure instead.
        local page rc=0
        page=$(bb_get "$page_url") || rc=$?
        if [[ "$rc" -ne 0 ]]; then
            # State what the failure MEANS for the write, which the API
            # response cannot know: the lookup that decides create-vs-update
            # never completed, so nothing was written and no duplicate was
            # created. bb_get already printed why the lookup failed.
            echo "  The lookup for an existing '${key}' did not complete;" >&2
            echo "  aborting before any write, so no duplicate was created." >&2
            exit "$rc"
        fi
        # Use jq's `first(...)` to emit at most one match rather than
        # piping into `head -n1`: with duplicate-keyed variables (the
        # case this find defends against), `jq ... | head -n1` makes head
        # close the pipe early, jq dies on SIGPIPE, and `pipefail` turns
        # a routine update into a hard abort. `first` keeps it all in jq.
        #
        # Select the first matching ENTRY (not its .uuid) so we can tell
        # "no match" from "matched but uuid is null/missing". Emitting just
        # `.uuid // empty` would collapse a matched-but-null-uuid entry to
        # "" and fall through to a CREATE, silently POSTing a duplicate of
        # a key that already exists. Bitbucket has returned uuid:null on
        # partially-provisioned entries; Python (bb_ops.vars_set) raises
        # BBOpNotFound for this exact shape, so mirror it here (parity).
        local matched uuid_val
        matched=$(echo "$page" | jq -c --arg k "$key" \
            'first(.values[] | select(.key == $k)) // empty')
        if [[ -n "$matched" ]]; then
            uuid_val=$(printf '%s' "$matched" | jq -r '.uuid // empty')
            if [[ -z "$uuid_val" ]]; then
                echo "Error: variable '${key}' exists but has no uuid; cannot update." >&2
                echo "  Refusing to create a duplicate. Inspect it in the Bitbucket UI." >&2
                exit 1
            fi
            existing_uuid="$uuid_val"
            break
        fi
        # Follow the `next` link if present. bb_get takes a path, but the
        # API's `next` is a full URL; strip the API base to re-derive the
        # path. If there's no next link, stop.
        local next
        next=$(echo "$page" | jq -r '.next // ""')
        if [[ -z "$next" ]]; then
            page_url=""
        else
            page_url="${next#"${BB_API}"}"
        fi
    done

    # Build the body with jq so the value is JSON-escaped regardless of
    # content. --arg keeps it a string; never interpolated into a shell
    # word, so a value with quotes / newlines / shell metacharacters is
    # safe and never appears in a command line.
    local payload
    payload=$(jq -n \
        --arg key "$key" \
        --arg value "$resolved_value" \
        --argjson secured "$secured" \
        '{key: $key, value: $value, secured: $secured}')

    local action response rc=0
    if [[ -n "$existing_uuid" ]]; then
        action="Updated"
        # The uuid comes back brace-wrapped (`{...}`); URL-encode it for
        # the path segment so the braces don't break the URL.
        local encoded_uuid
        encoded_uuid=$(_url_encode_segment "$existing_uuid")
        response=$(bb_put "${base}${encoded_uuid}" "$payload") || rc=$?
    else
        action="Created"
        response=$(bb_post "$base" "$payload") || rc=$?
    fi

    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi

    # NEVER echo the value. Report key + scope + secured flag + action only.
    local target="${BB_WORKSPACE}"
    [[ "$scope" != "workspace" ]] && target="${BB_WORKSPACE}/${repo}"
    [[ "$scope" == "deployment" ]] && target="${target} [env: ${environment}]"
    echo "${action} ${scope} variable ${key} on ${target} (secured: ${secured})"
}

cmd_vars_delete() {
    # bb vars delete [--workspace | --deployment <env>] [repo] <KEY>
    #
    # Delete a pipeline variable by key at the chosen scope. Resolves the
    # key to its uuid (walking all pages, same as `vars set`), then DELETEs
    # `.../variables/{uuid}`. A key that doesn't exist at the scope is a
    # clean not-found error with NO delete issued, so a typo can't no-op.
    #
    # `delete` is consumed by the dispatcher before this runs; remaining
    # args are [repo] KEY plus scope flags. Same positional disambiguation
    # as `vars set`: 1 positional → KEY (repo auto-detected); 2 → repo, KEY.
    local scope="repo" environment=""
    local positionals=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --workspace)      scope="workspace"; shift ;;
            --deployment)     _require_flag_value "$@"; scope="deployment"; environment="$2"; shift 2 ;;
            --deployment=*)   scope="deployment"; environment="${1#*=}"; shift ;;
            -*)
                echo "Error: unknown flag for vars delete: $1" >&2
                exit 1 ;;
            *)
                positionals+=("$1"); shift ;;
        esac
    done

    if [[ "$scope" == "deployment" && -z "$environment" ]]; then
        echo "Error: --deployment requires an environment name." >&2
        exit 1
    fi
    if [[ "$scope" != "deployment" && -n "$environment" ]]; then
        echo "Error: --deployment <env> conflicts with --workspace / repo scope." >&2
        exit 1
    fi

    local repo_arg="" key=""
    case "${#positionals[@]}" in
        1) key="${positionals[0]}" ;;
        2) repo_arg="${positionals[0]}"; key="${positionals[1]}" ;;
        *)
            echo "Usage: bb vars delete [--workspace | --deployment <env>] [repo] <KEY>" >&2
            exit 1 ;;
    esac

    # Strip leading/trailing whitespace from the key (parity with
    # bb_ops.vars_delete's `key.strip()`); reject whitespace-only.
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    if [[ -z "$key" ]]; then
        echo "Usage: bb vars delete [--workspace | --deployment <env>] [repo] <KEY>" >&2
        exit 1
    fi

    local repo=""
    if [[ "$scope" == "workspace" ]]; then
        if [[ -n "$repo_arg" ]]; then
            resolve_repo "$repo_arg"
        else
            resolve_workspace
        fi
    else
        resolve_repo "$repo_arg"
    fi
    local base
    base=$(_vars_base_path "$scope" "$repo" "$environment")

    # Find the variable by key (walk all pages). Same rc-capture and
    # `first(...)`-not-`head` discipline as cmd_vars_set's lookup.
    local existing_uuid="" page_url="${base}?pagelen=100"
    while [[ -n "$page_url" ]]; do
        local page rc=0
        page=$(bb_get "$page_url") || rc=$?
        if [[ "$rc" -ne 0 ]]; then
            # State what the failure means for the delete, which the API
            # response cannot know: the key was never resolved to a UUID,
            # so nothing was deleted.
            echo "  The lookup resolving '${key}' did not complete;" >&2
            echo "  aborting before any delete." >&2
            exit "$rc"
        fi
        local matched uuid_val
        matched=$(echo "$page" | jq -c --arg k "$key" \
            'first(.values[] | select(.key == $k)) // empty')
        if [[ -n "$matched" ]]; then
            uuid_val=$(printf '%s' "$matched" | jq -r '.uuid // empty')
            if [[ -z "$uuid_val" ]]; then
                echo "Error: variable '${key}' found but has no uuid; cannot delete." >&2
                exit 1
            fi
            existing_uuid="$uuid_val"
            break
        fi
        local next
        next=$(echo "$page" | jq -r '.next // ""')
        if [[ -z "$next" ]]; then
            page_url=""
        else
            page_url="${next#"${BB_API}"}"
        fi
    done

    # Not found → clean error, NO delete issued (parity with
    # bb_ops.vars_delete raising BBOpNotFound before any DELETE).
    if [[ -z "$existing_uuid" ]]; then
        local where="the ${scope} scope"
        [[ "$scope" == "deployment" ]] && where="${where} (environment '${environment}')"
        echo "Error: no variable named '${key}' at ${where}." >&2
        exit 1
    fi

    local encoded_uuid rc=0
    encoded_uuid=$(_url_encode_segment "$existing_uuid")
    bb_delete "${base}${encoded_uuid}" > /dev/null || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        exit "$rc"
    fi

    local target="${BB_WORKSPACE}"
    [[ "$scope" != "workspace" ]] && target="${BB_WORKSPACE}/${repo}"
    [[ "$scope" == "deployment" ]] && target="${target} [env: ${environment}]"
    echo "Deleted ${scope} variable ${key} from ${target}"
}

# =========================================================================
#  OPEN IN BROWSER
# =========================================================================

cmd_whoami() {
    # Report the resolved config + git context. Useful as a connectivity
    # smoke test before invasive operations. NEVER echoes BB_TOKEN and
    # NEVER echoes credentials embedded in the origin URL (a common
    # pattern for token-based git auth — `whoami` output gets pasted
    # into bug reports / screenshots, can't have secrets in it).
    echo "bb configuration:"
    echo "  User:      ${BB_USER}"
    if [[ -n "${BB_WORKSPACE:-}" ]]; then
        echo "  Workspace: ${BB_WORKSPACE} (default)"
    else
        echo "  Workspace: (not set — auto-detected per-repo from git origin)"
    fi
    echo "  API:       ${BB_API}"
    echo "  Token:     [set, redacted]"

    echo ""
    echo "Git context:"
    local cwd_branch cwd_remote
    cwd_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "(not a git repo)")
    # Strip `user:token@` from origin URL before echoing. Matches the
    # `_redact_url` helper in mcp_server.py / git_ops.py: anything of
    # the form `://user:token@host/path` becomes `://[redacted]@host/path`.
    #
    # Use an `if` block (not `git ... | sed ... || echo`) because in a
    # pipeline the `||` attaches to sed, which always exits 0 on
    # empty input — so the fallback would never fire when git itself
    # failed (no origin remote / not in a git repo). The if-form
    # branches on git's exit status, not sed's.
    local _git_remote_raw
    if _git_remote_raw=$(git remote get-url origin 2>/dev/null); then
        cwd_remote=$(printf '%s' "$_git_remote_raw" | sed -E 's#://[^/@]+@#://[redacted]@#')
    else
        cwd_remote="(no origin remote)"
    fi
    echo "  Cwd:       $(pwd)"
    echo "  Branch:    ${cwd_branch}"
    echo "  Origin:    ${cwd_remote}"

    # Light reachability check. Probe the configured workspace
    # endpoint (not /user — workspace-scoped tokens, which Atlassian
    # now recommends, reject /user with 401/403 while serving
    # /repositories/{workspace} correctly). False-negative on /user
    # would tell a user with a valid token to rotate it.
    #
    # Converse caveat: /repositories/{workspace} requires
    # `repository:read` scope. A workspace-scoped token granting only
    # `pipelines:read` or `pullrequest:read` will fail this probe even
    # though `bb pipelines` / `bb prs` still work. Treat this as a
    # scope hint, not a global credential verdict.
    echo ""
    echo "Auth check:"
    # BB_WORKSPACE is optional now, so pick a workspace to probe: the
    # configured/flag default if set, else the git origin's workspace.
    # If neither resolves, skip the probe rather than build a bad URL.
    local probe_ws="${BB_WORKSPACE:-}"
    if [[ -z "$probe_ws" && -n "${_git_remote_raw:-}" ]]; then
        local _pr="${_git_remote_raw%/}"
        if [[ "$_pr" =~ [:/]([^/:]+)/([^/]+)$ ]]; then
            probe_ws="${BASH_REMATCH[1]}"
        fi
    fi
    if [[ -z "$probe_ws" ]]; then
        echo "  Skipped — no workspace to probe (set BB_WORKSPACE or run"
        echo "  inside a Bitbucket git checkout). Config + token look set."
    elif bb_get "/repositories/${probe_ws}?pagelen=1" > /dev/null 2>&1; then
        echo "  Workspace '${probe_ws}' reachable — auth OK."
    else
        echo "  Workspace '${probe_ws}' NOT reachable — token may be invalid,"
        echo "  expired, scoped to a different workspace, or missing"
        echo "  repository:read (pipeline/PR-only scoped tokens still work"
        echo "  for those commands)."
        echo "  Rotate at https://id.atlassian.com/manage-profile/security/api-tokens"
    fi
}

cmd_open() {
    local repo
    resolve_repo "${1:-}"
    local section="${2:-}"

    local url="https://bitbucket.org/${BB_WORKSPACE}/${repo}"

    case "$section" in
        pr|prs)       url="${url}/pull-requests" ;;
        pipelines|pl) url="${url}/addon/pipelines/home" ;;
        branches|br)  url="${url}/branches" ;;
        settings)     url="${url}/admin" ;;
        commits)      url="${url}/commits" ;;
        *)            ;; # default: repo home
    esac

    echo "Opening: ${url}"
    open "$url" 2>/dev/null || xdg-open "$url" 2>/dev/null || echo "  $url"
}

# =========================================================================
#  HELP
# =========================================================================

cmd_help() {
    cat <<'HELP'
bb - Bitbucket CLI

PIPELINES
  bb pipelines [repo] [count]           List recent pipelines (default: 10)
  bb pipeline [repo] <number>           Show pipeline details and steps
  bb watch [repo] [number] [interval]   Poll pipeline until done (default: 15s)
  bb logs [repo] <number> [step]        Show step logs
  bb trigger [repo] [branch] [pattern] [--var KEY=VALUE ...]
                                        Trigger a pipeline run ([pattern] selects a
                                          custom: pipeline; --var/-v is repeatable and
                                          passes per-run variables; trailing KEY=VALUE
                                          positional pairs also accepted)
  bb stop [repo] <number>               Stop a running pipeline
  bb approve [repo] <number>            Open pipeline in browser (manual steps require UI)
  bb pipelines-status [repo]            Show whether Pipelines (CI) is enabled
  bb pipelines-enable [repo]            Enable Pipelines (CI) on a repo
  bb pipelines-disable [repo]           Disable Pipelines (CI) on a repo
                                          (enable/disable need admin:pipeline:bitbucket)

PULL REQUESTS
  bb prs [repo] [state]                 List PRs (default: OPEN)
  bb pr [repo] <id>                     View PR details
  bb pr-create [--repo R] <title> [dest]  Create PR from current branch
                                          <title> is the FIRST positional; repo is
                                          --repo (slug or ws/slug) or auto-detected.
                                          opt: --close-source-branch (delete the
                                          source branch when the PR merges; kept
                                          by default);
                                          --description TEXT | --description-file PATH
                                          (or pipe a body: pr-create ... < body.md);
                                          --reviewer UUID (repeatable; get UUIDs
                                          from bb members)
  bb pr-update [repo] <id> [--title T] [--description D | --description-file F]
                                        Update a PR title and/or description
                                          opt: --reviewer UUID (repeatable; adds,
                                          keeping existing reviewers)
                                          opt: --remove-reviewer UUID (repeatable)
                                          opt: --drop-approvals (allow removing a
                                          reviewer who already approved)
  bb pr-approve [repo] <id>             Approve a PR
  bb pr-unapprove [repo] <id>           Remove your approval on a PR
  bb pr-merge [repo] <id> [strategy]    Merge a PR (merge_commit|squash|fast_forward)
                                          opt: --close-source-branch (delete the
                                          source branch on merge; kept by default,
                                          overriding the PR's stored setting)
  bb pr-decline [repo] <id>             Decline a PR
  bb pr-diff [repo] <id>                Show PR diff
  bb pr-comments [repo] <id>            Show PR comments
  bb pr-comment [repo] <id> <body>      Add a comment to a PR

BRANCHES
  bb branches [repo]                    List branches
  bb branch [repo] <name>               Show a single branch
  bb commits [repo] [branch] [count]    List recent commits (default count: 10)

REPOSITORY
  bb workspaces                         List workspaces you belong to (needs read:workspace:bitbucket scope)
  bb projects [workspace]               List workspace projects (needs read:project:bitbucket scope)
  bb members [workspace]                List workspace members + their UUIDs
                                          (the UUID --reviewer takes)
  bb repos                              List workspace repos
  bb repo [repo]                        Show repo details
  bb repo-create <name> [opts]          Create a repo (default PRIVATE)
                                          opts: --public | --private,
                                          --project KEY, --description TEXT
  bb repo-update [repo] <opts>          Update a repo (move project, change description)
                                          opts: --project KEY, --description TEXT
                                          (at least one required)
  bb downloads [repo]                   List repo downloads
  bb environments [repo]                List deployment environments
  bb environment-create [repo] <name>   Create a deployment environment
                                          opts: --type Test|Staging|Production
                                          (default Test; needs admin:pipeline:bitbucket)
  bb environment-delete [repo] <name>   Delete a deployment environment
                                          (needs admin:pipeline:bitbucket)
  bb vars [scope] [repo]                List pipeline variables
                                          scope: --workspace | --deployment <env>
                                          (default: repo)
  bb vars set [scope] [repo] <KEY> [opts]  Create or update a pipeline variable
                                          scope: --workspace | --deployment <env>
                                          opts: --secured, and exactly one of
                                          --value V | --value-file F | --value-env E
                                          (use --value-file/--value-env for secrets)
  bb vars delete [scope] [repo] <KEY>   Delete a pipeline variable (destructive)
    (alias: bb vars rm ...)               scope: --workspace | --deployment <env>
                                          (default: repo)

UTILITIES
  bb whoami                             Show resolved config + git context
  bb open [repo] [section]              Open in browser (pr|pipelines|branches|settings|commits)
  bb help                               Show this help

GLOBAL FLAGS
  -w, --workspace <name>                Override BB_WORKSPACE for this invocation

  Example: bb -w acme repos

NOTES
  [repo] is auto-detected from the current git remote if omitted.
  For PR-id commands (pr / pr-merge / pr-diff / pr-comments / pr-approve /
  pr-unapprove / pr-decline / pr-comment), a bare numeric first arg is
  treated as the PR id and the repo is auto-detected (e.g. `bb pr 42` from
  inside the checkout). Pass `workspace/slug <id>` for an explicit repo.
  Config: ~/.config/bb/config or env vars BB_USER, BB_TOKEN, BB_WORKSPACE.
  BB_DEBUG=1 traces each request as "[bb] METHOD /path -> status" on
  stderr. Failed requests always print the API's own error, including the
  required and granted token scopes on a 403.

  Auth uses Atlassian API tokens with HTTP Basic auth.
  BB_USER is your Bitbucket account email address.
  Create a token at: https://id.atlassian.com/manage-profile/security/api-tokens

  For agent-driven workflows, register the MCP server (mcp_server.py)
  with Claude Code. See README.md for the install path.
HELP
}

# --- Main ---

# Parse global flags before the command
while [[ "${1:-}" == -* ]]; do
    case "$1" in
        -w|--workspace)
            BB_WORKSPACE_OVERRIDE="$2"
            shift 2
            ;;
        --workspace=*)
            BB_WORKSPACE_OVERRIDE="${1#*=}"
            shift
            ;;
        --help|-h)
            cmd_help
            exit 0
            ;;
        *)
            echo "Unknown flag: $1" >&2
            echo "Run 'bb help' for usage." >&2
            exit 1
            ;;
    esac
done

command="${1:-help}"
shift || true

# Allow help without credentials
if [[ "$command" == "help" || "$command" == "--help" || "$command" == "-h" ]]; then
    cmd_help
    exit 0
fi

load_config

# Apply workspace override after config load
if [[ -n "${BB_WORKSPACE_OVERRIDE:-}" ]]; then
    BB_WORKSPACE="$BB_WORKSPACE_OVERRIDE"
fi

case "$command" in
    # Pipelines
    pipelines|pls)        cmd_pipelines "$@" ;;
    pipeline|pl)          cmd_pipeline "$@" ;;
    watch|w)              cmd_watch "$@" ;;
    logs|l)               cmd_logs "$@" ;;
    trigger|run)          cmd_pipeline_trigger "$@" ;;
    stop)                 cmd_pipeline_stop "$@" ;;
    approve|ap)           cmd_pipeline_approve "$@" ;;
    pipelines-enable|pl-on)    cmd_pipelines_enable "$@" ;;
    pipelines-disable|pl-off)  cmd_pipelines_disable "$@" ;;
    pipelines-status|pl-status) cmd_pipelines_status "$@" ;;
    # Pull Requests
    prs|pr-list)          cmd_pr_list "$@" ;;
    pr|pr-view)           cmd_pr_view "$@" ;;
    pr-create|prc)        cmd_pr_create "$@" ;;
    pr-update|pr-edit)    cmd_pr_update "$@" ;;
    pr-approve|pra)       cmd_pr_approve "$@" ;;
    pr-unapprove|prua)    cmd_pr_unapprove "$@" ;;
    pr-merge|prm)         cmd_pr_merge "$@" ;;
    pr-decline|prd)       cmd_pr_decline "$@" ;;
    pr-diff)              cmd_pr_diff "$@" ;;
    pr-comments|pr-comm)  cmd_pr_comments "$@" ;;
    pr-comment)           cmd_pr_comment_add "$@" ;;
    # Branches
    branches|br)          cmd_branches "$@" ;;
    branch)               cmd_branch_show "$@" ;;
    commits)              cmd_commits "$@" ;;
    # Repos
    workspaces|ws)        cmd_workspaces "$@" ;;
    projects|proj)        cmd_projects "$@" ;;
    members|mem)          cmd_members "$@" ;;
    repos)                cmd_repos "$@" ;;
    repo)                 cmd_repo "$@" ;;
    repo-create|rc)       cmd_repo_create "$@" ;;
    repo-update|ru)       cmd_repo_update "$@" ;;
    downloads|dl)         cmd_downloads "$@" ;;
    environments|envs)    cmd_environments "$@" ;;
    environment-create|env-create)  cmd_environment_create "$@" ;;
    environment-delete|env-delete)  cmd_environment_delete "$@" ;;
    vars)
        # `vars set ...` create-or-updates; `vars delete ...` removes;
        # bare `vars` (or `vars <repo>`) lists. Peek at the first arg.
        if [[ "${1:-}" == "set" ]]; then
            shift
            cmd_vars_set "$@"
        elif [[ "${1:-}" == "delete" || "${1:-}" == "rm" ]]; then
            shift
            cmd_vars_delete "$@"
        else
            cmd_vars "$@"
        fi
        ;;
    # Utilities
    whoami)               cmd_whoami ;;
    open|o)               cmd_open "$@" ;;
    help|--help|-h)       cmd_help ;;
    *)
        echo "Unknown command: $command" >&2
        echo "Run 'bb help' for usage." >&2
        exit 1
        ;;
esac
