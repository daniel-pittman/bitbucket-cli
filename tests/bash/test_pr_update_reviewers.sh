#!/usr/bin/env bash
#
# Regression harness for `bb pr-update` reviewer changes (#57).
#
# The defining hazard: Bitbucket's PR PUT REPLACES the whole `reviewers`
# array. Sending only the person being added silently unassigns everyone
# else, and the request still returns 200, so the bug is invisible without
# inspecting the body. Every assertion here reads the actual PUT body.
#
# The second hazard is approvals. Removing a reviewer who has already
# approved discards the approval, and re-adding them does not bring it
# back. That is refused without an explicit opt-in, and the refusal must
# happen BEFORE the PUT — a guard that fires after the write is worthless.
# Those cases assert the request log contains no PUT at all.
#
# Note the two arrays are NOT interchangeable: the current reviewer list is
# `.reviewers[].uuid`, but approval state lives only on `.participants[]`.
# A guard that looks for `approved` on `.reviewers` finds nothing and lets
# every removal through, which is exactly the silent-approval-loss bug. The
# fixtures keep them distinct so that mistake fails here.
#
# python3 is REQUIRED: the mock API is the test.
#
# bash 3.2-safe. Runs on the host runner (needs curl + jq + git + python3).

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
GETBODY="$WORK/get_body.json"
PUTBODY="$WORK/put_body.json"
PORT_FILE="$WORK/port.txt"
REQLOG="$WORK/requests.log"
: > "$PORT_FILE"
: > "$REQLOG"
: > "$PUTBODY"

cleanup() {
    if [ -n "${SRVPID:-}" ]; then
        kill "$SRVPID" 2>/dev/null
        wait "$SRVPID" 2>/dev/null
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

# Mock API. GET returns the scripted PR record (the "current" state the
# read-modify-write reads); PUT records the request body and echoes it back
# merged onto the PR so the command's readback has something to print.
python3 - "$GETBODY" "$PUTBODY" "$PORT_FILE" "$REQLOG" >/dev/null 2>&1 <<'PY' &
import sys, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
getbody, putbody, portfile, reqlog = sys.argv[1:5]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _current(self):
        try:
            return json.load(open(getbody))
        except Exception:
            return {"id": 42, "title": "t", "reviewers": [], "participants": []}

    def _send(self, obj, status=200):
        b = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        with open(reqlog, "a") as fh:
            fh.write("GET " + self.path + "\n")
        self._send(self._current())

    def do_PUT(self):
        with open(reqlog, "a") as fh:
            fh.write("PUT " + self.path + "\n")
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n else b"{}"
        open(putbody, "wb").write(raw)
        # Reflect the submitted reviewers back as full user records so the
        # command's "Reviewers now:" readback renders realistically.
        try:
            sent = json.loads(raw)
        except Exception:
            sent = {}
        pr = self._current()
        pr.update({k: v for k, v in sent.items() if k != "reviewers"})
        if "reviewers" in sent:
            pr["reviewers"] = [
                {"uuid": r["uuid"], "display_name": "User " + r["uuid"]}
                for r in sent["reviewers"]
            ]
        self._send(pr)

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

cd "$WORK"

A="{aaaaaaaa-1111-2222-3333-444444444444}"
B="{bbbbbbbb-1111-2222-3333-444444444444}"
C="{cccccccc-1111-2222-3333-444444444444}"

# set_pr <approved-uuid-or-empty> <uuid...>  → the PR the GET will return.
# `reviewers` and `participants` are built separately on purpose: approval
# state exists ONLY on participants.
set_pr() {
    local approved="$1"; shift
    python3 - "$GETBODY" "$approved" "$@" <<'PY'
import sys, json
out, approved, uuids = sys.argv[1], sys.argv[2], sys.argv[3:]
json.dump({
    "id": 42,
    "title": "Existing title",
    "links": {"html": {"href": "https://bitbucket.org/acme/widget/pull-requests/42"}},
    "reviewers": [{"uuid": u, "display_name": "User " + u} for u in uuids],
    "participants": [
        {"role": "REVIEWER", "approved": (u == approved),
         "user": {"uuid": u, "display_name": "User " + u}}
        for u in uuids
    ],
}, open(out, "w"))
PY
    : > "$PUTBODY"
    : > "$REQLOG"
}

run() {
    [ "$1" = "--" ] && shift
    OUT="$("$BB" "$@" </dev/null 2>&1)"
    RC=$?
}

# check_put <label> <jq-filter> <expected>
check_put() {
    local got; got="$(jq -r "$2" < "$PUTBODY" 2>/dev/null)"
    if [ "$got" = "$3" ]; then ok "$1"; else failmsg "$1" "jq '$2' => [$got], expected [$3]"; fi
}
no_put() {
    if grep -q "^PUT " "$REQLOG"; then
        OUT="$(cat "$REQLOG")"; RC=0
        failmsg "$1" "a PUT was issued when none should have been"
    else
        ok "$1"
    fi
}

echo "== --reviewer ADDS without unassigning anyone =="

# The core assertion. A naive implementation sends [C] and silently drops
# A and B; the API returns 200 either way.
set_pr "" "$A" "$B"
run -- pr-update acme/widget 42 --reviewer "$C"
check_put "add: existing reviewers are resent"  '[.reviewers[].uuid] | join(",")' "$A,$B,$C"
check_put "add: count is 3, not 1"              '.reviewers | length' "3"
check_contains "add: reports the resulting list" "Reviewers now:"
check_contains "add: readback shows the added uuid" "$C"

# Idempotent: re-adding an existing reviewer must not duplicate them.
set_pr "" "$A" "$B"
run -- pr-update acme/widget 42 --reviewer "$B"
check_put "add existing: no duplicate" '[.reviewers[].uuid] | join(",")' "$A,$B"

# Repeatable and order-stable.
set_pr "" "$A"
run -- pr-update acme/widget 42 --reviewer "$B" --reviewer "$C"
check_put "add two: appended in order" '[.reviewers[].uuid] | join(",")' "$A,$B,$C"

# A PR with no reviewers yet.
set_pr ""
run -- pr-update acme/widget 42 --reviewer "$A"
check_put "add to empty PR" '[.reviewers[].uuid] | join(",")' "$A"

# --reviewer=VALUE form.
set_pr "" "$A"
run -- pr-update acme/widget 42 --reviewer="$C"
check_put "--reviewer=VALUE form" '[.reviewers[].uuid] | join(",")' "$A,$C"

echo ""
echo "== --remove-reviewer REMOVES only the named person =="

set_pr "" "$A" "$B" "$C"
run -- pr-update acme/widget 42 --remove-reviewer "$B"
check_put "remove: others kept" '[.reviewers[].uuid] | join(",")' "$A,$C"

# Removing everyone must send [], not omit the key (omitting would leave
# the reviewers untouched, silently doing nothing).
set_pr "" "$A"
run -- pr-update acme/widget 42 --remove-reviewer "$A"
check_put "remove last: sends an empty array" '.reviewers | length' "0"
check_put "remove last: key is present"       'has("reviewers")' "true"

# add + remove compose in one call.
set_pr "" "$A" "$B"
run -- pr-update acme/widget 42 --reviewer "$C" --remove-reviewer "$A"
check_put "add+remove compose" '[.reviewers[].uuid] | join(",")' "$B,$C"

# Removing someone who is not on the PR is a no-op, not an error.
set_pr "" "$A"
run -- pr-update acme/widget 42 --remove-reviewer "$C"
check_put "remove non-reviewer: list unchanged" '[.reviewers[].uuid] | join(",")' "$A"

# A bare (unbraced) UUID is explicitly accepted by the validator, but the
# API returns reviewers BRACED. If the value is stored raw, the string
# comparison matches nothing and the removal silently no-ops with exit 0.
BARE_A="aaaaaaaa-1111-2222-3333-444444444444"
set_pr "" "$A" "$B"
run -- pr-update acme/widget 42 --remove-reviewer "$BARE_A"
check_put "remove by bare uuid still removes" '[.reviewers[].uuid] | join(",")' "$B"

# Same shape on the add side: a bare add must dedup against the braced
# entry already on the PR rather than appending a second copy.
set_pr "" "$A"
run -- pr-update acme/widget 42 --reviewer "$BARE_A"
check_put "add by bare uuid does not duplicate" '.reviewers | length' "1"

echo ""
echo "== approvals are not discarded silently =="

# B has approved. Removing B must be refused BEFORE any write.
set_pr "$B" "$A" "$B"
run -- pr-update acme/widget 42 --remove-reviewer "$B"
check_rc_nonzero "removing an approver is refused"
check_contains   "refusal names the reason"     "already approved"
check_contains   "refusal names the opt-in"     "--drop-approvals"
check_contains   "refusal names who"            "$B"
no_put           "refusal happens before the PUT"

# The guard must be scoped to approvers: removing a non-approver while
# someone else has approved must still work.
set_pr "$B" "$A" "$B"
run -- pr-update acme/widget 42 --remove-reviewer "$A"
check_put "removing a non-approver needs no opt-in" '[.reviewers[].uuid] | join(",")' "$B"

# Explicit opt-in goes through.
set_pr "$B" "$A" "$B"
run -- pr-update acme/widget 42 --remove-reviewer "$B" --drop-approvals
check_put "--drop-approvals allows it" '[.reviewers[].uuid] | join(",")' "$A"

# Approval state lives on participants, NOT reviewers. Build a PR whose
# reviewers carry no approval marker at all (the real shape) and confirm
# the guard still fires — a guard reading `.reviewers[].approved` sees
# nothing here and would wrongly allow the removal.
set_pr "$A" "$A"
if jq -e '[.reviewers[] | has("approved")] | any | not' "$GETBODY" >/dev/null; then
    ok "fixture: reviewers carry no approval field (matches the real API)"
else
    OUT="$(cat "$GETBODY")"; RC=0
    failmsg "fixture: reviewers carry no approval field" "fixture drifted"
fi
run -- pr-update acme/widget 42 --remove-reviewer "$A"
check_rc_nonzero "guard reads approval from participants"
no_put           "participants-sourced refusal issues no PUT"

echo ""
echo "== other fields still work =="

# A title-only update must not pay for the read-modify-write, and must not
# touch reviewers at all.
set_pr "" "$A"
run -- pr-update acme/widget 42 --title "Just a title"
check_put "title-only: reviewers key absent" 'has("reviewers")' "false"
check_put "title-only: title sent"           '.title' "Just a title"
if grep -q "^GET " "$REQLOG"; then
    OUT="$(cat "$REQLOG")"; RC=0
    failmsg "title-only: issues no extra GET" "a GET was issued"
else
    ok "title-only: issues no extra GET"
fi

# Reviewers combine with a title change in one PUT.
set_pr "" "$A"
run -- pr-update acme/widget 42 --title "T2" --reviewer "$C"
check_put "title+reviewer: title sent"     '.title' "T2"
check_put "title+reviewer: reviewers sent" '[.reviewers[].uuid] | join(",")' "$A,$C"

echo ""
echo "== bad input fails locally =="

set_pr "" "$A"
run -- pr-update acme/widget 42 --reviewer "ada"
check_rc_nonzero "nickname rejected"
check_contains   "nickname rejection points at bb members" "bb members"
no_put           "nickname rejection issues no PUT"

set_pr "" "$A"
run -- pr-update acme/widget 42 --remove-reviewer "not-a-uuid"
check_rc_nonzero "bad remove-reviewer rejected"
no_put           "bad remove-reviewer issues no PUT"

# No fields at all is still a usage error, and now mentions the new flags.
set_pr "" "$A"
run -- pr-update acme/widget 42
check_rc_nonzero "no fields rejected"
check_contains   "usage mentions --reviewer" "--reviewer"
no_put           "no-fields usage error issues no PUT"

echo ""
echo "== summary: ${passes} passed, ${fails} failed =="
[ "$fails" -eq 0 ]
