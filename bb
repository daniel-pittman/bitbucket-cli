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
    if [[ -f "$HOME/.config/bb/config" ]]; then
        # shellcheck source=/dev/null
        source "$HOME/.config/bb/config"
    fi
    if [[ -f "$SCRIPT_DIR/.env" ]]; then
        # shellcheck source=/dev/null
        source "$SCRIPT_DIR/.env"
    fi

    if [[ -z "${BB_USER:-}" || -z "${BB_TOKEN:-}" || -z "${BB_WORKSPACE:-}" ]]; then
        echo "Error: BB_USER, BB_TOKEN, and BB_WORKSPACE must be set." >&2
        echo "" >&2
        echo "Quick setup:" >&2
        echo "  mkdir -p ~/.config/bb" >&2
        echo "  cat > ~/.config/bb/config <<EOF" >&2
        echo "BB_USER=your-email@example.com" >&2
        echo "BB_TOKEN=your-api-token" >&2
        echo "BB_WORKSPACE=your-workspace" >&2
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

# Detect repo from current git remote if not provided
detect_repo() {
    if [[ -n "${1:-}" ]]; then
        echo "$1"
        return
    fi
    local remote_url
    remote_url=$(git remote get-url origin 2>/dev/null || true)
    if [[ -z "$remote_url" ]]; then
        echo "Error: No repo specified and not in a git repository." >&2
        exit 1
    fi
    echo "$remote_url" | sed -E 's#.*[:/]([^/]+)/([^/.]+)(\.git)?$#\2#'
}

repo_path() {
    local repo="$1"
    echo "/repositories/${BB_WORKSPACE}/${repo}"
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
    repo=$(detect_repo "${1:-}")
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
    repo=$(detect_repo "${1:-}")
    local build_number="${2:-}"

    if [[ -z "$build_number" ]]; then
        echo "Usage: bb pipeline [repo] <build-number>" >&2
        exit 1
    fi

    local response
    response=$(bb_get "$(repo_path "$repo")/pipelines/?sort=-created_on&pagelen=50")

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
    repo=$(detect_repo "${1:-}")
    local build_number="${2:-}"
    local poll_interval="${3:-15}"

    if [[ -z "$build_number" ]]; then
        local latest
        latest=$(bb_get "$(repo_path "$repo")/pipelines/?sort=-created_on&pagelen=1")
        build_number=$(echo "$latest" | jq -r '.values[0].build_number')
        echo "Watching most recent pipeline: #${build_number}"
    fi

    echo "Watching pipeline #${build_number} on ${BB_WORKSPACE}/${repo} (every ${poll_interval}s)..."
    echo ""

    while true; do
        local response
        response=$(bb_get "$(repo_path "$repo")/pipelines/?sort=-created_on&pagelen=50")

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
    repo=$(detect_repo "${1:-}")
    local build_number="${2:-}"
    local step_index="${3:-}"

    if [[ -z "$build_number" ]]; then
        echo "Usage: bb logs [repo] <build-number> [step-index]" >&2
        exit 1
    fi

    local response
    response=$(bb_get "$(repo_path "$repo")/pipelines/?sort=-created_on&pagelen=50")

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

    curl -sf -u "${BB_USER}:${BB_TOKEN}" \
        "${BB_API}$(repo_path "$repo")/pipelines/%7B${pipeline_uuid}%7D/steps/%7B${step_uuid}%7D/log" \
        2>/dev/null || echo "(no log output available)"
}

cmd_pipeline_trigger() {
    local repo
    repo=$(detect_repo "${1:-}")
    local branch="${2:-}"
    local pattern="${3:-}"
    shift 3 2>/dev/null || true

    # Remaining args are VAR=VALUE pairs
    local variables="[]"
    if [[ $# -gt 0 ]]; then
        variables="["
        local first=true
        for var_pair in "$@"; do
            local key="${var_pair%%=*}"
            local value="${var_pair#*=}"
            if [[ "$first" == "true" ]]; then
                first=false
            else
                variables+=","
            fi
            variables+="{\"key\":\"${key}\",\"value\":\"${value}\"}"
        done
        variables+="]"
    fi

    if [[ -z "$branch" ]]; then
        branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
    fi

    local payload
    if [[ -n "$pattern" ]]; then
        payload=$(jq -n \
            --arg ref "$branch" \
            --arg pat "$pattern" \
            --argjson vars "$variables" \
            '{target: {ref_name: $ref, ref_type: "branch", selector: {type: "custom", pattern: $pat}}, variables: $vars}')
    else
        payload=$(jq -n --arg ref "$branch" '{target: {ref_name: $ref, ref_type: "branch"}}')
    fi

    echo "Triggering pipeline on ${BB_WORKSPACE}/${repo} branch ${branch}..."
    if [[ -n "$pattern" ]]; then
        echo "  Custom pipeline: ${pattern}"
    fi
    if [[ "$variables" != "[]" ]]; then
        echo "  Variables: $*"
    fi

    local response
    response=$(bb_post "$(repo_path "$repo")/pipelines/" "$payload")

    local build_num
    build_num=$(echo "$response" | jq -r '.build_number')
    echo "Started pipeline #${build_num}"
    echo ""
    echo "Watch with: bb watch ${repo} ${build_num}"
}

cmd_pipeline_stop() {
    local repo
    repo=$(detect_repo "${1:-}")
    local build_number="${2:-}"

    if [[ -z "$build_number" ]]; then
        echo "Usage: bb pipeline-stop [repo] <build-number>" >&2
        exit 1
    fi

    local response
    response=$(bb_get "$(repo_path "$repo")/pipelines/?sort=-created_on&pagelen=50")

    local pipeline_uuid
    pipeline_uuid=$(echo "$response" | jq -r ".values[] | select(.build_number == ${build_number}) | .uuid" | tr -d '{}')

    if [[ -z "$pipeline_uuid" ]]; then
        echo "Pipeline #${build_number} not found." >&2
        exit 1
    fi

    bb_post "$(repo_path "$repo")/pipelines/%7B${pipeline_uuid}%7D/stopPipeline" > /dev/null
    echo "Stopped pipeline #${build_number}"
}

cmd_pipeline_approve() {
    local repo
    repo=$(detect_repo "${1:-}")
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

# =========================================================================
#  PULL REQUEST COMMANDS
# =========================================================================

cmd_pr_list() {
    local repo
    repo=$(detect_repo "${1:-}")
    local state="${2:-OPEN}"

    echo "Pull requests for ${BB_WORKSPACE}/${repo} [${state}]:"
    echo ""

    local response
    response=$(bb_get "$(repo_path "$repo")/pullrequests?state=${state}&pagelen=25")

    local count
    count=$(echo "$response" | jq '.size')

    if [[ "$count" == "0" ]]; then
        echo "  No ${state,,} pull requests."
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
    local repo
    repo=$(detect_repo "${1:-}")
    local pr_id="${2:-}"

    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-view [repo] <pr-id>" >&2
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
    repo=$(detect_repo "${1:-}")
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

    # Read description from stdin if piped, otherwise empty
    local description=""
    if [[ ! -t 0 ]]; then
        description=$(cat)
    fi

    local payload
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

    echo "Creating PR: ${source_branch} -> ${dest}"
    local response
    response=$(bb_post "$(repo_path "$repo")/pullrequests" "$payload")

    local pr_id pr_url
    pr_id=$(echo "$response" | jq -r '.id')
    pr_url=$(echo "$response" | jq -r '.links.html.href')

    echo "Created PR #${pr_id}: ${title}"
    echo "  ${pr_url}"
}

cmd_pr_approve() {
    local repo
    repo=$(detect_repo "${1:-}")
    local pr_id="${2:-}"

    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-approve [repo] <pr-id>" >&2
        exit 1
    fi

    bb_post "$(repo_path "$repo")/pullrequests/${pr_id}/approve" > /dev/null
    echo "Approved PR #${pr_id}"
}

cmd_pr_merge() {
    local repo
    repo=$(detect_repo "${1:-}")
    local pr_id="${2:-}"
    local strategy="${3:-merge_commit}"

    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-merge [repo] <pr-id> [strategy]" >&2
        echo "  Strategies: merge_commit (default), squash, fast_forward" >&2
        exit 1
    fi

    local payload
    payload=$(jq -n --arg strategy "$strategy" \
        '{type: "pullrequest", merge_strategy: $strategy, close_source_branch: true}')

    local response
    response=$(bb_put "$(repo_path "$repo")/pullrequests/${pr_id}/merge" "$payload")

    echo "Merged PR #${pr_id} (${strategy})"
}

cmd_pr_decline() {
    local repo
    repo=$(detect_repo "${1:-}")
    local pr_id="${2:-}"

    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-decline [repo] <pr-id>" >&2
        exit 1
    fi

    bb_post "$(repo_path "$repo")/pullrequests/${pr_id}/decline" > /dev/null
    echo "Declined PR #${pr_id}"
}

cmd_pr_diff() {
    local repo
    repo=$(detect_repo "${1:-}")
    local pr_id="${2:-}"

    if [[ -z "$pr_id" ]]; then
        echo "Usage: bb pr-diff [repo] <pr-id>" >&2
        exit 1
    fi

    curl -sf -u "${BB_USER}:${BB_TOKEN}" \
        "${BB_API}$(repo_path "$repo")/pullrequests/${pr_id}/diff"
}

cmd_pr_comments() {
    local repo
    repo=$(detect_repo "${1:-}")
    local pr_id="${2:-}"

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

# =========================================================================
#  BRANCH COMMANDS
# =========================================================================

cmd_branches() {
    local repo
    repo=$(detect_repo "${1:-}")

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

# =========================================================================
#  REPOSITORY COMMANDS
# =========================================================================

cmd_repos() {
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
    repo=$(detect_repo "${1:-}")

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

# =========================================================================
#  DOWNLOADS (deployment artifacts)
# =========================================================================

cmd_downloads() {
    local repo
    repo=$(detect_repo "${1:-}")

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
#  ENVIRONMENT / DEPLOY VARIABLES
# =========================================================================

cmd_vars() {
    local repo
    repo=$(detect_repo "${1:-}")

    echo "Repository variables for ${BB_WORKSPACE}/${repo}:"
    echo ""

    local response
    response=$(bb_get "$(repo_path "$repo")/pipelines_config/variables/?pagelen=100")

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

# =========================================================================
#  OPEN IN BROWSER
# =========================================================================

cmd_open() {
    local repo
    repo=$(detect_repo "${1:-}")
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
  bb approve [repo] <number>             Open pipeline in browser (manual steps require UI)

PULL REQUESTS
  bb prs [repo] [state]                 List PRs (default: OPEN)
  bb pr [repo] <id>                     View PR details
  bb pr-create [repo] <title> [dest]    Create PR from current branch
  bb pr-approve [repo] <id>             Approve a PR
  bb pr-merge [repo] <id> [strategy]    Merge a PR (merge_commit|squash|fast_forward)
  bb pr-decline [repo] <id>             Decline a PR
  bb pr-diff [repo] <id>                Show PR diff
  bb pr-comments [repo] <id>            Show PR comments

BRANCHES
  bb branches [repo]                    List branches

REPOSITORY
  bb repos                              List workspace repos
  bb repo [repo]                        Show repo details
  bb downloads [repo]                   List repo downloads
  bb vars [repo]                        List pipeline variables

UTILITIES
  bb open [repo] [section]              Open in browser (pr|pipelines|branches|settings|commits)
  bb help                               Show this help

GLOBAL FLAGS
  -w, --workspace <name>                Override workspace (default: dreamfacesbir)

  Example: bb -w other-workspace repos

NOTES
  [repo] is auto-detected from git remote if omitted.
  Config: ~/.config/bb/config or env vars BB_USER, BB_TOKEN, BB_WORKSPACE

  Auth uses Atlassian API tokens with HTTP Basic auth.
  BB_USER is your Bitbucket account email address.
  Create a token at: https://id.atlassian.com/manage-profile/security/api-tokens
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
    # Pull Requests
    prs|pr-list)          cmd_pr_list "$@" ;;
    pr|pr-view)           cmd_pr_view "$@" ;;
    pr-create|prc)        cmd_pr_create "$@" ;;
    pr-approve|pra)       cmd_pr_approve "$@" ;;
    pr-merge|prm)         cmd_pr_merge "$@" ;;
    pr-decline|prd)       cmd_pr_decline "$@" ;;
    pr-diff)              cmd_pr_diff "$@" ;;
    pr-comments|pr-comm)  cmd_pr_comments "$@" ;;
    # Branches
    branches|br)          cmd_branches "$@" ;;
    # Repos
    repos)                cmd_repos "$@" ;;
    repo)                 cmd_repo "$@" ;;
    downloads|dl)         cmd_downloads "$@" ;;
    vars)                 cmd_vars "$@" ;;
    # Utilities
    open|o)               cmd_open "$@" ;;
    help|--help|-h)       cmd_help ;;
    *)
        echo "Unknown command: $command" >&2
        echo "Run 'bb help' for usage." >&2
        exit 1
        ;;
esac
