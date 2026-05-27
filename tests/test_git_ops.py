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

from types import SimpleNamespace
from typing import Any

import pytest

import git_ops
from git_ops import GitOpError


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
        raise FileNotFoundError("[Errno 2] No such file or directory: 'git'")


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
        assert runner.calls[0]["args"] == [
            "git",
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
        ]
        kwargs = runner.calls[0]["kwargs"]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False

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

    def test_empty_stdout_raises(self) -> None:
        # rev-parse should never return empty on a healthy repo; if it
        # does, fail loud rather than returning "" as a branch name.
        runner = _RecordingRunner([(0, "\n", "")])
        with pytest.raises(GitOpError, match="empty branch name"):
            git_ops.git_current_branch(runner=runner)


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
            "remote",
            "get-url",
            "origin",
        ]

    def test_no_origin_remote_raises(self) -> None:
        runner = _RecordingRunner(
            [(128, "", "error: No such remote 'origin'\n")]
        )
        with pytest.raises(GitOpError, match="No such remote"):
            git_ops.git_remote_repo(runner=runner)

    def test_unparseable_url_raises(self) -> None:
        runner = _RecordingRunner([(0, "not-a-url\n", "")])
        with pytest.raises(GitOpError, match="could not parse"):
            git_ops.git_remote_repo(runner=runner)


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


class TestGitStatusDriver:
    def test_subprocess_shape(self) -> None:
        runner = _RecordingRunner([(0, STATUS_CLEAN, "")])
        git_ops.git_status(runner=runner)
        # The porcelain=v2 + branch + untracked-files=normal flags are
        # the contract. A regression to porcelain v1 would change every
        # field we parse without breaking the canned-output tests above
        # — assert the args explicitly.
        assert runner.calls[0]["args"] == [
            "git",
            "status",
            "--porcelain=v2",
            "--branch",
            "--untracked-files=normal",
        ]

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
        assert args[0] == "git"
        assert args[1] == "log"
        # The pretty-format string uses U+001F (Unit Separator) so no
        # subject line / author / date can contain the delimiter — a
        # subject like "fix: split a|b|c" would otherwise break a
        # pipe-delimited format. Pin the format.
        pretty_arg = args[2]
        assert pretty_arg.startswith("--pretty=format:")
        assert "%H" in pretty_arg
        assert "%h" in pretty_arg
        assert "%s" in pretty_arg
        assert "%an" in pretty_arg
        assert "%aI" in pretty_arg  # ISO 8601 strict
        assert _SEP in pretty_arg
        # `-n<count>` shape, not separate args
        assert args[3] == "-n3"
        assert args[4] == "HEAD"

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
        assert runner.calls[0]["args"][4] == "origin/main"

    def test_empty_output_returns_empty_list(self) -> None:
        # `git log` on a freshly-init'd repo with no commits returns
        # exit 0 + empty stdout. Should return [] not raise.
        runner = _RecordingRunner([(0, "", "")])
        assert git_ops.git_recent_commits(runner=runner) == []

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
        assert runner.calls[0]["args"] == ["git", "diff", "--cached"]
        assert runner.calls[1]["args"] == ["git", "diff"]
        assert runner.calls[2]["args"] == [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
        ]
        assert result == {
            "staged_diff": "diff --staged\n",
            "working_diff": "diff --working\n",
            "untracked_files": ["untracked1.py", "untracked2.md"],
        }

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
