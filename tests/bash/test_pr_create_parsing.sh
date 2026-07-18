#!/usr/bin/env bash
#
# Regression harness for `bb pr-create` argument parsing and the
# stdin-read no-hang guarantee.
#
# Two defects motivated this harness:
#
#   1. HANG (HIGH). pr-create read its description with `cat` whenever
#      stdin was not a tty (`[[ ! -t 0 ]]`). Run non-interactively with no
#      controlling tty and no piped data (e.g. under an agent's shell),
#      `cat` blocked forever waiting for an EOF that never came, orphaning
#      the process. The fix reads stdin ONLY for a `< body.md` regular-file
#      redirect (which always reaches EOF); pipes / char devices / ttys are
#      never auto-read. This harness reproduces the exact hang condition (an
#      open pipe with no data and no EOF) behind a watchdog: a regression
#      that reintroduces the blocking `cat` times out here (RC 124) instead
#      of hanging a real invocation.
#
#   2. WRONG-ARG (MEDIUM). The leading `[repo]` positional was optional and
#      greedy, so `bb pr-create "<title>" <dest>` mis-read the title as the
#      repo. The fix makes <title> the first positional and takes the repo
#      from --repo (or a leading unambiguous "ws/slug" positional for
#      back-compat). These assertions pin the corrected parse.
#
# Unlike the security-guard harness this one exercises the happy path up to
# (but not through) the network, so it needs git + jq on PATH and runs on a
# normal runner rather than the jq/curl/git-less bash:3.2 image. It never
# reaches a real Bitbucket host: BB_API_BASE points at an unrouted port, so
# the post fails fast AFTER the observable "Creating PR:" banner prints.
#
# bash 3.2-safe (no ${var,,}, mapfile, or declare -A).

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
BB="${HERE}/../../bb"

# Config so load_config passes. BB_API_BASE is an unrouted port: any network
# call fails immediately rather than hanging or reaching a real host. The
# git-origin auto-detect (below) overrides BB_WORKSPACE for the no-repo case.
export BB_USER="ci@example.com"
export BB_TOKEN="SENTINEL_TOKEN_MUST_NOT_LEAK"
export BB_WORKSPACE="config-default-ws"
export BB_API_BASE="http://127.0.0.1:9/2.0"

passes=0
fails=0

ok()      { echo "pass: $1"; passes=$((passes + 1)); }
failmsg() {
    echo "FAIL: $1 — $2"
    echo "      rc=${RC:-?}"
    echo "      output: ${OUT}"
    fails=$((fails + 1))
}

check_contains()     { case "$OUT" in *"$2"*) ok "$1";; *) failmsg "$1" "expected to contain: $2";; esac; }
check_not_contains() { case "$OUT" in *"$2"*) failmsg "$1" "should NOT contain: $2";; *) ok "$1";; esac; }
check_rc_nonzero()   { if [ "${RC}" -ne 0 ]; then ok "$1"; else failmsg "$1" "expected non-zero exit"; fi; }
check_not_hung()     { if [ "${RC}" -ne 124 ]; then ok "$1"; else failmsg "$1" "HUNG — watchdog timed out on stdin read"; fi; }

# capture -- <bb args...>  → sets OUT, RC. stdin is /dev/null (immediate EOF),
# so a correctly-behaving pr-create never blocks; a regression that reads a
# non-tty stdin unconditionally still wouldn't block on /dev/null, which is
# why the dedicated no-hang test below uses an open pipe instead.
capture() {
    [ "$1" = "--" ] && shift
    OUT="$("$BB" "$@" </dev/null 2>&1)"
    RC=$?
}

# capture_stdin <file> -- <bb args...>  → sets OUT, RC with stdin from <file>.
capture_stdin() {
    local src="$1"; shift
    [ "$1" = "--" ] && shift
    OUT="$("$BB" "$@" <"$src" 2>&1)"
    RC=$?
}

# watchdog_capture <secs> <stdin_src> -- <bb args...> → OUT, RC (124 == hang).
# Runs bb in the background reading from <stdin_src>; if it is still alive
# after <secs> it is killed and RC is forced to 124 (the "it hung" verdict).
watchdog_capture() {
    local secs="$1" src="$2"; shift 2
    [ "$1" = "--" ] && shift
    local tmpout; tmpout="$(mktemp)"
    "$BB" "$@" <"$src" >"$tmpout" 2>&1 &
    local pid=$! waited=0
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        waited=$((waited + 1))
        if [ "$waited" -ge "$secs" ]; then
            kill -TERM "$pid" 2>/dev/null
            sleep 1
            kill -KILL "$pid" 2>/dev/null
            wait "$pid" 2>/dev/null
            OUT="$(cat "$tmpout")"; RC=124; rm -f "$tmpout"; return
        fi
    done
    wait "$pid"; RC=$?
    OUT="$(cat "$tmpout")"; rm -f "$tmpout"
}

# --- Hermetic git checkout: a fake Bitbucket origin so the no-repo
#     auto-detect resolves deterministically (workspace=acme, repo=widget)
#     and `git rev-parse --abbrev-ref HEAD` yields a known source branch.
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

git init -q -b work-branch "$WORK/repo" 2>/dev/null || {
    git init -q "$WORK/repo"
    git -C "$WORK/repo" checkout -q -b work-branch
}
git -C "$WORK/repo" config user.email "ci@example.com"
git -C "$WORK/repo" config user.name  "CI"
git -C "$WORK/repo" commit -q --allow-empty -m init
git -C "$WORK/repo" remote add origin "git@bitbucket.org:acme/widget.git"
cd "$WORK/repo"

echo "== BUG 2: <title> is the first positional, dest the second =="

# 2-positional, repo omitted: title="Add widget cache", dest=develop, repo
# auto-detected. The old greedy [repo] positional mis-read the title as the
# repo (a whitespace repo → the "non-empty, non-whitespace" boundary error).
capture -- pr-create "Add widget cache" develop
check_contains     "no-repo 2-arg: dest parsed from 2nd positional" "-> develop"
check_contains     "no-repo 2-arg: title parsed from 1st positional" "Title: Add widget cache"
check_not_contains "no-repo 2-arg: title not mis-read as repo"       "non-empty, non-whitespace"

# 1-positional: title only, dest defaults to main.
capture -- pr-create "Only a title"
check_contains "no-repo 1-arg: default dest is main" "-> main"
check_contains "no-repo 1-arg: title parsed"         "Title: Only a title"

# Back-compat: a leading unambiguous "ws/slug" positional is still a repo.
capture -- pr-create acme/widget "Two words here" develop
check_contains "ws/slug positional: dest parsed"  "-> develop"
check_contains "ws/slug positional: title is 2nd positional, not the repo" "Title: Two words here"

# --repo flag (space form) selects the repo; positionals are title/dest.
capture -- pr-create --repo acme/widget "Flag repo title" develop
check_contains "--repo flag: dest parsed"  "-> develop"
check_contains "--repo flag: title parsed" "Title: Flag repo title"

# --repo=value form.
capture -- pr-create --repo=acme/widget "Eq repo title" develop
check_contains "--repo=value: dest parsed"  "-> develop"
check_contains "--repo=value: title parsed" "Title: Eq repo title"

echo ""
echo "== BUG 1: stdin read must never hang =="

if command -v mkfifo >/dev/null 2>&1; then
    # Reproduce the exact hang: an open pipe with no data and no EOF. A
    # background `sleep` holds the write end open (produces nothing, never
    # closes) so a reader blocks indefinitely — precisely the tty-less,
    # no-piped-data condition that orphaned pr-create. The fix skips the
    # read (a pipe is not a regular file), so bb reaches the banner and
    # exits well within the watchdog window.
    FIFO="$(mktemp -u)"
    mkfifo "$FIFO"
    sleep 20 >"$FIFO" &
    HOLDER=$!
    watchdog_capture 4 "$FIFO" -- pr-create --repo acme/widget "No hang please" develop
    kill "$HOLDER" 2>/dev/null
    wait "$HOLDER" 2>/dev/null
    rm -f "$FIFO"
    check_not_hung "open-pipe stdin: pr-create does not block on cat"
    check_contains "open-pipe stdin: reached the create banner"        "Creating PR:"
else
    echo "pass: (skipped) mkfifo unavailable — no-hang pipe test not run"
    passes=$((passes + 1))
fi

echo ""
echo "== description sources =="

# --description and --description-file are mutually exclusive.
capture -- pr-create --repo acme/widget "t" develop --description "x" --description-file /tmp/nope
check_rc_nonzero "desc + desc-file rejected"
check_contains   "desc + desc-file mutual-exclusion message" "mutually exclusive"

# --description-file must exist; the failure is local, before any network.
capture -- pr-create --repo acme/widget "t" develop --description-file /no/such/file.md
check_rc_nonzero "missing desc-file rejected"
check_contains   "missing desc-file message"        "--description-file not found"

# A real --description-file is read and the command proceeds to the banner.
BODY="$WORK/body.md"
printf 'line one\nline two\n' >"$BODY"
capture -- pr-create --repo acme/widget "With body" develop --description-file "$BODY"
check_contains     "desc-file read: reached banner"  "Creating PR:"
check_not_contains "desc-file read: no not-found err" "not found"

# A `< body.md` regular-file redirect is read implicitly and cannot hang
# (regular files always reach EOF).
capture_stdin "$BODY" -- pr-create --repo acme/widget "Redirect body" develop
check_contains "stdin file redirect: reached banner" "Creating PR:"

echo ""
echo "== summary: ${passes} passed, ${fails} failed =="
[ "$fails" -eq 0 ]
