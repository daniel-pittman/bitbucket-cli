#!/usr/bin/env bash
#
# Regression harness for API-error reporting and the repo `project` field.
#
# Two defects motivated this harness:
#
#   1. SWALLOWED ERRORS (#52). Every HTTP helper used `curl -sf`. `-f`
#      makes curl fail without emitting the response body, so Bitbucket's
#      own explanation was discarded and the caller saw a bare exit code.
#      That is worst on a 403, whose body names the exact scope the token
#      is missing AND the scopes it carries. Commands compensated with
#      hardcoded guesses ("if this is a 403, the token probably lacks X")
#      that were wrong whenever the real cause differed. The fix captures
#      the status via `-w` and prints the API's message; these assertions
#      pin that the real message reaches stderr, that the exit-code
#      contract is unchanged, and that nothing credential-shaped leaks
#      along with it.
#
#   2. WRITE-ONLY FIELD (#53). `bb repo-update --project KEY` could SET a
#      repository's project but nothing could read it back, so a wrong key
#      was invisible. `bb repo` and `bb repos` now display it from the
#      response they already fetch.
#
# Every case drives the real `bb` against a local mock that returns a
# scripted status + body, so the assertions are about what a user actually
# sees, not about internal helpers. python3 is REQUIRED: the mock is the
# test, and skipping it would leave a harness that passes without checking
# anything.
#
# bash 3.2-safe (no ${var,,}, mapfile, or declare -A). Runs on the host
# runner rather than the bash:3.2 image because it needs curl + git + jq +
# python3; bash-3.2 PARSE safety of the same code is covered by the
# "Parse bb under bash 3.2" CI step.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
BB="${HERE}/../../bb"

# A distinctive token: every assertion that checks for a leak looks for
# this exact string, so a redaction regression is unambiguous.
export BB_USER="ci@example.com"
export BB_TOKEN="SENTINEL_TOKEN_MUST_NOT_LEAK"
export BB_WORKSPACE="acme"

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
check_rc()           { if [ "${RC}" -eq "$2" ]; then ok "$1"; else failmsg "$1" "expected rc=$2"; fi; }
check_rc_not()       { if [ "${RC}" -ne "$2" ]; then ok "$1"; else failmsg "$1" "expected rc != $2"; fi; }

if ! command -v python3 >/dev/null 2>&1; then
    echo "FAIL: python3 is required by this harness (it serves the mock API)." >&2
    exit 1
fi

WORK="$(mktemp -d)"
STATUS_FILE="$WORK/status.txt"
BODY_FILE="$WORK/body.txt"
PORT_FILE="$WORK/port.txt"
: > "$PORT_FILE"

cleanup() {
    if [ -n "${SRVPID:-}" ]; then
        kill "$SRVPID" 2>/dev/null
        wait "$SRVPID" 2>/dev/null
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

# Mock API. Each request replies with whatever status/body the case just
# wrote to STATUS_FILE / BODY_FILE, so a single server covers every
# scenario. DELETE always answers 204 with an EMPTY body: that is the
# real shape of Bitbucket's delete responses and it exercises the
# body/status split's empty-body edge (a naive split collapses "\n204"
# into an unparseable single field).
python3 - "$STATUS_FILE" "$BODY_FILE" "$PORT_FILE" >/dev/null 2>&1 <<'PY' &
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
statusfile, bodyfile, portfile = sys.argv[1], sys.argv[2], sys.argv[3]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _scripted(self):
        try:
            status = int(open(statusfile).read().strip())
        except Exception:
            status = 200
        try:
            body = open(bodyfile, "rb").read()
        except Exception:
            body = b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        self._scripted()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n:
            self.rfile.read(n)
        self._scripted()

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n:
            self.rfile.read(n)
        self._scripted()

    def do_DELETE(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
srv.daemon_threads = True
open(portfile, "w").write(str(srv.server_address[1]))
srv.serve_forever()
PY
SRVPID=$!

port=""
waited=0
while [ -z "$port" ] && [ "$waited" -lt 40 ]; do
    sleep 0.25
    port="$(cat "$PORT_FILE" 2>/dev/null)"
    waited=$((waited + 1))
done
if [ -z "$port" ]; then
    echo "FAIL: mock API never bound a port" >&2
    exit 1
fi
export BB_API_BASE="http://127.0.0.1:${port}/2.0"

# Run from a non-git directory so the workspace resolves from BB_WORKSPACE
# rather than whatever origin the checkout this harness lives in happens to
# have. The asserted request paths depend on that resolution.
cd "$WORK"

# scenario <status> <body>  → what the mock returns for the next request.
scenario() {
    printf '%s' "$1" > "$STATUS_FILE"
    printf '%s' "$2" > "$BODY_FILE"
}

# capture -- <bb args...>  → sets OUT (stdout+stderr) and RC.
capture() {
    [ "$1" = "--" ] && shift
    OUT="$("$BB" "$@" </dev/null 2>&1)"
    RC=$?
}

# Bitbucket's real 403 scope-denial envelope, verified against the live
# API. `detail.required` / `detail.granted` are the payload that makes a
# scope failure self-diagnosing, and the whole reason -f had to go.
SCOPE_403='{"type":"error","error":{"message":"Your credentials lack one or more required privilege scopes.","detail":{"required":["read:workspace:bitbucket"],"granted":["read:repository:bitbucket","read:pullrequest:bitbucket"]}}}'

echo "== #52: the API's own error reaches the user =="

scenario 403 "$SCOPE_403"
capture -- workspaces
check_contains "403: API message printed"        "Your credentials lack one or more required privilege scopes."
check_contains "403: required scopes named"      "required scopes: read:workspace:bitbucket"
check_contains "403: granted scopes named"       "granted scopes:  read:repository:bitbucket, read:pullrequest:bitbucket"
check_contains "403: status + endpoint reported" "HTTP 403 on GET /user/workspaces"
check_contains "403: points at token rotation"   "id.atlassian.com/manage-profile/security/api-tokens"
check_rc       "403: exit code stays 22"         22

# The speculative per-command hint is gone: the cause is now quoted from
# the API, so guessing at it would contradict the printed message.
check_not_contains "403: no speculative guess"   "If this is a 403"

scenario 401 '{"type":"error","error":{"message":"Invalid or expired token"}}'
capture -- repos
check_contains "401: API message printed"    "Invalid or expired token"
check_contains "401: names token expiry"     "invalid, expired, or revoked"
check_rc       "401: exit code stays 22"     22

# `detail` is a string on some endpoints rather than the scope object.
scenario 400 '{"type":"error","error":{"message":"Bad request","detail":"branch is protected"}}'
capture -- repos
check_contains "400: string detail printed" "branch is protected"

# A non-JSON body (proxy/gateway HTML) must still surface something.
scenario 502 '<html><head><title>502 Bad Gateway</title></head></html>'
capture -- repos
check_contains "502: non-JSON body excerpted" "502 Bad Gateway"
check_rc       "502: exit code stays 22"      22

echo ""
echo "== #52: nothing credential-shaped leaks with the error =="

# An upstream that echoes the token back must not put it on the terminal.
# This is the assertion that fails if _redact is dropped.
scenario 403 "{\"type\":\"error\",\"error\":{\"message\":\"denied for token ${BB_TOKEN} on https://svc:hunter2@example.com/cb\"}}"
capture -- repos
check_not_contains "leak: token not echoed"        "SENTINEL_TOKEN_MUST_NOT_LEAK"
check_contains     "leak: token replaced"          "[redacted]"
check_not_contains "leak: URL password not echoed" "hunter2"
check_contains     "leak: URL creds replaced"      "https://[redacted]@example.com/cb"

echo ""
echo "== #52: exit-code contract is unchanged =="

# A transport failure (nothing listening) keeps curl's own exit code and
# must NOT be reported as an HTTP error. Port 9 is the discard port.
OLD_BASE="$BB_API_BASE"
export BB_API_BASE="http://127.0.0.1:9/2.0"
capture -- repos
check_rc_not   "transport: not reported as HTTP 22"  22
check_contains "transport: named as connectivity"    "connectivity error, not an API rejection"
check_not_contains "transport: no HTTP status claimed" "HTTP 0 on"
export BB_API_BASE="$OLD_BASE"

echo ""
echo "== #52: BB_DEBUG traces requests without leaking =="

scenario 200 '{"values":[],"pagelen":0}'
capture -- repos
check_not_contains "no BB_DEBUG: no trace line" "[bb] GET"

OUT="$(BB_DEBUG=1 "$BB" repos </dev/null 2>&1)"; RC=$?
check_contains     "BB_DEBUG=1: traces method, path, status" "[bb] GET /repositories/acme?pagelen=100&sort=-updated_on -> 200"
check_not_contains "BB_DEBUG=1: token not traced"            "SENTINEL_TOKEN_MUST_NOT_LEAK"
check_rc           "BB_DEBUG=1: success still exits 0"       0

echo ""
echo "== #52: the success path is untouched =="

scenario 200 '{"values":[{"slug":"widget","updated_on":"2026-07-01T00:00:00Z","language":"python","project":{"key":"PLAT"}}]}'
capture -- repos
check_rc           "200: exits 0"               0
check_contains     "200: body rendered"         "widget"
check_not_contains "200: no error banner"       "Error: HTTP"

# An empty 204 body must not be mistaken for a malformed response — the
# status is appended last precisely so this case stays splittable.
scenario 200 '{"values":[{"name":"Test","uuid":"{e-1}"}]}'
capture -- environment-delete widget Test
check_rc       "204 empty body: delete succeeds"  0
check_contains "204 empty body: success reported" "Deleted environment 'Test'"

echo ""
echo "== #53: a repo's project is readable =="

REPO_JSON='{"full_name":"acme/widget","description":"A widget","language":"python","created_on":"2026-01-01T00:00:00Z","updated_on":"2026-07-01T00:00:00Z","size":1048576,"mainbranch":{"name":"develop"},"is_private":true,"project":{"key":"PLAT","name":"Platform Services"},"links":{"clone":[{"name":"ssh","href":"git@bitbucket.org:acme/widget.git"}],"html":{"href":"https://bitbucket.org/acme/widget"}}}'
scenario 200 "$REPO_JSON"
capture -- repo widget
check_rc       "repo: exits 0"                 0
check_contains "repo: project key and name"    "Project:     PLAT (Platform Services)"

# Workspaces that do not use projects return no `project` at all.
NOPROJ_JSON='{"full_name":"acme/widget","description":"A widget","language":"python","created_on":"2026-01-01T00:00:00Z","updated_on":"2026-07-01T00:00:00Z","size":1048576,"mainbranch":{"name":"develop"},"is_private":true,"links":{"clone":[{"name":"ssh","href":"git@bitbucket.org:acme/widget.git"}],"html":{"href":"https://bitbucket.org/acme/widget"}}}'
scenario 200 "$NOPROJ_JSON"
capture -- repo widget
check_rc       "repo (no project): exits 0"        0
check_contains "repo (no project): reads (none)"   "Project:     (none)"

scenario 200 '{"values":[{"slug":"widget","updated_on":"2026-07-01T00:00:00Z","language":"python","project":{"key":"PLAT"}},{"slug":"orphan","updated_on":"2026-06-01T00:00:00Z","language":"go"}]}'
capture -- repos
check_contains "repos: PROJECT column header"     "PROJECT"
# Anchored on the preceding column so these pin the rendered ROW, not just
# the presence of the word somewhere in the output: a project column that
# silently dropped its value would still contain "PLAT" in the header-less
# case, but not "2026-07-01   PLAT".
check_contains "repos: project key in the row"    "2026-07-01   PLAT"
check_contains "repos: missing project reads -"   "2026-06-01   -"

echo ""
echo "== summary: ${passes} passed, ${fails} failed =="
[ "$fails" -eq 0 ]
