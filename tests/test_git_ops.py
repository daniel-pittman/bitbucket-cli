"""
Tests for git_ops.

Same discipline as bb_api / bb_ops tests: assert the exact subprocess
invocation (command + args + cwd + kwargs) AND the parsing of canned
realistic git output. Without the subprocess-shape assertions a future
refactor that swapped `git status --porcelain=v2` for the v1 format
would produce different parse results but the parser tests would still
pass on their canned input — exactly the kind of regression the test
discipline is meant to catch.

All fixture data is fictional (workspace `acme`, repo `widget-service`,
authors `alice` / `bob`).
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

import git_ops
from git_ops import GIT_PARSE_ERROR_RETURNCODE, GitOpError


# ---------------------------------------------------------------------------
# Subprocess scaffolding
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """Stand-in for the `subprocess` module that records every .run()
    call and returns canned (returncode, stdout, stderr) per invocation.

    Use the same instance across a single test so we can assert on the
    full sequence of git commands a function issues (some functions
    like git_uncommitted_changes shell out three times)."""

    def __init__(self, responses: list[tuple[int, str, str]]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def run(self, args: Any, **kwargs: Any) -> Any:
        self.calls.append({"args": args, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError(
                f"runner received an unexpected call: {args!r}"
            )
        returncode, stdout, stderr = self.responses.pop(0)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _MissingGitRunner:
    """Stand-in that raises FileNotFoundError on .run() — simulates
    `git` not being on PATH. Tests use this to verify the error wrap."""

    @staticmethod
    def run(*_args: Any, **_kwargs: Any) -> Any:
        e = FileNotFoundError("[Errno 2] No such file or directory: 'git'")
        e.filename = "git"
        raise e


class _MissingCwdRunner:
    """Stand-in that raises FileNotFoundError with the cwd as the
    filename — simulates `path=/no/such/dir` passed to a git wrapper."""

    def __init__(self, cwd: str):
        self.cwd = cwd

    def run(self, *_args: Any, **kwargs: Any) -> Any:
        e = FileNotFoundError(f"[Errno 2] No such file or directory: '{self.cwd}'")
        e.filename = kwargs.get("cwd") or self.cwd
        raise e


# ===========================================================================
# git_current_branch
# ===========================================================================


class TestGitCurrentBranch:
    def test_returns_branch_name(self) -> None:
        runner = _RecordingRunner([(0, "feat/widget\n", "")])
        assert git_ops.git_current_branch(runner=runner) == "feat/widget"
        # Assert the exact subprocess shape — `rev-parse --abbrev-ref HEAD`
        # is the contract. A regression to `git branch --show-current`
        # (different command, different edge-case behaviour on detached HEAD)
        # would silently pass the canned-output test without this.
        # The wrapper prepends `-c color.ui=never` to disable ANSI escapes
        # in any output (relevant for diff/log paths, prepended uniformly
        # for consistency).
        assert runner.calls[0]["args"] == [
            "git",
            "-c",
            "color.ui=never",
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
        ]
        kwargs = runner.calls[0]["kwargs"]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        # Explicit UTF-8 + replace on decode errors so non-ASCII filenames
        # or author names don't crash inside subprocess.run on a
        # locale-restricted host.
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        # Timeout so a wedged git can't hang the MCP server.
        assert kwargs["timeout"] == git_ops._GIT_SUBPROCESS_TIMEOUT
        # stdin=DEVNULL so a credential prompt fails immediately with
        # EOF rather than blocking on inherited stdin.
        assert kwargs["stdin"] == subprocess.DEVNULL
        # GIT_TERMINAL_PROMPT=0 + GIT_ASKPASS="" in the environment so
        # git itself refuses to prompt (defence in depth alongside
        # stdin=DEVNULL).
        env = kwargs["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"] == ""

    def test_passes_cwd_when_path_given(self) -> None:
        runner = _RecordingRunner([(0, "main\n", "")])
        git_ops.git_current_branch(path="/some/dir", runner=runner)
        assert runner.calls[0]["kwargs"]["cwd"] == "/some/dir"

    def test_detached_head_returns_literal_string(self) -> None:
        # On detached HEAD, `git rev-parse --abbrev-ref HEAD` returns "HEAD".
        # Callers that need to detect detached state check for this literal;
        # we don't special-case it inside the function.
        runner = _RecordingRunner([(0, "HEAD\n", "")])
        assert git_ops.git_current_branch(runner=runner) == "HEAD"

    def test_non_git_dir_raises_giterror(self) -> None:
        runner = _RecordingRunner(
            [(128, "", "fatal: not a git repository (or any of the parent directories): .git\n")]
        )
        with pytest.raises(GitOpError, match="not a git repository"):
            git_ops.git_current_branch(runner=runner)

    def test_missing_git_binary_raises_giterror(self) -> None:
        with pytest.raises(GitOpError, match="git executable not found"):
            git_ops.git_current_branch(runner=_MissingGitRunner)

    def test_missing_cwd_raises_distinct_error(self) -> None:
        """When `path=` points to a non-existent directory,
        subprocess.run raises FileNotFoundError with e.filename set to
        the cwd. Disambiguate from the missing-git case so the agent
        sees the actual cause instead of chasing a PATH config."""
        with pytest.raises(GitOpError, match="path does not exist"):
            git_ops.git_current_branch(
                path="/no/such/dir",
                runner=_MissingCwdRunner("/no/such/dir"),
            )

    def test_timeout_raises_giterror_with_parse_returncode(self) -> None:
        """A wedged git (credential-helper prompting on stdin, held
        index.lock, NFS server gone) would otherwise hang the MCP
        server thread. subprocess.TimeoutExpired must be wrapped as
        GitOpError so callers see the uniform error surface."""

        class _TimeoutRunner:
            @staticmethod
            def run(*_args: Any, **kwargs: Any) -> Any:
                raise subprocess.TimeoutExpired(
                    cmd=kwargs.get("args") or "git",
                    timeout=kwargs.get("timeout", 30.0),
                )

        with pytest.raises(GitOpError, match="timed out") as exc:
            git_ops.git_current_branch(runner=_TimeoutRunner)
        # Timeout uses the parse-error sentinel (no real git exit code).
        assert exc.value.returncode == GIT_PARSE_ERROR_RETURNCODE

    def test_empty_stdout_raises_with_parse_returncode(self) -> None:
        # rev-parse should never return empty on a healthy repo; if it
        # does, fail loud rather than returning "" as a branch name.
        # The sentinel returncode (-1) distinguishes parse failure from
        # git's own exit codes for callers branching on returncode.
        runner = _RecordingRunner([(0, "\n", "")])
        with pytest.raises(GitOpError, match="empty branch name") as exc:
            git_ops.git_current_branch(runner=runner)
        assert exc.value.returncode == GIT_PARSE_ERROR_RETURNCODE
        assert exc.value.returncode < 0  # any real git exit is >= 0


# ===========================================================================
# git_remote_repo
# ===========================================================================


class TestGitRemoteRepo:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://bitbucket.org/acme/widget-service.git\n", ("acme", "widget-service")),
            ("git@bitbucket.org:acme/widget-service.git\n", ("acme", "widget-service")),
            ("https://alice@bitbucket.org/acme/widget-service\n", ("acme", "widget-service")),
            # Self-hosted Bitbucket Server — parser is intentionally loose,
            # matches bb_api.parse_remote_url's documented contract.
            ("https://bitbucket.example.com/acme/widget-service.git\n", ("acme", "widget-service")),
        ],
    )
    def test_parses_known_remote_shapes(
        self, url: str, expected: tuple[str, str]
    ) -> None:
        runner = _RecordingRunner([(0, url, "")])
        assert git_ops.git_remote_repo(runner=runner) == expected

    def test_subprocess_shape(self) -> None:
        runner = _RecordingRunner(
            [(0, "https://bitbucket.org/acme/widget-service.git\n", "")]
        )
        git_ops.git_remote_repo(runner=runner)
        assert runner.calls[0]["args"] == [
            "git",
            "-c",
            "color.ui=never",
            "remote",
            "get-url",
            "origin",
        ]

    def test_passes_cwd_when_path_given(self) -> None:
        runner = _RecordingRunner(
            [(0, "https://bitbucket.org/acme/widget-service.git\n", "")]
        )
        git_ops.git_remote_repo(path="/work/dir", runner=runner)
        assert runner.calls[0]["kwargs"]["cwd"] == "/work/dir"

    def test_no_origin_remote_raises(self) -> None:
        runner = _RecordingRunner(
            [(128, "", "error: No such remote 'origin'\n")]
        )
        with pytest.raises(GitOpError, match="No such remote"):
            git_ops.git_remote_repo(runner=runner)

    def test_unparseable_url_raises_with_parse_returncode(self) -> None:
        runner = _RecordingRunner([(0, "not-a-url\n", "")])
        with pytest.raises(GitOpError, match="could not parse") as exc:
            git_ops.git_remote_repo(runner=runner)
        # Parse failure: git exited 0 but we couldn't make sense of the
        # output. Sentinel returncode distinguishes this from a real git
        # failure (which carries git's own non-zero exit code).
        assert exc.value.returncode == GIT_PARSE_ERROR_RETURNCODE

    def test_unparseable_url_redacts_embedded_credentials(self) -> None:
        """If the unparseable URL carries `user:token@` embedded auth
        (common in CI: https://x-token-auth:abcd@.../), the secret
        must NOT land in the error message — it would flow through
        MCP into agent context and downstream logs."""
        # Construct an unparseable URL (parse_remote_url's regex needs
        # a `[:/]X/Y` tail; "host-only" doesn't match).
        sensitive = "https://x-token-auth:supersecret123@bitbucket.org\n"
        runner = _RecordingRunner([(0, sensitive, "")])
        with pytest.raises(GitOpError) as exc:
            git_ops.git_remote_repo(runner=runner)
        msg = str(exc.value)
        assert "supersecret123" not in msg
        assert "x-token-auth" not in msg
        assert "[redacted]" in msg


# ===========================================================================
# git_status (parser + driver)
# ===========================================================================


# Realistic `git status --porcelain=v2 --branch --untracked-files=normal`
# captures, exercised against the parser.

STATUS_CLEAN = """\
# branch.oid 0a1b2c3d4e5f6789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab +0 -0
"""

STATUS_AHEAD = """\
# branch.oid 0a1b2c3d4e5f6789abcdef0123456789abcdef01
# branch.head feat/widget
# branch.upstream origin/feat/widget
# branch.ab +3 -1
"""

STATUS_NO_UPSTREAM = """\
# branch.oid 0a1b2c3d4e5f6789abcdef0123456789abcdef01
# branch.head feat/local-only
"""

STATUS_MIXED_CHANGES = """\
# branch.oid 0a1b2c3d4e5f6789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab +0 -0
1 M. N... 100644 100644 100644 hash1 hash1 staged_file.py
1 .M N... 100644 100644 100644 hash1 hash1 modified_file.py
1 MM N... 100644 100644 100644 hash1 hash1 both_staged_and_modified.py
? untracked.tmp
? docs/new_note.md
"""

STATUS_RENAMED = """\
# branch.oid 0a1b2c3d4e5f6789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab +0 -0
2 R. N... 100644 100644 100644 hash1 hash1 R100 new_name.py\told_name.py
"""

STATUS_UNMERGED = """\
# branch.oid 0a1b2c3d4e5f6789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab +0 -0
u UU N... 100644 100644 100644 100644 hash1 hash2 hash3 conflict.py
"""

# Type-1 line with spaces in the filename. The path is the 9th token
# (everything from index 8 onward) so split(" ", 8) preserves the spaces.
STATUS_SPACE_IN_FILENAME = """\
# branch.oid 0a1b2c3d4e5f6789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab +0 -0
1 .M N... 100644 100644 100644 hash1 hash1 docs/My Cool File.md
"""

# Porcelain v2 emits "(detached)" for detached HEAD. The parser
# normalises this to "HEAD" so it matches what git_current_branch
# returns for the same state — cross-checks between the two never
# disagree on the same underlying state.
STATUS_DETACHED = """\
# branch.oid 0a1b2c3d4e5f6789abcdef0123456789abcdef01
# branch.head (detached)
"""

# A malformed branch.ab line (negative ahead, positive behind — never
# emitted by real git but defensive). The parser should ignore it
# (leave ahead/behind at 0).
STATUS_BAD_AB = """\
# branch.oid 0a1b2c3d4e5f6789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab -3 +1
"""

# Type-1 line with a single-char XY field (corrupted output). The
# parser must skip rather than IndexError.
STATUS_CORRUPT_XY = """\
# branch.oid 0a1b2c3d4e5f6789abcdef0123456789abcdef01
# branch.head main
1 X N... 100644 100644 100644 hash1 hash1 single_char_xy.py
"""

# Freshly `git init`'d repo with no commits — branch.head reports the
# would-be branch (e.g. "main") but branch.oid is "(initial)" signaling
# unborn state. Normalise to "HEAD" for symmetry with the detached-HEAD
# convention.
STATUS_UNBORN = """\
# branch.oid (initial)
# branch.head main
? README.md
"""


class TestGitStatusParser:
    def test_clean_tree(self) -> None:
        s = git_ops._parse_status_porcelain_v2(STATUS_CLEAN)
        assert s["branch"] == "main"
        assert s["upstream"] == "origin/main"
        assert s["ahead"] == 0
        assert s["behind"] == 0
        assert s["clean"] is True
        assert s["staged"] == []
        assert s["modified"] == []
        assert s["untracked"] == []
        assert s["unmerged"] == []

    def test_ahead_behind(self) -> None:
        s = git_ops._parse_status_porcelain_v2(STATUS_AHEAD)
        assert s["ahead"] == 3
        assert s["behind"] == 1
        assert s["clean"] is True  # ahead/behind alone doesn't make tree dirty

    def test_no_upstream(self) -> None:
        s = git_ops._parse_status_porcelain_v2(STATUS_NO_UPSTREAM)
        assert s["branch"] == "feat/local-only"
        assert s["upstream"] is None
        assert s["ahead"] == 0
        assert s["behind"] == 0

    def test_mixed_changes(self) -> None:
        s = git_ops._parse_status_porcelain_v2(STATUS_MIXED_CHANGES)
        assert s["staged"] == ["staged_file.py", "both_staged_and_modified.py"]
        assert s["modified"] == ["modified_file.py", "both_staged_and_modified.py"]
        assert s["untracked"] == ["untracked.tmp", "docs/new_note.md"]
        assert s["clean"] is False

    def test_renamed_keeps_new_path(self) -> None:
        # Renamed entries in porcelain=v2 use a tab to separate new from old;
        # we keep the new path only (matches what `git status` displays
        # by default).
        s = git_ops._parse_status_porcelain_v2(STATUS_RENAMED)
        assert s["staged"] == ["new_name.py"]
        assert s["clean"] is False

    def test_unmerged(self) -> None:
        s = git_ops._parse_status_porcelain_v2(STATUS_UNMERGED)
        assert s["unmerged"] == ["conflict.py"]
        assert s["clean"] is False

    def test_spaces_in_filename_preserved(self) -> None:
        """Type-1 line uses `split(" ", 8)` so spaces in the filename
        are preserved by collapsing everything from index 8 onward into
        the path token. Regression guard for this parsing choice."""
        s = git_ops._parse_status_porcelain_v2(STATUS_SPACE_IN_FILENAME)
        assert s["modified"] == ["docs/My Cool File.md"]
        assert s["clean"] is False

    def test_detached_head_normalised_to_HEAD(self) -> None:
        """Porcelain v2 emits "(detached)"; we normalise to "HEAD" so
        cross-checks with git_current_branch (which always returns
        "HEAD" for detached) agree on the same underlying state."""
        s = git_ops._parse_status_porcelain_v2(STATUS_DETACHED)
        assert s["branch"] == "HEAD"

    def test_bad_branch_ab_format_falls_back_to_zero(self) -> None:
        """Sign-validated parsing rejects malformed branch.ab lines
        (negative ahead, positive behind) — leaves ahead/behind at 0
        rather than propagating bogus values into the MCP layer."""
        s = git_ops._parse_status_porcelain_v2(STATUS_BAD_AB)
        assert s["ahead"] == 0
        assert s["behind"] == 0

    def test_corrupt_xy_field_skipped(self) -> None:
        """Type-1 line with single-char XY (corrupted output) skipped
        defensively rather than raising IndexError on `xy[1]`."""
        s = git_ops._parse_status_porcelain_v2(STATUS_CORRUPT_XY)
        assert s["staged"] == []
        assert s["modified"] == []

    def test_unborn_branch_normalised_to_HEAD(self) -> None:
        """branch.oid (initial) signals unborn state. Normalising to
        "HEAD" gives consistent "weird state" signaling alongside the
        detached-HEAD convention — git_current_branch raises on the
        same repo, so the two functions agree they can't give you a
        regular branch name."""
        s = git_ops._parse_status_porcelain_v2(STATUS_UNBORN)
        assert s["branch"] == "HEAD"
        assert s["untracked"] == ["README.md"]


class TestGitStatusDriver:
    def test_subprocess_shape(self) -> None:
        runner = _RecordingRunner([(0, STATUS_CLEAN, "")])
        git_ops.git_status(runner=runner)
        # The porcelain=v2 + branch + untracked-files=normal flags are
        # the contract. A regression to porcelain v1 would change every
        # field we parse without breaking the canned-output tests above
        # — assert the args explicitly. `-c color.ui=never` prepended by
        # the _run_git wrapper.
        assert runner.calls[0]["args"] == [
            "git",
            "-c",
            "color.ui=never",
            "status",
            "--porcelain=v2",
            "--branch",
            "--untracked-files=normal",
        ]

    def test_passes_cwd_when_path_given(self) -> None:
        runner = _RecordingRunner([(0, STATUS_CLEAN, "")])
        git_ops.git_status(path="/work/dir", runner=runner)
        assert runner.calls[0]["kwargs"]["cwd"] == "/work/dir"

    def test_end_to_end_clean(self) -> None:
        runner = _RecordingRunner([(0, STATUS_CLEAN, "")])
        s = git_ops.git_status(runner=runner)
        assert s["clean"] is True
        assert s["branch"] == "main"

    def test_end_to_end_dirty(self) -> None:
        runner = _RecordingRunner([(0, STATUS_MIXED_CHANGES, "")])
        s = git_ops.git_status(runner=runner)
        assert s["clean"] is False
        assert len(s["staged"]) == 2
        assert len(s["untracked"]) == 2


# ===========================================================================
# git_recent_commits
# ===========================================================================


# Build realistic log output using the same U+001F separator git_ops uses.
_SEP = "\x1f"


def _log_line(sha: str, short: str, subj: str, author: str, date: str) -> str:
    return _SEP.join([sha, short, subj, author, date])


LOG_THREE_COMMITS = "\n".join(
    [
        _log_line(
            "a" * 40,
            "aaaaaaa",
            "Add widget endpoint",
            "Alice Garcia",
            "2026-05-26T12:00:00-07:00",
        ),
        _log_line(
            "b" * 40,
            "bbbbbbb",
            "Fix pagination off-by-one",
            "Bob Jones",
            "2026-05-25T15:30:00-07:00",
        ),
        _log_line(
            "c" * 40,
            "ccccccc",
            "Refactor: rename helper",
            "Alice Garcia",
            "2026-05-25T10:00:00-07:00",
        ),
    ]
)


class TestGitRecentCommits:
    def test_subprocess_shape(self) -> None:
        runner = _RecordingRunner([(0, LOG_THREE_COMMITS, "")])
        git_ops.git_recent_commits(count=3, runner=runner)
        args = runner.calls[0]["args"]
        # `git -c color.ui=never log ...` — the wrapper prepends the
        # color-suppression even though log doesn't strictly need it
        # in our format, for uniformity.
        assert args[0] == "git"
        assert args[1] == "-c"
        assert args[2] == "color.ui=never"
        assert args[3] == "log"
        # The pretty-format string uses U+001F (Unit Separator) so no
        # subject line / author / date can contain the delimiter — a
        # subject like "fix: split a|b|c" would otherwise break a
        # pipe-delimited format. Pin the format.
        pretty_arg = args[4]
        assert pretty_arg.startswith("--pretty=format:")
        assert "%H" in pretty_arg
        assert "%h" in pretty_arg
        assert "%s" in pretty_arg
        assert "%an" in pretty_arg
        assert "%aI" in pretty_arg  # ISO 8601 strict
        assert _SEP in pretty_arg
        # `-n<count>` shape, not separate args
        assert args[5] == "-n3"
        assert args[6] == "HEAD"
        # `--` terminator separates options from positional refs (defense
        # against an agent-supplied ref that starts with `-`).
        assert args[7] == "--"

    def test_passes_cwd_when_path_given(self) -> None:
        runner = _RecordingRunner([(0, LOG_THREE_COMMITS, "")])
        git_ops.git_recent_commits(path="/work/dir", runner=runner)
        assert runner.calls[0]["kwargs"]["cwd"] == "/work/dir"

    def test_parses_log_output(self) -> None:
        runner = _RecordingRunner([(0, LOG_THREE_COMMITS, "")])
        commits = git_ops.git_recent_commits(count=3, runner=runner)
        assert len(commits) == 3
        assert commits[0] == {
            "sha": "a" * 40,
            "short": "aaaaaaa",
            "subject": "Add widget endpoint",
            "author": "Alice Garcia",
            "date": "2026-05-26T12:00:00-07:00",
        }
        # Author with multi-word name preserved.
        assert commits[1]["author"] == "Bob Jones"
        # Subject containing colon preserved.
        assert commits[2]["subject"] == "Refactor: rename helper"

    def test_alternate_ref(self) -> None:
        runner = _RecordingRunner([(0, LOG_THREE_COMMITS, "")])
        git_ops.git_recent_commits(count=5, ref="origin/main", runner=runner)
        # ref is at index 6 now (after git -c color.ui=never log
        # --pretty=... -n5).
        assert runner.calls[0]["args"][6] == "origin/main"

    def test_empty_output_returns_empty_list(self) -> None:
        # If git ever returns exit 0 + empty stdout, return [] not raise.
        # (Note: a real git on an unborn-branch repo exits 128 — see
        # test_unborn_branch_raises_giterror below.)
        runner = _RecordingRunner([(0, "", "")])
        assert git_ops.git_recent_commits(runner=runner) == []

    def test_unborn_branch_raises_giterror(self) -> None:
        # Real-world behaviour: `git log` on a freshly `git init`'d
        # repo with no commits exits 128 (`fatal: your current branch
        # 'main' does not have any commits yet`). Pin that this
        # surfaces as a GitOpError rather than silently returning [].
        runner = _RecordingRunner(
            [
                (
                    128,
                    "",
                    "fatal: your current branch 'main' does not have any commits yet\n",
                )
            ]
        )
        with pytest.raises(GitOpError, match="does not have any commits"):
            git_ops.git_recent_commits(runner=runner)

    @pytest.mark.parametrize("bad", [0, -1, True, False, "ten"])
    def test_rejects_invalid_count(self, bad: Any) -> None:
        runner = _RecordingRunner([])
        with pytest.raises(ValueError, match="count"):
            git_ops.git_recent_commits(count=bad, runner=runner)
        # No request emitted.
        assert runner.calls == []

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_rejects_empty_ref(self, bad: str) -> None:
        runner = _RecordingRunner([])
        with pytest.raises(ValueError, match="ref"):
            git_ops.git_recent_commits(ref=bad, runner=runner)
        assert runner.calls == []

    @pytest.mark.parametrize(
        "bad_ref",
        ["--all", "-h", "--format=junk", "  --pretty=blah"],
    )
    def test_rejects_ref_starting_with_dash(self, bad_ref: str) -> None:
        """An agent-supplied ref like '--all' would otherwise be parsed
        by git as an option flag, not a ref — silently changing the
        meaning of the call. Reject at the boundary; the `--` terminator
        below is the structural backstop."""
        runner = _RecordingRunner([])
        with pytest.raises(ValueError, match="must not start with '-'"):
            git_ops.git_recent_commits(ref=bad_ref, runner=runner)
        assert runner.calls == []  # no subprocess invocation

    def test_rejects_count_above_cap(self) -> None:
        runner = _RecordingRunner([])
        with pytest.raises(ValueError, match="<="):
            git_ops.git_recent_commits(count=10_001, runner=runner)
        assert runner.calls == []

    def test_count_at_cap_accepted(self) -> None:
        # Verify the cap is inclusive (count == cap should pass), not
        # off-by-one. The cap is _MAX_LOG_COUNT (1000). `-n` arg is at
        # index 5 after the prepended `git -c color.ui=never log
        # --pretty=...` tokens.
        runner = _RecordingRunner([(0, "", "")])
        git_ops.git_recent_commits(count=git_ops._MAX_LOG_COUNT, runner=runner)
        assert runner.calls[0]["args"][5] == f"-n{git_ops._MAX_LOG_COUNT}"

    def test_subject_containing_separator_drops_malformed_line(self) -> None:
        # The U+001F separator should not appear in commit subjects in
        # practice, but if a malformed line shows up we skip it rather
        # than crash. Verify the parser is defensive.
        text = (
            _log_line("a" * 40, "aaaaaaa", "good", "Alice", "2026-05-26T12:00:00Z")
            + "\n"
            + "junk\x1fline\x1fwith\x1fonly\x1f4\x1fparts\x1ftoo many"  # 7 fields
        )
        runner = _RecordingRunner([(0, text, "")])
        commits = git_ops.git_recent_commits(runner=runner)
        # Only the well-formed line yielded.
        assert len(commits) == 1
        assert commits[0]["subject"] == "good"

    def test_pure_separator_line_skipped(self) -> None:
        """A line of pure separators ("\\x1f\\x1f\\x1f\\x1f") splits
        into exactly 5 empty strings, passing the parts-count guard.
        Without the `if not sha: continue` check we'd append a
        degenerate {"sha":"", "short":"", ...} entry."""
        text = "\x1f\x1f\x1f\x1f"  # 4 separators -> 5 empty fields
        runner = _RecordingRunner([(0, text, "")])
        assert git_ops.git_recent_commits(runner=runner) == []

    def test_subject_with_carriage_return_preserved(self) -> None:
        """A commit subject containing \\r (legal in git; happens when
        an author commits with `git commit -F` from a Windows-line-ended
        file) must NOT fragment into two "lines" under splitlines(),
        which would silently drop the entire commit."""
        text = _log_line(
            "a" * 40,
            "aaaaaaa",
            "subject with\rcarriage return",
            "Alice",
            "2026-05-26T12:00:00Z",
        )
        runner = _RecordingRunner([(0, text, "")])
        commits = git_ops.git_recent_commits(runner=runner)
        assert len(commits) == 1
        assert commits[0]["subject"] == "subject with\rcarriage return"


# ===========================================================================
# git_uncommitted_changes
# ===========================================================================


class TestGitUncommittedChanges:
    def test_three_subprocess_calls(self) -> None:
        runner = _RecordingRunner(
            [
                (0, "diff --staged\n", ""),
                (0, "diff --working\n", ""),
                (0, "untracked1.py\nuntracked2.md\n", ""),
            ]
        )
        result = git_ops.git_uncommitted_changes(runner=runner)
        # Verify the EXACT command shape for all three calls. A regression
        # to a single `git status -s` would lose the diff content; pinning
        # each command separately catches that.
        assert runner.calls[0]["args"] == [
            "git",
            "-c",
            "color.ui=never",
            "diff",
            "--cached",
        ]
        assert runner.calls[1]["args"] == [
            "git",
            "-c",
            "color.ui=never",
            "diff",
        ]
        assert runner.calls[2]["args"] == [
            "git",
            "-c",
            "color.ui=never",
            "ls-files",
            "--others",
            "--exclude-standard",
        ]
        assert result == {
            "staged_diff": "diff --staged\n",
            "working_diff": "diff --working\n",
            "untracked_files": ["untracked1.py", "untracked2.md"],
        }

    def test_oversize_diff_truncated_with_marker(self) -> None:
        """A multi-MiB diff (someone accidentally staged a generated
        blob) must NOT be returned in full — would OOM the MCP server.
        Truncation marker tells the caller what happened."""
        big_diff = "+" + ("x" * (git_ops._MAX_DIFF_BYTES + 100))
        runner = _RecordingRunner(
            [
                (0, big_diff, ""),
                (0, "", ""),
                (0, "", ""),
            ]
        )
        result = git_ops.git_uncommitted_changes(runner=runner)
        # The returned staged_diff is at most _MAX_DIFF_BYTES plus the
        # truncation marker length.
        assert len(result["staged_diff"]) <= (
            git_ops._MAX_DIFF_BYTES + len(git_ops._DIFF_TRUNCATION_MARKER)
        )
        assert "truncated by bb MCP server" in result["staged_diff"]
        # Below-cap diffs are returned verbatim.
        small_runner = _RecordingRunner(
            [
                (0, "diff body\n", ""),
                (0, "", ""),
                (0, "", ""),
            ]
        )
        small = git_ops.git_uncommitted_changes(runner=small_runner)
        assert small["staged_diff"] == "diff body\n"

    def test_clean_tree_returns_empties(self) -> None:
        runner = _RecordingRunner(
            [
                (0, "", ""),  # no staged diff
                (0, "", ""),  # no working diff
                (0, "", ""),  # no untracked
            ]
        )
        assert git_ops.git_uncommitted_changes(runner=runner) == {
            "staged_diff": "",
            "working_diff": "",
            "untracked_files": [],
        }

    def test_propagates_giterror_on_failure(self) -> None:
        runner = _RecordingRunner(
            [(128, "", "fatal: not a git repository\n")]
        )
        with pytest.raises(GitOpError, match="not a git repository"):
            git_ops.git_uncommitted_changes(runner=runner)

    def test_passes_cwd(self) -> None:
        runner = _RecordingRunner(
            [
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
            ]
        )
        git_ops.git_uncommitted_changes(path="/work/dir", runner=runner)
        for call in runner.calls:
            assert call["kwargs"]["cwd"] == "/work/dir"
