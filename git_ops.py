"""
git_ops — lightweight git wrappers used by the MCP server.

The agent needs git context to do its job: "what's my current branch?",
"is the working tree clean?", "what's the workspace/repo from origin?",
"what did I commit recently?", "what's uncommitted?". These wrappers
shell out to `git` with the safe-to-test `runner=` injection seam from
`bb_api.detect_repo` so the test suite never touches a real subprocess.

Stdlib-only on purpose, same as bb_api: keeps the MCP server's bootstrap
fast and minimises the supply-chain surface.

Public surface (all functions accept an optional `path` defaulting to the
current working directory):

    git_current_branch(path?) -> str
    git_status(path?) -> dict
    git_remote_repo(path?) -> (workspace, repo)
    git_recent_commits(path?, *, count=10) -> list[dict]
    git_uncommitted_changes(path?) -> dict

Errors raise `GitOpError` with the failing command's stderr so callers can
surface a useful message rather than guessing at the failure mode.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from bb_api import parse_remote_url


# Regex for redacting URL-embedded credentials before they land in error
# messages. Matches the `user:token@` (or `user@`) shape in any URL and
# replaces with `[redacted]@`. Used in any GitOpError message that
# echoes a remote URL — we'd rather lose the auth detail than leak a
# token into the MCP agent's context / downstream logs.
#
# `[^/]+` (excludes only `/`, allows `@` inside) so a password
# containing a literal `@` (legal in RFC 3986 syntax) is greedy-matched
# up to the LAST `@` before the path. The previous `[^/@]+@` shape
# would have stopped at the first `@`, leaking the tail of the password
# (e.g. `https://user:p@ss@host/...` → `https://[redacted]@ss@host/...`).
_URL_CRED_PATTERN = re.compile(r"://[^/]+@")


def _redact_url_creds(url: str) -> str:
    """Strip `user:token@` from a URL before echoing it in error text."""
    return _URL_CRED_PATTERN.sub("://[redacted]@", url)

# Subprocess defaults applied to every `git` call:
#
#   timeout=30s — a wedged git (stuck on a credential-helper prompt, a
#     held `.git/index.lock`, an NFS mount whose server went away) would
#     otherwise hang the MCP server thread forever with no recovery path.
#
#   encoding="utf-8" — `text=True` alone defers to
#     locale.getpreferredencoding(), which on minimal Docker / cron /
#     systemd contexts is ASCII or C, blowing up with UnicodeDecodeError
#     on a UTF-8 filename or author name BEFORE we can wrap as GitOpError.
#
#   errors="replace" — never crash on a non-UTF-8 byte; substitute U+FFFD
#     and keep going. The caller would rather see a single replacement
#     character than an exception inside subprocess.run.
_GIT_SUBPROCESS_TIMEOUT = 30.0

# `-c color.ui=never` injected into every git invocation. A developer
# with `color.ui = always` in ~/.gitconfig forces git to emit ANSI
# escape sequences even when stdout is a pipe — the MCP agent (and
# any other consumer) would see `\x1b[31m...\x1b[m` garbage in diffs
# and log output. Disabling color at the command level overrides the
# config and matches what every other "machine-readable git" wrapper
# does.
_GIT_NO_COLOR = ["-c", "color.ui=never"]


# Sentinel returncode for parse-failure errors (the git command itself
# exited 0, but our parser couldn't make sense of the output). Picked
# at -1000 to stay outside Python's signal-killed convention: a
# subprocess child killed by signal N has returncode = -N (e.g. -1 for
# SIGHUP, -9 for SIGKILL, -15 for SIGTERM). Callers branching on
# `err.returncode == GIT_PARSE_ERROR_RETURNCODE` would otherwise
# misclassify a SIGHUP-killed git as a parse failure.
GIT_PARSE_ERROR_RETURNCODE = -1000


class GitOpError(RuntimeError):
    """Raised when a `git` invocation fails or returns unparseable output.

    Carries the failing command's stderr (truncated in the message) and
    a returncode field. For genuine git failures, returncode is git's own
    exit code (>= 0). For parse failures where git exited 0 but the
    parser couldn't extract a usable value, returncode is
    `GIT_PARSE_ERROR_RETURNCODE` (-1) — callers branching on git exit
    semantics should gate on `err.returncode >= 0` first. A separate
    exception class from `BBApiError` so MCP tools can render "git
    failure" vs "Bitbucket failure" differently.
    """

    def __init__(self, command: list[str], returncode: int, stderr: str):
        super().__init__(
            f"git {' '.join(command[1:])!r} failed (exit {returncode}): {stderr.strip()[:500]}"
        )
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


def _run_git(
    args: list[str],
    *,
    path: str | os.PathLike[str] | None = None,
    runner: Any = subprocess,
) -> str:
    """Run `git <args>` and return stdout text. Mirrors `bb_api.detect_repo`'s
    runner-injection pattern so tests can substitute a fake subprocess
    without monkey-patching the module.

    Every call is wrapped with:
      - `git -c color.ui=never` so ANSI escapes never leak into diffs
        / log output regardless of the user's gitconfig.
      - `timeout=_GIT_SUBPROCESS_TIMEOUT` so a wedged git can't hang
        the MCP server.
      - `encoding="utf-8", errors="replace"` so non-ASCII filenames
        / author names don't trip the locale-default decoder in
        minimal-environment containers.
    """
    cmd = ["git", *_GIT_NO_COLOR, *args]
    cwd = str(path) if path is not None else None
    # Environment hardening (belt + suspenders alongside the 30s timeout):
    #   GIT_TERMINAL_PROMPT=0 — git itself refuses to prompt for input,
    #     so a credential-helper-less repo with a 401 fails immediately
    #     with a clear error instead of wedging for 30s on a hidden
    #     prompt.
    #   GIT_ASKPASS="" — disables any GUI askpass helper that would
    #     otherwise pop up out-of-band (X11 dialog, macOS keychain
    #     prompt) and block the subprocess.
    git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
    try:
        result = runner.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            check=False,
            timeout=_GIT_SUBPROCESS_TIMEOUT,
            # stdin=DEVNULL turns any prompt into an immediate EOF
            # instead of letting git read from whatever stdin the
            # MCP server inherited.
            stdin=subprocess.DEVNULL,
            env=git_env,
        )
    except FileNotFoundError as e:
        # subprocess raises FileNotFoundError for two distinct cases.
        # Disambiguate so the agent sees the actual cause:
        #   - Missing cwd directory: e.filename is the cwd path
        #   - Missing git binary: e.filename is the executable name (git)
        if cwd is not None and getattr(e, "filename", None) == cwd:
            raise GitOpError(
                cmd, 127, f"path does not exist: {cwd}"
            ) from e
        raise GitOpError(cmd, 127, "git executable not found on PATH") from e
    except NotADirectoryError as e:
        # cwd is a path that exists but isn't a directory (e.g. a regular
        # file). Wrap as GitOpError so callers always see the documented
        # contract instead of a raw OSError.
        raise GitOpError(
            cmd, 127, f"path is not a directory: {getattr(e, 'filename', cwd) or cwd!r}"
        ) from e
    except PermissionError as e:
        # cwd unreadable / git binary lacks +x. e.filename indicates which.
        raise GitOpError(
            cmd, 126, f"permission denied: {getattr(e, 'filename', cwd) or 'git'!r}"
        ) from e
    except subprocess.TimeoutExpired as e:
        # Wrap so callers always see GitOpError. Use the parse-error
        # sentinel (-1) — the git process never exited, so there's no
        # real returncode to surface.
        raise GitOpError(
            cmd,
            GIT_PARSE_ERROR_RETURNCODE,
            f"git invocation timed out after {_GIT_SUBPROCESS_TIMEOUT}s",
        ) from e

    if result.returncode != 0:
        raise GitOpError(cmd, result.returncode, result.stderr or "")
    return result.stdout


# ---------------------------------------------------------------------------
# Current branch
# ---------------------------------------------------------------------------


def git_current_branch(
    path: str | os.PathLike[str] | None = None,
    *,
    runner: Any = subprocess,
) -> str:
    """Return the current branch name.

    Detached HEAD returns the literal string `"HEAD"`. The companion
    `git_status` function normalises porcelain v2's `"(detached)"` to
    the same `"HEAD"` sentinel so cross-checks between the two
    functions agree on the same underlying state.
    """
    out = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], path=path, runner=runner
    )
    branch = out.strip()
    if not branch:
        # Include the same `-c color.ui=never` prefix that _run_git
        # actually executed, so err.command reflects the real
        # invocation when a caller introspects the error.
        raise GitOpError(
            ["git", *_GIT_NO_COLOR, "rev-parse", "--abbrev-ref", "HEAD"],
            GIT_PARSE_ERROR_RETURNCODE,
            "git returned empty branch name",
        )
    return branch


# ---------------------------------------------------------------------------
# Remote-origin -> (workspace, repo)
# ---------------------------------------------------------------------------


def git_remote_repo(
    path: str | os.PathLike[str] | None = None,
    *,
    runner: Any = subprocess,
) -> tuple[str, str]:
    """Return (workspace, repo_slug) parsed from the `origin` remote URL.

    Distinct from `bb_api.detect_repo` because the MCP server's git-context
    layer needs the workspace too (for cross-workspace operations the
    agent might attempt). bb_api.detect_repo is the bash-parity surface
    that returns only the repo slug.

    Raises GitOpError if there's no origin remote, or the URL doesn't
    parse as a workspace/repo pair. Same loose-host-parsing behaviour as
    `parse_remote_url` (intentional — enterprise / self-hosted Bitbucket
    deployments use non-bitbucket.org URLs).
    """
    url = _run_git(["remote", "get-url", "origin"], path=path, runner=runner)
    parsed = parse_remote_url(url)
    if parsed is None:
        # Redact any embedded `user:token@` before the URL lands in the
        # error message — URL-embedded auth is a common CI pattern
        # (e.g. `https://x-token-auth:abcd@bitbucket.org/...`), and
        # this string flows up through MCP into the agent's context
        # and downstream logs.
        safe_url = _redact_url_creds(url.strip())
        # Include the same `-c color.ui=never` prefix that _run_git
        # actually executed.
        raise GitOpError(
            ["git", *_GIT_NO_COLOR, "remote", "get-url", "origin"],
            GIT_PARSE_ERROR_RETURNCODE,
            f"could not parse workspace/repo from origin URL: {safe_url!r}",
        )
    return parsed


# ---------------------------------------------------------------------------
# Status (branch, clean/dirty, ahead/behind, file lists)
# ---------------------------------------------------------------------------


def _parse_status_porcelain_v2(text: str) -> dict[str, Any]:
    """Parse `git status --porcelain=v2 --branch` output into a structured
    dict. The format is documented in `git help status` under
    "Porcelain Format Version 2" — stable across git versions and
    designed for machine consumption.

    Header lines start with `#`:
        # branch.oid  <commit>
        # branch.head <branch-name>            (or "(detached)")
        # branch.upstream <upstream-name>      (optional)
        # branch.ab +<ahead> -<behind>         (optional, when upstream is set)

    Then per-file lines:
        1 <XY> ...    -> tracked, ordinary changes (X=staged, Y=worktree)
        2 <XY> ...    -> tracked, renamed/copied
        u <XY> ...    -> unmerged
        ? <path>      -> untracked
        ! <path>      -> ignored (we never request these via --untracked-files=normal)
    """
    out: dict[str, Any] = {
        "branch": None,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "clean": True,
        "staged": [],
        "modified": [],
        "untracked": [],
        "unmerged": [],
    }
    # branch.oid / branch.head order in porcelain v2 output is not
    # documented as stable. Track unborn state with a flag and apply
    # the normalisation after the loop so it wins regardless of order.
    is_unborn = False
    # split("\n") rather than splitlines(): splitlines() also breaks on
    # \r / \v / \f / U+0085 / U+2028 / U+2029, any of which could appear
    # inside a path on platforms / repositories that allow them, causing
    # one logical record to fragment into multiple "lines" that each
    # fail the per-line guards and get silently dropped.
    for line in text.split("\n"):
        if not line:
            continue
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head ") :].strip()
            # Porcelain v2 emits "(detached)" for detached HEAD; the
            # `git rev-parse --abbrev-ref HEAD` path emits the literal
            # string "HEAD" for the same state. Normalise to "HEAD" here
            # so cross-checks between git_current_branch and git_status
            # never disagree on the same underlying state.
            out["branch"] = "HEAD" if branch == "(detached)" else branch
        elif line.startswith("# branch.oid "):
            # On a freshly `git init`'d repo with no commits, branch.oid
            # is "(initial)" — flag for the end-of-loop normalisation.
            oid = line[len("# branch.oid ") :].strip()
            if oid == "(initial)":
                is_unborn = True
        elif line.startswith("# branch.upstream "):
            out["upstream"] = line[len("# branch.upstream ") :].strip()
        elif line.startswith("# branch.ab "):
            # Format: "# branch.ab +N -M" per porcelain v2 spec. Validate
            # strictly: must be exactly "+<digits> -<digits>" with no
            # double-sign smuggling (the previous startswith-only check
            # accepted "+-3" → int("-3") → ahead=-3, contradicting the
            # parser's own promise to reject negative values).
            parts = line[len("# branch.ab ") :].split()
            if (
                len(parts) == 2
                and parts[0].startswith("+")
                and parts[1].startswith("-")
                and parts[0][1:].isdigit()
                and parts[1][1:].isdigit()
            ):
                # Parse both into locals first, commit only on full
                # success. Non-atomic try/except previously could leave
                # ahead updated while behind silently kept the default
                # 0, producing an internally-inconsistent dict on a
                # half-malformed line like "+5 -junk".
                ahead = int(parts[0][1:])
                behind = int(parts[1][1:])
                out["ahead"] = ahead
                out["behind"] = behind
        elif line.startswith("1 "):
            # Ordinary tracked file. Format:
            #   1 XY <sub> <mH> <mI> <mW> <hH> <hI> <path>
            # 9 space-separated tokens; path is the last one.
            tokens = line.split(" ", 8)
            if len(tokens) < 9:
                continue
            xy, path = tokens[1], tokens[8]
            if len(xy) != 2:
                continue  # malformed XY field
            staged_status, worktree_status = xy[0], xy[1]
            if staged_status != ".":
                out["staged"].append(path)
            if worktree_status != ".":
                out["modified"].append(path)
        elif line.startswith("2 "):
            # Renamed/copied tracked file. Format:
            #   2 XY <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <new-path>\t<orig-path>
            # 10 space-separated tokens; the new path comes after the
            # rename-score (e.g. "R100") and is tab-separated from the
            # original path. Keep only the new path (matches the bash
            # `git status` display default).
            tokens = line.split(" ", 9)
            if len(tokens) < 10:
                continue
            xy, path_field = tokens[1], tokens[9]
            if len(xy) != 2:
                continue
            path = path_field.split("\t", 1)[0]
            staged_status, worktree_status = xy[0], xy[1]
            if staged_status != ".":
                out["staged"].append(path)
            if worktree_status != ".":
                out["modified"].append(path)
        elif line.startswith("u "):
            # Unmerged. Format:
            #   u XY <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>
            # XY width check parity with type-1 and type-2 paths.
            tokens = line.split(" ", 10)
            if len(tokens) >= 11:
                xy = tokens[1]
                if len(xy) != 2:
                    continue
                out["unmerged"].append(tokens[10])
        elif line.startswith("? "):
            # Untracked. Strip the "? " prefix; skip if the remainder is
            # empty (would otherwise append "" and flip clean=False with
            # a phantom entry).
            path = line[2:]
            if path:
                out["untracked"].append(path)
        # `! ignored` and any other prefixes are ignored intentionally.

    if is_unborn:
        # Override any branch.head value (which would be the would-be
        # branch name on the unborn line, e.g. "main") with the HEAD
        # sentinel so cross-checks with git_current_branch agree on
        # "this is a weird state, not a regular branch."
        out["branch"] = "HEAD"
    out["clean"] = (
        not out["staged"]
        and not out["modified"]
        and not out["untracked"]
        and not out["unmerged"]
    )
    # Cap each file-list field and surface the omitted count in a
    # sibling field. A repo with millions of untracked files
    # (forgot-to-gitignore-node_modules onboarding bug) would otherwise
    # return all of them across MCP.
    for key in ("staged", "modified", "untracked", "unmerged"):
        out[key], out[f"{key}_omitted"] = _truncated_path_list(out[key])
    return out


def git_status(
    path: str | os.PathLike[str] | None = None,
    *,
    runner: Any = subprocess,
) -> dict[str, Any]:
    """Return a structured snapshot of the working-tree state.

    Returned dict shape:

        {
            "branch":     "feat/widget" | "HEAD" (detached or unborn),
            "upstream":   "origin/feat/widget" | None,
            "ahead":      0,
            "behind":     0,
            "clean":      True/False,
            "staged":     [path, ...],
            "modified":   [path, ...],
            "untracked":  [path, ...],
            "unmerged":   [path, ...],
            "staged_omitted":    0,
            "modified_omitted":  0,
            "untracked_omitted": 0,
            "unmerged_omitted":  0,
        }

    `clean` is True iff there are no staged, modified, untracked, or
    unmerged entries. `ahead`/`behind` are zero when no upstream is set
    or when the branch is in sync.

    `branch` is the literal string "HEAD" for both detached and unborn
    state (matching what git_current_branch returns for the same
    states), so cross-checks between the two functions agree on "this
    is a weird state, not a regular branch."

    Each file-list field is capped at `_MAX_PATH_LIST` entries; the
    sibling `*_omitted` field carries the count of entries that were
    dropped (0 when the list fits under the cap). The list itself
    contains only real paths — no sentinel marker — so callers
    iterating with `os.stat` etc. don't trip on a non-path entry.

    Known limitation: pathnames containing newlines, tabs, double-quotes,
    or non-ASCII control characters are returned in git's C-quoted form
    (e.g. `weird\\"name.py` instead of `weird"name.py`) because we use
    the line-oriented porcelain=v2 output. Switching to `-z` + NUL-split
    would be the robust fix; deferred until a real bug report shows up
    (this affects 0% of file names in typical use). The same limitation
    applies to git_uncommitted_changes() which parses `git ls-files`
    output in line-oriented mode.
    """
    text = _run_git(
        ["status", "--porcelain=v2", "--branch", "--untracked-files=normal"],
        path=path,
        runner=runner,
    )
    return _parse_status_porcelain_v2(text)


# ---------------------------------------------------------------------------
# Recent commits
# ---------------------------------------------------------------------------


# Unit Separator (0x1F) — a control character that almost never appears
# in commit subjects, author names, or dates in practice. git stores
# arbitrary bytes, so a commit message could technically contain
# U+001F; the parser handles malformed lines defensively below by
# skipping them. The trade-off accepted here is "rare data loss on a
# pathological commit" vs "a robust separator that won't collide with
# common subject content like pipes, tabs, or colons." A NUL-separated
# `git log -z --pretty=...%x00` shape is the genuinely safe variant if
# this ever bites a real user.
_LOG_FIELD_SEP = "\x1f"

# Upper bound on `git log -n<count>` requests. Without this, an agent
# that confabulates `count=1_000_000` would silently pull a million
# commit records into memory and back across the MCP boundary. 1000 is
# generous for any "show me recent activity" workflow.
_MAX_LOG_COUNT = 1000


def git_recent_commits(
    path: str | os.PathLike[str] | None = None,
    *,
    count: int = 10,
    ref: str = "HEAD",
    runner: Any = subprocess,
) -> list[dict[str, Any]]:
    """Return the most recent `count` commits reachable from `ref`.

    Each entry:

        {
            "sha":      "<full 40-char hash>",
            "short":    "<7-char abbreviated hash>",
            "subject":  "<commit subject line>",
            "author":   "<author display name>",
            "date":     "<ISO 8601 author date>",
        }

    The format string uses U+001F (Unit Separator) as the field
    delimiter — a control character that cannot appear in commit
    subjects, so we never have to escape or parse-around content.
    """
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError(f"count must be a positive int, got {count!r}")
    if count > _MAX_LOG_COUNT:
        raise ValueError(
            f"count must be <= {_MAX_LOG_COUNT}, got {count!r}"
        )
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError(f"ref must be a non-empty string, got {ref!r}")
    # Reject refs starting with '-' so an agent-supplied ref can't smuggle
    # a git option flag (e.g. `ref='--all'` would replace the explicit
    # ref with a glob, `ref='-h'` would print help). The `--` terminator
    # below also closes the same hole structurally; the explicit check
    # produces a clearer error than letting git fail downstream.
    stripped_ref = ref.strip()
    if stripped_ref.startswith("-"):
        raise ValueError(
            f"ref must not start with '-' (would be parsed as a git option), got {ref!r}"
        )

    pretty = _LOG_FIELD_SEP.join(["%H", "%h", "%s", "%an", "%aI"])
    # `--` terminator separates options from positional arguments. Even
    # if a future caller's ref slipped a leading '-' past the check, git
    # would interpret everything after `--` as a pathspec or ref, not a
    # flag. Belt and suspenders.
    text = _run_git(
        ["log", f"--pretty=format:{pretty}", f"-n{count}", stripped_ref, "--"],
        path=path,
        runner=runner,
    )

    commits: list[dict[str, Any]] = []
    # split("\n") rather than splitlines(): splitlines() treats \r / \v /
    # \f / U+0085 / U+2028 / U+2029 as record terminators too, so a
    # commit subject containing \r (legal in git; happens when an author
    # pastes Windows-line-ended text into `git commit -F`) would
    # fragment into two "lines" that each fail the parts-count check
    # and the entire commit would silently vanish from the result.
    for line in text.split("\n"):
        if not line:
            continue
        parts = line.split(_LOG_FIELD_SEP)
        if len(parts) != 5:
            # Malformed line — skip rather than crash. Either git
            # changed format unexpectedly or a commit message contained
            # the separator character. Defensive but quiet.
            continue
        sha, short, subject, author, date = parts
        # Reject all-empty (or no-SHA) records — a line of pure
        # separators ("\x1f\x1f\x1f\x1f") would otherwise pass the
        # parts-count guard and append a degenerate
        # {"sha": "", "short": "", ...} entry.
        if not sha:
            continue
        commits.append(
            {
                "sha": sha,
                "short": short,
                "subject": subject,
                "author": author,
                "date": date,
            }
        )
    return commits


# ---------------------------------------------------------------------------
# Uncommitted changes (staged diff + working diff + untracked file list)
# ---------------------------------------------------------------------------


# Maximum bytes returned per diff. A stray multi-GB generated file
# (binary blob, vendored deps, ML model checkpoint) accidentally staged
# in a monorepo would otherwise materialise the entire diff in RAM and
# ship it across the MCP boundary as a single string — almost certainly
# OOM-killing the MCP server. 1 MiB per diff is generous for any
# realistic code change and bounded enough that an agent receiving a
# truncated diff can ask for a `--stat` summary instead.
_MAX_DIFF_BYTES = 1024 * 1024
_DIFF_TRUNCATION_MARKER = (
    "\n\n[... diff truncated by bb MCP server: exceeded "
    f"{_MAX_DIFF_BYTES} bytes. Use `git diff --stat` for a summary.]\n"
)

# Maximum number of path entries returned per file-list field
# (staged / modified / untracked / unmerged + git_uncommitted_changes'
# untracked_files). A repo that forgot to gitignore node_modules /
# .venv / target/ commonly has hundreds of thousands of untracked
# paths; capture_output materialises the full stdout in RAM and the
# JSON serialisation across MCP would amplify the cost. Matches the
# same defensive bound as _MAX_DIFF_BYTES / _MAX_LOG_COUNT.
_MAX_PATH_LIST = 10_000


def _truncated_path_list(paths: list[str]) -> tuple[list[str], int]:
    """Cap a path list at `_MAX_PATH_LIST` entries.

    Returns `(truncated_list, omitted_count)`. The truncated list
    contains ONLY real paths — no sentinel string — so callers
    iterating it with `os.stat` / `Path.exists()` / `os.path.join`
    don't trip on a non-path entry. The omitted count goes into a
    sibling `*_omitted` field on the parent dict so the agent can
    detect truncation explicitly and fall back to a narrower query.
    """
    if len(paths) <= _MAX_PATH_LIST:
        return paths, 0
    return paths[:_MAX_PATH_LIST], len(paths) - _MAX_PATH_LIST


def _cap_diff(text: str) -> str:
    """Truncate a diff string to `_MAX_DIFF_BYTES` if it exceeds the cap,
    appending an explicit marker so the caller knows what happened.

    Fast path on `text.isascii()`: ASCII-only strings have byte count
    equal to char count, so a `len(text) <= cap` check is sound. For
    non-ASCII content (CJK, emoji, accented Latin) the bytes-vs-chars
    ratio can be 2-4x, so we have to encode-and-measure.

    The prior round's fast path (`if len(text) <= cap: return text`)
    was wrong by inversion: UTF-8 byte count is always >= char count,
    so `chars <= cap` does NOT imply `bytes <= cap`. A 600K-emoji
    string would pass the fast path but encode to 2.4 MiB.
    """
    # ASCII fast path: chars == bytes, so the cheap len check is sound.
    if text.isascii() and len(text) <= _MAX_DIFF_BYTES:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= _MAX_DIFF_BYTES:
        return text
    # Truncate at the byte cap then decode; errors="replace" handles the
    # case where we sliced a multibyte character in half.
    truncated = encoded[:_MAX_DIFF_BYTES].decode("utf-8", errors="replace")
    return truncated + _DIFF_TRUNCATION_MARKER


def git_uncommitted_changes(
    path: str | os.PathLike[str] | None = None,
    *,
    runner: Any = subprocess,
) -> dict[str, Any]:
    """Return everything that hasn't been committed yet.

    Returned dict:

        {
            "staged_diff":             "<git diff --cached output>",
            "working_diff":            "<git diff output>",
            "untracked_files":         [path, ...],
            "untracked_files_omitted": 0,
        }

    Diff strings and the untracked list may be empty (`""` / `""` /
    `[]`) when the working tree is clean. Diffs are returned as raw
    unified-diff text so callers can either show them verbatim or
    parse them further.

    Each diff is capped at `_MAX_DIFF_BYTES` (1 MiB) plus a small
    truncation marker; diffs that exceed the cap are truncated and
    the marker tells the caller (typically the MCP agent) to fall
    back to `git diff --stat` or a path-narrowed diff. The
    `untracked_files` list is capped at `_MAX_PATH_LIST` entries;
    the `untracked_files_omitted` sibling field carries the count
    of paths that were dropped (0 when the list fits).
    """
    staged_diff = _cap_diff(
        _run_git(["diff", "--cached"], path=path, runner=runner)
    )
    working_diff = _cap_diff(_run_git(["diff"], path=path, runner=runner))
    untracked_text = _run_git(
        ["ls-files", "--others", "--exclude-standard"],
        path=path,
        runner=runner,
    )
    # split("\n") for the same reason as git_status / git_recent_commits:
    # avoid splitlines() collapsing paths that contain \r etc.
    untracked = [line for line in untracked_text.split("\n") if line]
    untracked_capped, untracked_omitted = _truncated_path_list(untracked)
    return {
        "staged_diff": staged_diff,
        "working_diff": working_diff,
        "untracked_files": untracked_capped,
        "untracked_files_omitted": untracked_omitted,
    }
