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
import subprocess
from typing import Any

from bb_api import parse_remote_url


class GitOpError(RuntimeError):
    """Raised when a `git` invocation fails or returns unparseable output.

    Carries the failing command's stderr (truncated in the message) and
    the original return code so callers branching on git semantics can
    inspect them. A separate exception class from `BBApiError` so MCP
    tools can render "git failure" vs "Bitbucket failure" differently.
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
    without monkey-patching the module."""
    cmd = ["git", *args]
    cwd = str(path) if path is not None else None
    try:
        result = runner.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
    except FileNotFoundError as e:
        raise GitOpError(cmd, 127, "git executable not found on PATH") from e

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

    Detached HEAD returns the literal string `"HEAD"` — same shape as
    `git rev-parse --abbrev-ref HEAD` produces, with no special-case
    handling. Callers that need to distinguish "on a branch" from
    "detached" check for `"HEAD"` explicitly.
    """
    out = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], path=path, runner=runner
    )
    branch = out.strip()
    if not branch:
        raise GitOpError(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            0,
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
        raise GitOpError(
            ["git", "remote", "get-url", "origin"],
            0,
            f"could not parse workspace/repo from origin URL: {url.strip()!r}",
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
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            out["branch"] = line[len("# branch.head ") :].strip()
        elif line.startswith("# branch.upstream "):
            out["upstream"] = line[len("# branch.upstream ") :].strip()
        elif line.startswith("# branch.ab "):
            # Format: "# branch.ab +N -M"
            parts = line[len("# branch.ab ") :].split()
            if len(parts) == 2:
                try:
                    out["ahead"] = int(parts[0].lstrip("+"))
                    out["behind"] = int(parts[1].lstrip("-"))
                except ValueError:
                    pass  # leave defaults
        elif line.startswith("1 "):
            # Ordinary tracked file. Format:
            #   1 XY <sub> <mH> <mI> <mW> <hH> <hI> <path>
            # 9 space-separated tokens; path is the last one.
            tokens = line.split(" ", 8)
            if len(tokens) < 9:
                continue
            xy, path = tokens[1], tokens[8]
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
            path = path_field.split("\t", 1)[0]
            staged_status, worktree_status = xy[0], xy[1]
            if staged_status != ".":
                out["staged"].append(path)
            if worktree_status != ".":
                out["modified"].append(path)
        elif line.startswith("u "):
            tokens = line.split(" ", 10)
            if len(tokens) >= 11:
                out["unmerged"].append(tokens[10])
        elif line.startswith("? "):
            out["untracked"].append(line[2:])
        # `! ignored` and any other prefixes are ignored intentionally.

    out["clean"] = (
        not out["staged"]
        and not out["modified"]
        and not out["untracked"]
        and not out["unmerged"]
    )
    return out


def git_status(
    path: str | os.PathLike[str] | None = None,
    *,
    runner: Any = subprocess,
) -> dict[str, Any]:
    """Return a structured snapshot of the working-tree state.

    Returned dict shape:

        {
            "branch": "feat/widget" | "(detached)",
            "upstream": "origin/feat/widget" | None,
            "ahead": 0,
            "behind": 0,
            "clean": True/False,
            "staged":    [path, ...],
            "modified":  [path, ...],
            "untracked": [path, ...],
            "unmerged":  [path, ...],
        }

    `clean` is True iff there are no staged, modified, untracked, or
    unmerged entries. `ahead`/`behind` are zero when no upstream is set
    or when the branch is in sync.
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


# Unit Separator (0x1F) — a control character that cannot appear in commit
# subjects, author names, or dates. Using it as the field separator means
# we don't have to worry about subject lines containing pipes / tabs /
# whatever-else-the-author-felt-like.
_LOG_FIELD_SEP = "\x1f"


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
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError(f"ref must be a non-empty string, got {ref!r}")

    pretty = _LOG_FIELD_SEP.join(["%H", "%h", "%s", "%an", "%aI"])
    text = _run_git(
        ["log", f"--pretty=format:{pretty}", f"-n{count}", ref],
        path=path,
        runner=runner,
    )

    commits: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split(_LOG_FIELD_SEP)
        if len(parts) != 5:
            # Malformed line — skip rather than crash. A real git output
            # would never emit this; if we see it, something injected the
            # separator into a field (extremely unlikely with U+001F).
            continue
        sha, short, subject, author, date = parts
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


def git_uncommitted_changes(
    path: str | os.PathLike[str] | None = None,
    *,
    runner: Any = subprocess,
) -> dict[str, Any]:
    """Return everything that hasn't been committed yet.

    Returned dict:

        {
            "staged_diff":      "<git diff --cached output>",
            "working_diff":     "<git diff output>",
            "untracked_files":  [path, ...],
        }

    All three may be empty (`""` / `""` / `[]`) when the working tree
    is clean. Diffs are returned as raw unified-diff text so callers
    can either show them verbatim or parse them further.
    """
    staged_diff = _run_git(["diff", "--cached"], path=path, runner=runner)
    working_diff = _run_git(["diff"], path=path, runner=runner)
    untracked_text = _run_git(
        ["ls-files", "--others", "--exclude-standard"],
        path=path,
        runner=runner,
    )
    untracked = [line for line in untracked_text.splitlines() if line]
    return {
        "staged_diff": staged_diff,
        "working_diff": working_diff,
        "untracked_files": untracked,
    }
