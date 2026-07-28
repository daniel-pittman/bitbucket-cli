#!/usr/bin/env bash
#
# Regression harness for `bb members` and `bb pr-create --reviewer` (#55).
#
# The two halves are one capability. Bitbucket identifies PR reviewers ONLY
# by account UUID, so a --reviewer flag with no way to discover a UUID is
# unusable, and a member listing with no flag to consume it is a lookup to
# nowhere. These assertions pin both ends AND the contract between them:
# the UUID column `bb members` prints is the value `--reviewer` accepts and
# puts in the request body.
#
# The rejection cases matter as much as the happy path. A display name or
# nickname passed to --reviewer is the natural mistake, and it must fail
# locally with a pointer to `bb members` rather than costing a round trip
# to learn that Bitbucket wanted a UUID. Those cases also assert ZERO
# requests were issued, so a regression that defers validation to the API
# fails here.
#
# python3 is REQUIRED: the mock API is the test, and skipping it would
# leave a harness that passes without checking anything.
#
# bash 3.2-safe (no ${var,,}, mapfile, or declare -A). Runs on the host
# runner because it needs curl + jq + git + python3; bash-3.2 PARSE safety
# is covered by the floor job, and the runtime constructs this exercises
# (empty-array count under `set -u`, ERE interval quantifiers in the UUID
# guard) were verified against the bash:3.2 image.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
BB="${HERE}/../../bb"

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
check_rc_nonzero()   { if [ "${RC}" -ne 0 ]; then ok "$1"; else failmsg "$1" "expected non-zero exit"; fi; }

if ! command -v python3 >/dev/null 2>&1; then
    echo "FAIL: python3 is required by this harness (it serves the mock API)." >&2
    exit 1
fi

WORK="$(mktemp -d)"
STATUS_FILE="$WORK/status.txt"
BODY_FILE="$WORK/body.txt"
PORT_FILE="$WORK/port.txt"
REQLOG="$WORK/requests.log"
POSTBODY="$WORK/post_body.json"
: > "$PORT_FILE"
: > "$REQLOG"

cleanup() {
    if [ -n "${SRVPID:-}" ]; then
        kill "$SRVPID" 2>/dev/null
        wait "$SRVPID" 2>/dev/null
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

# Mock API: replies with the scripted status/body, logs every request path
# (so "issued no request" is assertable), and records POST bodies.
python3 - "$STATUS_FILE" "$BODY_FILE" "$PORT_FILE" "$REQLOG" "$POSTBODY" >/dev/null 2>&1 <<'PY' &
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
statusfile, bodyfile, portfile, reqlog, postbody = sys.argv[1:6]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self):
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
        with open(reqlog, "a") as fh:
            fh.write("GET " + self.path + "\n")
        self._reply()

    def do_POST(self):
        with open(reqlog, "a") as fh:
            fh.write("POST " + self.path + "\n")
        n = int(self.headers.get("Content-Length", "0"))
        open(postbody, "wb").write(self.rfile.read(n) if n else b"")
        self._reply()

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

scenario() {
    printf '%s' "$1" > "$STATUS_FILE"
    printf '%s' "$2" > "$BODY_FILE"
}

capture() {
    [ "$1" = "--" ] && shift
    OUT="$("$BB" "$@" </dev/null 2>&1)"
    RC=$?
}

reqcount() { wc -l < "$REQLOG" | tr -d ' '; }

# Hermetic git checkout so pr-create's source-branch auto-detect is
# deterministic and the workspace resolves from BB_WORKSPACE.
git init -q -b work-branch "$WORK/repo" 2>/dev/null || {
    git init -q "$WORK/repo"
    git -C "$WORK/repo" checkout -q -b work-branch
}
git -C "$WORK/repo" config user.email "ci@example.com"
git -C "$WORK/repo" config user.name  "CI"
git -C "$WORK/repo" commit -q --allow-empty -m init
git -C "$WORK/repo" remote add origin "git@bitbucket.org:acme/widget.git"
cd "$WORK/repo"

# Invented identifiers. Never copy real workspace members into a fixture.
U1="{11111111-2222-3333-4444-555555555555}"
U2="{66666666-7777-8888-9999-000000000000}"

# `.user` nesting mirrors Bitbucket's workspace_membership envelope: the
# reviewer UUID lives at .user.uuid, not at the top level.
MEMBERS_JSON="{\"values\":[
  {\"type\":\"workspace_membership\",\"user\":{\"type\":\"user\",\"display_name\":\"Ada Lovelace\",\"nickname\":\"ada\",\"account_id\":\"acct-ada\",\"uuid\":\"${U1}\"}},
  {\"type\":\"workspace_membership\",\"user\":{\"type\":\"user\",\"display_name\":\"Grace Hopper\",\"nickname\":\"grace\",\"account_id\":\"acct-grace\",\"uuid\":\"${U2}\"}}
]}"

echo "== bb members: lists people and their UUIDs =="

scenario 200 "$MEMBERS_JSON"
capture -- members
check_contains "members: exits with the header"     "DISPLAY NAME"
check_contains "members: NICKNAME column"           "NICKNAME"
check_contains "members: UUID column"               "UUID"
check_contains "members: display name rendered"     "Ada Lovelace"
check_contains "members: nickname rendered"         "ada"
check_contains "members: uuid rendered"             "$U1"
check_contains "members: second member rendered"    "Grace Hopper"
check_contains "members: names the workspace"       "Members of acme:"

# The endpoint is workspace-scoped. A regression to /users/... or a repo
# path would still return the mock's body, so assert the REQUEST path.
if grep -q "GET /2.0/workspaces/acme/members?pagelen=100" "$REQLOG"; then
    ok "members: hits /workspaces/{ws}/members"
else
    OUT="$(cat "$REQLOG")"; RC=0
    failmsg "members: hits /workspaces/{ws}/members" "wrong request path"
fi

# An explicit [workspace] positional targets another workspace.
: > "$REQLOG"
capture -- members other-org
if grep -q "GET /2.0/workspaces/other-org/members" "$REQLOG"; then
    ok "members: explicit [workspace] positional is used"
else
    OUT="$(cat "$REQLOG")"; RC=0
    failmsg "members: explicit [workspace] positional is used" "wrong workspace in path"
fi

# Missing optional fields must not render as "null".
scenario 200 "{\"values\":[{\"user\":{\"uuid\":\"${U1}\"}}]}"
capture -- members
check_contains     "members: absent name/nickname read as -" "-"
check_not_contains "members: no literal null rendered"       "null"

# A workspace larger than one page must say so rather than silently
# truncating — the person being looked up may be on page 2.
scenario 200 "{\"values\":[{\"user\":{\"display_name\":\"Ada\",\"nickname\":\"ada\",\"uuid\":\"${U1}\"}}],\"next\":\"https://api.bitbucket.org/2.0/workspaces/acme/members?page=2\"}"
capture -- members
check_contains "members: truncation is disclosed"   "showing first 100"
check_contains "members: points at the MCP tool"    "members_list"

echo ""
echo "== pr-create --reviewer: UUID reaches the request body =="

PR_OK='{"id":7,"links":{"html":{"href":"https://bitbucket.org/acme/widget/pull-requests/7"}}}'

# body_json <label> -- <bb args...>  → runs bb, loads the POSTed body.
body_json() {
    [ "$1" = "--" ] && shift
    : > "$POSTBODY"
    scenario 200 "$PR_OK"
    "$BB" "$@" </dev/null >/dev/null 2>&1
    BODY_JSON="$(cat "$POSTBODY" 2>/dev/null)"
}
check_json() {
    local got; got="$(printf '%s' "$BODY_JSON" | jq -r "$2" 2>/dev/null)"
    if [ "$got" = "$3" ]; then ok "$1"; else failmsg "$1" "jq '$2' => [$got], expected [$3]"; fi
}

body_json -- pr-create --repo acme/widget "One reviewer" develop --reviewer "$U1"
check_json "single --reviewer: payload shape is [{uuid}]" '.reviewers' "$(printf '[\n  {\n    "uuid": "%s"\n  }\n]' "$U1")"
check_json "single --reviewer: uuid value preserved"       '.reviewers[0].uuid' "$U1"
check_json "single --reviewer: title still set"            '.title' "One reviewer"

# Repeatable, order preserved.
body_json -- pr-create --repo acme/widget "Two reviewers" develop --reviewer "$U1" --reviewer "$U2"
check_json "repeated --reviewer: both present"    '.reviewers | length' "2"
check_json "repeated --reviewer: order preserved" '.reviewers[1].uuid'  "$U2"

# --reviewer=VALUE form.
body_json -- pr-create --repo acme/widget "Eq form" develop --reviewer="$U1"
check_json "--reviewer=VALUE form accepted" '.reviewers[0].uuid' "$U1"

# Omitted entirely when unused — parity with bb_ops.pr_create, which only
# sets the key for a non-empty list. An empty [] would be a payload change.
body_json -- pr-create --repo acme/widget "No reviewers" develop
check_json "no --reviewer: key omitted, not empty list" 'has("reviewers")' "false"

# The payload build was refactored from duplicated jq programs to
# incremental field addition; description and reviewers must coexist.
body_json -- pr-create --repo acme/widget "Both" develop --description "BODY_TEXT" --reviewer "$U1"
check_json "reviewer + description: description kept" '.description'       "BODY_TEXT"
check_json "reviewer + description: reviewer kept"    '.reviewers[0].uuid' "$U1"
check_json "reviewer + description: base fields kept" '.destination.branch.name' "develop"

# A bare (unbraced) UUID is accepted: users routinely strip the braces.
BARE="11111111-2222-3333-4444-555555555555"
body_json -- pr-create --repo acme/widget "Bare uuid" develop --reviewer "$BARE"
check_json "bare (unbraced) uuid accepted" '.reviewers[0].uuid' "$BARE"

echo ""
echo "== pr-create --reviewer: bad values fail locally, before any request =="

# A name or nickname is the natural mistake. It must fail here, not as a
# 400, and must point at the lookup that produces the right value.
: > "$REQLOG"
capture -- pr-create --repo acme/widget "T" develop --reviewer "ada"
check_rc_nonzero "nickname rejected"
check_contains   "nickname rejection names the requirement" "must be a Bitbucket account UUID"
check_contains   "nickname rejection points at bb members"  "bb members"
if [ "$(reqcount)" = "0" ]; then
    ok "nickname rejected with ZERO requests issued"
else
    OUT="$(cat "$REQLOG")"; RC=0
    failmsg "nickname rejected with ZERO requests issued" "$(reqcount) request(s) were issued"
fi

# A half-brace is a copy/paste truncation, not a valid alternate form.
: > "$REQLOG"
capture -- pr-create --repo acme/widget "T" develop --reviewer "{11111111-2222-3333-4444-555555555555"
check_rc_nonzero "unmatched brace rejected"
if [ "$(reqcount)" = "0" ]; then
    ok "unmatched brace rejected with ZERO requests issued"
else
    OUT="$(cat "$REQLOG")"; RC=0
    failmsg "unmatched brace rejected with ZERO requests issued" "$(reqcount) request(s) issued"
fi

# Wrong-length UUID (a truncated paste).
capture -- pr-create --repo acme/widget "T" develop --reviewer "11111111-2222-3333-4444-5555"
check_rc_nonzero "short uuid rejected"

# Empty value.
capture -- pr-create --repo acme/widget "T" develop --reviewer ""
check_rc_nonzero "empty --reviewer rejected"

# Missing value entirely (flag at end of argv).
capture -- pr-create --repo acme/widget "T" develop --reviewer
check_rc_nonzero "--reviewer with no value rejected"

echo ""
echo "== the two halves agree: the printed UUID is an accepted --reviewer =="

# The contract issue #55 exists for. Take the UUID out of `bb members`
# output exactly as a user would, feed it to --reviewer, and assert it
# lands in the payload. A format change on either side breaks this.
scenario 200 "$MEMBERS_JSON"
PRINTED_UUID="$("$BB" members </dev/null 2>/dev/null | awk '/Ada Lovelace/ {print $NF}')"
if [ -n "$PRINTED_UUID" ]; then
    ok "members output yields a copyable UUID ($PRINTED_UUID)"
else
    OUT="(no uuid parsed from members output)"; RC=0
    failmsg "members output yields a copyable UUID" "could not parse the UUID column"
fi
body_json -- pr-create --repo acme/widget "Round trip" develop --reviewer "$PRINTED_UUID"
check_json "members UUID is accepted by --reviewer verbatim" '.reviewers[0].uuid' "$U1"

echo ""
echo "== summary: ${passes} passed, ${fails} failed =="
[ "$fails" -eq 0 ]
