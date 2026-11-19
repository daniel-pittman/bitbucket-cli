#!/usr/bin/env bash
#
# Security-guard regression harness for the bb bash CLI.
#
# The _require_* guards (build_number / pr_id / step_index / count /
# pr_state) are SECURITY boundaries: without them, user-supplied values
# reach a jq program or a request URL and become injection surfaces. The
# worst is step_index, which is interpolated into a jq program — an
# unvalidated value can make jq emit $ENV.BB_TOKEN (v1.7.1 fix). Those
# guards live only in bash and were previously verified only by hand, so
# a refactor that dropped a guard call would re-open the hole silently.
# This harness asserts each guard rejects its injection vector.
#
# Offline by design: every MALICIOUS case is rejected BEFORE any network
# call, so it needs no live workspace and invokes no jq / curl / git —
# that "zero network IO on bad input" property is itself worth pinning
# and is what lets the harness run in the minimal bash:3.2 CI image. (The
# valid-input assert_passes_guard cases deliberately proceed past the
# guard into bb_get, which invokes curl against the unrouted host; that
# call failing is expected, and the assertion holds whether or not curl
# is present.) To catch a regression that lets input slip past a guard to
# the network, assertions match the SPECIFIC guard message, not just a
# non-zero exit (a network failure also exits non-zero, so exit-code
# alone would false-pass).
#
# bash 3.2-safe (runs in the bash:3.2 CI container): no ${var,,},
# mapfile, or declare -A.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
BB="${HERE}/../../bb"

# Dummy config so load_config passes; a sentinel token so the step_index
# leak check has something unmistakable to look for. BB_API_BASE points
# at an unrouted port so any accidental network call fails immediately
# rather than hanging or hitting a real host.
export BB_USER="ci@example.com"
export BB_TOKEN="SENTINEL_TOKEN_MUST_NOT_LEAK"
export BB_WORKSPACE="dummy-ws"
export BB_API_BASE="http://127.0.0.1:9/2.0"

REPO="dummy-ws/dummy-repo"   # explicit ws/repo → no git auto-detect

pass=0
fail=0

# assert_rejects <label> <expected-stderr-substring> -- <bb args...>
assert_rejects() {
    local label="$1" needle="$2"
    shift 2
    [ "$1" = "--" ] && shift
    local out rc
    out="$("$BB" "$@" 2>&1)"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "FAIL: ${label} — expected non-zero exit, got 0"
        echo "      output: ${out}"
        fail=$((fail + 1))
        return
    fi
    case "$out" in
        *"$needle"*)
            echo "pass: ${label}"
            pass=$((pass + 1))
            ;;
        *)
            echo "FAIL: ${label} — guard message '${needle}' not found"
            echo "      output: ${out}"
            fail=$((fail + 1))
            ;;
    esac
}

# Note on end-to-end non-leak: proving the step_index injection actually
# CANNOT emit $ENV.BB_TOKEN through jq requires a mock server that returns
# a real pipeline+steps so the injection reaches jq when the guard is
# absent. In this offline harness the guard rejects before any network
# call, so a token-leak assertion could never fail here (a removed guard
# just fails at the unrouted network instead) — it would be a test that
# cannot fail, which is worse than no test. The "guard message present"
# assertion below IS the regression detector: remove _require_step_index
# and the step_index case fails. The mock-server end-to-end variant is
# tracked as follow-up.

# assert_passes_guard <label> <guard-substring> -- <bb args...>
# Valid input must get PAST the guard: the command proceeds to the
# network (which fails against the unrouted host, that's fine) and the
# guard's rejection message must be ABSENT. Catches a guard that falsely
# rejects legitimate input.
assert_passes_guard() {
    local label="$1" guardmsg="$2"
    shift 2
    [ "$1" = "--" ] && shift
    local out
    out="$("$BB" "$@" 2>&1)"
    case "$out" in
        *"$guardmsg"*)
            echo "FAIL: ${label} — valid input wrongly rejected by guard"
            echo "      output: ${out}"
            fail=$((fail + 1))
            ;;
        *)
            echo "pass: ${label} (valid input cleared the guard)"
            pass=$((pass + 1))
            ;;
    esac
}

echo "== injection vectors must be rejected =="
# step_index → jq program injection / token exfiltration (the v1.7.1 HIGH)
assert_rejects "logs step_index jq-injection" \
    "step-index must be a non-negative integer" \
    -- logs "$REPO" 42 '0].uuid,$ENV.BB_TOKEN,.values[0'

# pr_id → URL path manipulation on mutation endpoints
assert_rejects "pr-merge pr_id path-manip" \
    "pr-id must be a positive integer" \
    -- pr-merge "$REPO" '1/../../foo'
assert_rejects "pr-approve pr_id path-manip" \
    "pr-id must be a positive integer" \
    -- pr-approve "$REPO" '../../other'

# count → query-param injection
assert_rejects "pipelines count query-injection" \
    "count must be a positive integer" \
    -- pipelines "$REPO" '10&role=admin'
assert_rejects "commits count query-injection" \
    "count must be a positive integer" \
    -- commits "$REPO" main '5&fields=x'

# state → query-param injection (the earlier _require_pr_state fix)
assert_rejects "prs state query-injection" \
    "state must be one of" \
    -- prs "$REPO" 'OPEN&pagelen=1000'

# build_number → jq program injection (the PR #8 fix)
assert_rejects "logs build_number non-numeric" \
    "build_number must be a positive integer" \
    -- logs "$REPO" 'abc'

# repo / workspace slug → path rewrite or query smuggling (issue #69).
# A `?` or `#` turns the rest of the request path into a query string or
# fragment; a space or control character makes the URL invalid.
SLUG_MSG="must contain only letters, digits, '.', '_' and '-'"
assert_rejects "repo slug '?' query-smuggle" "$SLUG_MSG" \
    -- pipelines 'dummy-ws/widget?x=1'
assert_rejects "repo slug '#' fragment" "$SLUG_MSG" \
    -- prs 'dummy-ws/widget#frag'
assert_rejects "repo slug percent-escape" "$SLUG_MSG" \
    -- pipelines 'dummy-ws/widget%2Fother'
assert_rejects "workspace slug '?' via ws/repo" "$SLUG_MSG" \
    -- pipelines 'ws?x=1/widget'
assert_rejects "workspace slug control char" "$SLUG_MSG" \
    -- pipelines "$(printf 'ws\001x')/widget"
assert_rejects "projects workspace '?'" "$SLUG_MSG" \
    -- -w 'ws?x=1' projects
assert_rejects "members workspace '#'" "$SLUG_MSG" \
    -- -w 'ws#x' members
# Inner whitespace: the long-standing whitespace check catches it first.
assert_rejects "repo slug inner space" "non-whitespace" \
    -- pipelines 'dummy-ws/bad repo'
# Workspace-level builders that bypass repo_path (review of #69).
assert_rejects "repos workspace '?'" "$SLUG_MSG" \
    -- -w 'acme?x=1' repos
assert_rejects "repos workspace '..'" "must not be '.' or '..'" \
    -- -w '..' repos
assert_rejects "vars --workspace '#'" "invalid workspace for workspace-scope" \
    -- -w 'acme#f' vars --workspace
assert_rejects "vars set --workspace '?'" "invalid workspace for workspace-scope" \
    -- -w 'acme?x=1' vars set --workspace SOME_KEY --value v
assert_rejects "vars --workspace inner space" "invalid workspace for workspace-scope" \
    -- -w 'bad ws' vars --workspace
# Browser URLs go through the same slug checks.
assert_rejects "open repo '?'" "$SLUG_MSG" \
    -- open 'dummy-ws/widget?x=1'
assert_rejects "approve workspace '#'" "$SLUG_MSG" \
    -- approve 'ws#x/widget' 1

# whoami must not probe an invalid workspace (`..` reaches the global
# repositories listing and would report a false "auth OK").
for bad_ws in '..' 'acme?x=1'; do
    out="$("$BB" -w "$bad_ws" whoami 2>&1)"
    if printf '%s' "$out" | grep -q "is not a valid slug"; then
        echo "pass: whoami skips probe for '${bad_ws}'"; pass=$((pass + 1))
    else
        echo "FAIL: whoami probed invalid workspace '${bad_ws}'"
        echo "      output: ${out}"; fail=$((fail + 1))
    fi
done

# _slug_chars_ok itself: `$(...)` strips trailing newlines, so a value
# whose only bad bytes are newlines must still fail.
eval "$(sed -n '/^_slug_chars_ok() {/,/^}/p' "$BB")"
for v in $'acme\n' $'a\nb' $'\n'; do
    if _slug_chars_ok "$v"; then
        echo "FAIL: _slug_chars_ok accepted a newline-bearing value"; fail=$((fail + 1))
    else
        echo "pass: _slug_chars_ok rejects newline-bearing value"; pass=$((pass + 1))
    fi
done
if _slug_chars_ok 'Widget.Service_2-x'; then
    echo "pass: _slug_chars_ok accepts a valid slug"; pass=$((pass + 1))
else
    echo "FAIL: _slug_chars_ok rejected a valid slug"; fail=$((fail + 1))
fi

echo ""
echo "== valid input must clear the guards =="
assert_passes_guard "logs valid step 0 clears guard" \
    "step-index must be" -- logs "$REPO" 42 0
assert_passes_guard "pr-merge valid id clears guard" \
    "pr-id must be" -- pr-merge "$REPO" 1
assert_passes_guard "pipelines valid count clears guard" \
    "count must be" -- pipelines "$REPO" 3
assert_passes_guard "prs valid state clears guard" \
    "state must be one of" -- prs "$REPO" MERGED
assert_passes_guard "slug with . _ - and caps clears guard" \
    "must contain only letters" -- pipelines 'My_ws-1/Widget.Service_2'
assert_passes_guard "projects valid workspace clears guard" \
    "must contain only letters" -- -w 'acme-co_2' projects

echo ""
echo "== summary: ${pass} passed, ${fail} failed =="
[ "$fail" -eq 0 ]
