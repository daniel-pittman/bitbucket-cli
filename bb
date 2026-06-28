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

bb_get() {
    local path="$1"
    shift
    curl -sf -u "${BB_USER}:${BB_TOKEN}" "${BB_API}${path}" "$@"
}

bb_post() {
    local path="$1"
    local data="${2:-}"
    if [[ -n "$data" ]]; then
        curl -sf -u "${BB_USER}:${BB_TOKEN}" \
            -X POST -H "Content-Type: application/json" \
            -d "$data" "${BB_API}${path}"
    else
        curl -sf -u "${BB_USER}:${BB_TOKEN}" \
            -X POST "${BB_API}${path}"
    fi
}

bb_put() {
    local path="$1"
    local data="${2:-}"
    if [[ -n "$data" ]]; then
        curl -sf -u "${BB_USER}:${BB_TOKEN}" \
            -X PUT -H "Content-Type: application/json" \
            -d "$data" "${BB_API}${path}"
    else
        curl -sf -u "${BB_USER}:${BB_TOKEN}" \
            -X PUT "${BB_API}${path}"
    fi
}

bb_delete() {
    local path="$1"
    curl -sf -u "${BB_USER}:${BB_TOKEN}" -X DELETE "${BB_API}${path}"
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
        echo "Error: build_number must be a positive integer (got ${1!r:-empty})." >&2
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

    curl -sfL -u "${BB_USER}:${BB_TOKEN}" \
        "${BB_API}$(repo_path "$repo")/pipelines/%7B${pipeline_uuid}%7D/steps/%7B${step_uuid}%7D/log" \
        2>/dev/null || echo "(no log output available)"
}

cmd_pipeline_trigger() {
    local repo
    resolve_repo "${1:-}"
    local branch="${2:-}"
    local pattern="${3:-}"

    # `shift 3 || true` previously masked the under-3-args case:
    # bash leaves $@ unchanged when the shift count exceeds $#, so
    # `bb trigger myrepo` (1 arg) left "myrepo" in $@ and the
    # var-loop below parsed it as a VAR=VALUE pair, sending
    # {"key":"myrepo","value":"myrepo"} as a pipeline variable.
    # Guard explicitly.
    if [[ $# -ge 3 ]]; then
        shift 3
    else
        # Consume what's there; remaining $@ is empty.
        shift $#
    fi

    # Remaining args are VAR=VALUE pairs. Build the array via `jq`
    # so values containing `"`, `\`, newlines, or tabs are correctly
    # JSON-escaped. NUL delimiter (not newline) so values containing
    # newlines aren't fragmented into ghost vars — the previous
    # newline-split approach would turn VAR=$'line1\nline2' into a
    # real {VAR:line1} entry plus a ghost {line2:""} entry. jq's
    # split(" ") on a NUL-delimited stream sidesteps that.
    local variables="[]"
    if [[ $# -gt 0 ]]; then
        # Per-pair shape: split on the FIRST `=` only so values
        # containing `=` survive intact.
        variables=$(printf '%s\0' "$@" | jq -Rs '
            split(" ")
            | map(select(length > 0))
            | map(
                split("=") | {
                    key: .[0],
                    value: (.[1:] | join("="))
                }
            )
        ')
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
        # deploy creds) that the user passed as VAR=value. The previous
        # `Variables: $*` form leaked the full value into stdout /
        # terminal scrollback / CI logs / shell history.
        local masked
        masked=$(printf '%s\n' "$@" | sed -E 's/=.*$/=***/' | tr '\n' ' ')
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
        echo "Trigger request failed for ${BB_WORKSPACE}/${repo} branch ${branch} (exit $rc)." >&2
        echo "  Common causes: protected branch, custom pipeline name not" >&2
        echo "  found, or invalid variable shape." >&2
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
    # normal pre-enable state, reported as disabled) from a 403 (token
    # lacks read:pipeline:bitbucket) or any other error, which must be
    # surfaced. The Python side (bb_ops.pipelines_config_show) translates
    # ONLY a 404 and re-raises everything else. `bb_get` uses `curl -f`,
    # which collapses every HTTP >=400 to exit 22 with no body, so it can't
    # make that distinction. Do a single un-`-f`'d curl that writes the
    # body to stdout with the status code appended on its own line, then
    # split the two. One request (no re-probe), so there's no window where
    # the state changes between two calls. Mirrors the auth + base that
    # bb_get uses, so BB_API_BASE overrides still apply.
    local path raw code body rc=0
    path="$(repo_path "$repo")/pipelines_config"
    # `|| rc=$?` so a transport-level curl failure (DNS, TLS, connection
    # refused — NOT an HTTP error, since we don't pass -f) surfaces a
    # friendly message instead of tripping `set -e` and exiting silently.
    raw=$(curl -s -w '\n%{http_code}' \
        -u "${BB_USER}:${BB_TOKEN}" "${BB_API}${path}") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        echo "Could not read pipelines config (curl exit $rc)." >&2
        echo "  This looks like a connectivity error (not an HTTP response)." >&2
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
            echo "Could not read pipelines config (HTTP ${code})." >&2
            if [[ "$code" == "403" ]]; then
                echo "  The token lacks read:pipeline:bitbucket scope." >&2
            fi
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
        echo "Pipelines $([[ "$enabled" == "true" ]] && echo enable || echo disable) failed (exit $rc)." >&2
        echo "  Common cause: the token lacks admin:pipeline:bitbucket scope" >&2
        echo "  (write:pipeline:bitbucket alone is not enough)." >&2
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
    echo "$response" | jq -r '
        if (.reviewers | length) == 0 then "    (none)"
        else .reviewers[] | "    - " + .display_name
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
    local repo
    resolve_repo "${1:-}"
    local title="${2:-}"
    local dest="${3:-main}"

    local source_branch
    source_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)

    if [[ -z "$source_branch" || "$source_branch" == "HEAD" ]]; then
        echo "Error: Not on a branch. Check out a branch first." >&2
        exit 1
    fi

    if [[ -z "$title" ]]; then
        echo "Usage: bb pr-create [repo] <title> [dest-branch]" >&2
        echo "" >&2
        echo "  Creates a PR from current branch (${source_branch}) to dest (default: main)" >&2
        exit 1
    fi

    # Read description from stdin if piped, otherwise empty.
    local description=""
    if [[ ! -t 0 ]]; then
        description=$(cat)
    fi

    # Parity fix: omit `description` from the payload when empty so it
    # matches the Python omit-when-empty contract (bb_ops.pr_create).
    # close_source_branch hardcoded to true matches bb_ops's default
    # too; both surfaces could expose this as a flag in a future PR.
    local payload
    if [[ -n "$description" ]]; then
        payload=$(jq -n \
            --arg title "$title" \
            --arg desc "$description" \
            --arg src "$source_branch" \
            --arg dst "$dest" \
            '{
                title: $title,
                description: $desc,
                source: {branch: {name: $src}},
                destination: {branch: {name: $dst}},
                close_source_branch: true
            }')
    else
        payload=$(jq -n \
            --arg title "$title" \
            --arg src "$source_branch" \
            --arg dst "$dest" \
            '{
                title: $title,
                source: {branch: {name: $src}},
                destination: {branch: {name: $dst}},
                close_source_branch: true
            }')
    fi

    echo "Creating PR: ${source_branch} -> ${dest}"

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
        echo "PR-create request failed (exit $rc)." >&2
        echo "  Common causes: dest branch typo, a PR with this source" >&2
        echo "  branch is already open, source branch not pushed." >&2
        exit "$rc"
    fi

    local pr_id pr_url
    pr_id=$(echo "$response" | jq -r '.id')
    pr_url=$(echo "$response" | jq -r '.links.html.href')

    echo "Created PR #${pr_id}: ${title}"
    echo "  ${pr_url}"
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
    local repo pr_id pr_args_consumed
    _resolve_pr_args "$@"
    # Check usage BEFORE `shift $pr_args_consumed`. If the user passed a
    # single non-numeric arg (e.g. `bb pr-merge myrepo` with no id),
    # pr_args_consumed=2 but only 1 positional exists, so shift 2 returns
    # non-zero, which under `set -euo pipefail` aborts the script before
    # the friendly Usage message can print.
    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-merge [repo] <pr-id> [strategy]   (or: bb pr-merge <id> [strategy] from inside a checkout)" >&2
        echo "  Strategies: merge_commit (default), squash, fast_forward" >&2
        exit 1
    fi
    shift $pr_args_consumed
    local strategy="${1:-merge_commit}"

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
    payload=$(jq -n --arg strategy "$strategy" \
        '{type: "pullrequest", merge_strategy: $strategy, close_source_branch: true}')

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
        echo "Merge request failed for PR #${pr_id} (exit $rc)." >&2
        echo "  Common causes: unresolved comments, failing required" >&2
        echo "  builds, or wrong merge strategy for this repo." >&2
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
    curl -sfL -u "${BB_USER}:${BB_TOKEN}" \
        "${BB_API}$(repo_path "$repo")/pullrequests/${pr_id}/diff"
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
    # Requires `read:workspace:bitbucket` scope on the API token.
    # A token granted only repository/pullrequest/pipeline scopes
    # returns 403 — bb_get (`curl -sf`) exits non-zero WITHOUT printing
    # the body, so we can't echo Bitbucket's exact message; instead we
    # name the scope unconditionally on the error path so the user
    # knows the fix regardless.
    if [[ $# -gt 0 ]]; then
        echo "Usage: bb workspaces   (takes no arguments)" >&2
        exit 1
    fi

    echo "Workspaces accessible to ${BB_USER}:"
    echo ""

    # Capture rc via `|| rc=$?` rather than `if ! cmd; then rc=$?`.
    # The `!`-negation form sets $? to the LOGICAL NEGATION of the
    # command's status (always 0 for a failing command), so the real
    # curl exit code is unrecoverable inside an `if !` block — verified
    # on bash 3.2 and 5.x. curl -f exits 22 on an HTTP >=400 response;
    # other codes are transport-level (DNS, connection, TLS).
    local response rc=0
    response=$(bb_get "/user/workspaces?pagelen=100") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        echo "Workspace listing failed (exit $rc)." >&2
        if [[ "$rc" -eq 22 ]]; then
            echo "If this is a 403, the token lacks the read:workspace:bitbucket" >&2
            echo "scope. Rotate it at" >&2
            echo "https://id.atlassian.com/manage-profile/security/api-tokens" >&2
            echo "with that scope checked (existing scopes stay as they are)." >&2
        else
            echo "This looks like a connectivity error (not an HTTP response)." >&2
            echo "Check your network and that api.bitbucket.org is reachable." >&2
        fi
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
    # token without it returns 403; bb_get (`curl -sf`) exits non-zero
    # WITHOUT printing the body, so we name the scope on the error path
    # so the user knows the fix regardless.
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

    # Capture rc via `|| rc=$?` (not `if ! cmd`) so curl's real exit code
    # survives — same idiom as cmd_workspaces. curl -f exits 22 on HTTP
    # >=400; other codes are transport-level.
    local response rc=0
    response=$(bb_get "/workspaces/${BB_WORKSPACE}/projects?pagelen=100") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        echo "Project listing failed (exit $rc)." >&2
        if [[ "$rc" -eq 22 ]]; then
            echo "If this is a 403, the token lacks the read:project:bitbucket" >&2
            echo "scope. Rotate it at" >&2
            echo "https://id.atlassian.com/manage-profile/security/api-tokens" >&2
            echo "with that scope checked (existing scopes stay as they are)." >&2
        else
            echo "This looks like a connectivity error (not an HTTP response)." >&2
            echo "Check your network and that api.bitbucket.org is reachable." >&2
        fi
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

cmd_repos() {
    resolve_workspace
    echo "Repositories in ${BB_WORKSPACE}:"
    echo ""

    local response
    response=$(bb_get "/repositories/${BB_WORKSPACE}?pagelen=100&sort=-updated_on")

    printf "  %-35s %-12s %s\n" "REPO" "UPDATED" "LANGUAGE"
    printf "  %-35s %-12s %s\n" "----" "-------" "--------"

    echo "$response" | jq -r '
        .values[] |
        [
            .slug,
            (.updated_on | split("T") | .[0]),
            (.language // "-")
        ] | @tsv
    ' | while IFS=$'\t' read -r slug updated lang; do
        printf "  %-35s %-12s %s\n" "$slug" "$updated" "$lang"
    done
}

cmd_repo() {
    local repo
    resolve_repo "${1:-}"

    local response
    response=$(bb_get "$(repo_path "$repo")")

    echo "$response" | jq -r '
        .full_name + " - " + (.description // "(no description)"),
        "",
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
        echo "Repo-create request failed (exit $rc)." >&2
        echo "  Common causes: a repo with this slug already exists, the" >&2
        echo "  workspace requires a --project KEY, or the token lacks" >&2
        echo "  repository:admin scope." >&2
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
        echo "Repo-update request failed (exit $rc)." >&2
        echo "  Common causes: the target --project KEY doesn't exist in" >&2
        echo "  the workspace, the repo slug is wrong, or the token lacks" >&2
        echo "  admin:repository:bitbucket scope (write alone is not enough)." >&2
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
        echo "Environment create failed (exit $rc)." >&2
        echo "  Common cause: a same-named environment already exists, or the" >&2
        echo "  token lacks admin:pipeline:bitbucket scope." >&2
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
        echo "Environment delete failed for '$name' (exit $rc)." >&2
        echo "  The token may lack admin:pipeline:bitbucket scope." >&2
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

    # rc-capture so a 5xx / expired-token on the list surfaces a curated
    # error instead of `set -e` aborting with only curl's stderr (parity
    # with cmd_vars_set's lookup-failure handling).
    local response rc=0
    response=$(bb_get "${base}?pagelen=100") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        echo "vars list failed (exit $rc). Check token scope / connectivity." >&2
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
            echo "vars-set lookup failed (exit $rc) while checking for an" >&2
            echo "  existing '${key}'. Aborting before any write to avoid" >&2
            echo "  creating a duplicate. Check token scope / connectivity." >&2
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
        echo "vars-set request failed (exit $rc)." >&2
        echo "  Common causes: token lacks admin:pipeline:bitbucket scope" >&2
        echo "  (write:pipeline alone is not enough for any variable scope)," >&2
        echo "  or the repo has no pipelines configuration yet." >&2
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
            echo "vars-delete lookup failed (exit $rc) while resolving '${key}'." >&2
            echo "  Aborting before any delete. The token may lack" >&2
            echo "  admin:pipeline:bitbucket scope (write:pipeline alone is not" >&2
            echo "  enough for any variable scope), or check connectivity." >&2
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
        echo "vars-delete request failed (exit $rc)." >&2
        echo "  The token may lack admin:pipeline:bitbucket scope" >&2
        echo "  (write:pipeline alone is not enough for any variable scope)." >&2
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
  bb trigger [repo] [branch] [pattern]  Trigger a pipeline run
  bb stop [repo] <number>               Stop a running pipeline
  bb approve [repo] <number>            Open pipeline in browser (manual steps require UI)
  bb pipelines-status [repo]            Show whether Pipelines (CI) is enabled
  bb pipelines-enable [repo]            Enable Pipelines (CI) on a repo
  bb pipelines-disable [repo]           Disable Pipelines (CI) on a repo
                                          (enable/disable need admin:pipeline:bitbucket)

PULL REQUESTS
  bb prs [repo] [state]                 List PRs (default: OPEN)
  bb pr [repo] <id>                     View PR details
  bb pr-create [repo] <title> [dest]    Create PR from current branch
  bb pr-approve [repo] <id>             Approve a PR
  bb pr-unapprove [repo] <id>           Remove your approval on a PR
  bb pr-merge [repo] <id> [strategy]    Merge a PR (merge_commit|squash|fast_forward)
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
