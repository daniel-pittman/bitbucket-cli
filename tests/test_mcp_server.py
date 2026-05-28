"""
Tests for mcp_server.

Discipline:
  - BB_MCP_SKIP_BOOTSTRAP=1 is set in conftest.py BEFORE this module
    imports, so importing mcp_server skips the venv bootstrap and uses
    the FastMCP stub (decorator returns the function unchanged).
  - Tools are tested by patching bb_ops / git_ops functions at the
    module level, calling the tool directly, and asserting (a) the
    underlying function was called with the right arguments, (b) the
    success-path result shape, and (c) the error-path shape for each
    expected exception kind.
  - No live HTTP / subprocess: bb_ops / git_ops have their own tests
    for that. This file pins the WIRING — the layer that decides which
    bb_ops function to call and how to shape the response dict.

All fixtures are fictional (acme / widget-service / alice / bob).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

import bb_api
import bb_ops
import git_ops
import mcp_server


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level client cache + default cwd between tests so
    one test's state doesn't leak into another's.

    Also scrubs the BB_* auth env vars so a developer running pytest
    locally with `BB_USER=...` exported in their shell doesn't accidentally
    let `bb_api.load_config()` pick up their real config in any test that
    reaches `_get_client()` without the `stub_client` fixture. Without
    this scrub, a future test could pass locally and fail in CI (or
    vice versa) based on developer env, not code state."""
    mcp_server._reset_client_cache()
    # Default BB_DEFAULT_REPO_PATH to a stable value so tests can assert on it.
    monkeypatch.setenv("BB_DEFAULT_REPO_PATH", "/test/cwd")
    # Scrub ambient BB config so the suite is hermetic against dev env.
    for k in ("BB_USER", "BB_TOKEN", "BB_WORKSPACE", "BB_API_BASE"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def stub_client(monkeypatch: pytest.MonkeyPatch) -> bb_api.BBClient:
    """Inject a stub client into the cache so tools don't try to read
    real config files. Returns the client so tests can inspect it."""
    cfg = bb_api.BBConfig(
        user="alice@example.com",
        token="tok-xyz",
        workspace="acme",
        api_base=bb_api.DEFAULT_API_BASE,
    )
    # We don't need a real opener — tests patch bb_ops/git_ops functions
    # before they're called, so the client's HTTP layer is never invoked.
    client = bb_api.BBClient(cfg)
    monkeypatch.setattr(mcp_server, "_client_cache", client)
    return client


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


# Every tool the agent expects. If we add or remove a tool, this list
# updates and the count assertion below catches an accidental drop.
EXPECTED_TOOLS = {
    # Pipelines
    "pipelines_list",
    "pipeline_show",
    "pipeline_steps",
    "pipeline_trigger",
    "pipeline_stop",
    "pipeline_logs",
    # PRs
    "prs_list",
    "pr_show",
    "pr_activity",
    "pr_create",
    "pr_approve",
    "pr_unapprove",
    "pr_merge",
    "pr_decline",
    "pr_diff",
    "pr_comments_list",
    "pr_comment_add",
    # Workspaces
    "workspaces_list",
    # Repos / branches / metadata
    "repos_list",
    "repo_show",
    "branches_list",
    "branch_show",
    "vars_list",
    "downloads_list",
    "commits_list",
    # Git context
    "git_current_branch",
    "git_status",
    "git_remote_repo",
    "git_recent_commits",
    "git_uncommitted_changes",
    # Meta
    "whoami",
}


def test_all_expected_tools_registered() -> None:
    """The FastMCP stub records every @mcp.tool()-decorated function in
    its _tools dict. Pin the exact set so an accidental rename or drop
    is caught by the suite — the agent depends on these exact names."""
    registered = set(mcp_server.mcp._tools.keys())
    assert registered == EXPECTED_TOOLS, (
        f"Tool set drift. Missing: {EXPECTED_TOOLS - registered}. "
        f"Extra: {registered - EXPECTED_TOOLS}."
    )


def test_tool_count_matches_expectation() -> None:
    """Independent sanity check — pin the exact number so a silent
    regression that drops a registration is visible. 31 = 6 pipelines
    + 11 PRs + 1 workspaces + 7 repos/metadata + 5 git context + 1 meta."""
    assert len(mcp_server.mcp._tools) == 31


# ---------------------------------------------------------------------------
# _resolve_repo
# ---------------------------------------------------------------------------


class TestResolveRepo:
    def test_empty_repo_uses_git_remote(
        self,
        stub_client: bb_api.BBClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `""` triggers git_remote_repo auto-detect from BB_DEFAULT_REPO_PATH.
        monkeypatch.setattr(
            git_ops, "git_remote_repo",
            lambda path=None: ("from-remote", "widget-service"),
        )
        client, ws, slug = mcp_server._resolve_repo("")
        assert client is stub_client
        assert ws == "from-remote"
        assert slug == "widget-service"

    def test_bare_slug_uses_config_workspace(
        self, stub_client: bb_api.BBClient
    ) -> None:
        # No slash → workspace defaults to client.config.workspace.
        client, ws, slug = mcp_server._resolve_repo("my-repo")
        assert ws == "acme"  # from BBConfig
        assert slug == "my-repo"

    def test_workspace_slash_repo_overrides(
        self, stub_client: bb_api.BBClient
    ) -> None:
        # "ws/repo" overrides the configured workspace.
        client, ws, slug = mcp_server._resolve_repo("other/cool-repo")
        assert ws == "other"
        assert slug == "cool-repo"

    def test_bare_slug_with_empty_workspace_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v1.2.0: BB_WORKSPACE is optional, so a bare slug can have no
        configured workspace to resolve against. That must raise a clear
        ValueError naming the fixes (set BB_WORKSPACE / use ws/slug /
        omit for auto-detect) rather than building a '/repositories//slug'
        URL. The 'ws/slug' and auto-detect paths still work without a
        configured workspace — only the bare-slug path needs one."""
        mcp_server._reset_client_cache()
        cfg = bb_api.BBConfig(
            user="alice@example.com", token="tok-xyz",
            workspace="",  # optional + absent
            api_base=bb_api.DEFAULT_API_BASE,
        )
        monkeypatch.setattr(mcp_server, "_client_cache", bb_api.BBClient(cfg))
        with pytest.raises(ValueError, match="no workspace for bare slug"):
            mcp_server._resolve_repo("my-repo")
        # But ws/slug still resolves fine with an empty config workspace.
        _client, ws, slug = mcp_server._resolve_repo("acme/my-repo")
        assert (ws, slug) == ("acme", "my-repo")

    @pytest.mark.parametrize(
        "bad",
        ["a/b/c", "/repo", "ws/", "/", "//"],
    )
    def test_malformed_repo_raises_value_error(
        self, stub_client: bb_api.BBClient, bad: str
    ) -> None:
        with pytest.raises(ValueError, match="repo must be"):
            mcp_server._resolve_repo(bad)

    def test_strips_whitespace_before_parsing(
        self, stub_client: bb_api.BBClient
    ) -> None:
        """A sloppy paste like '  acme/widget  ' must not slip through
        as workspace='  acme' and surface as a deep API failure."""
        client, ws, slug = mcp_server._resolve_repo("  acme/widget  ")
        assert ws == "acme"
        assert slug == "widget"

    def test_whitespace_only_triggers_autodetect(
        self,
        stub_client: bb_api.BBClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Whitespace-only repo must not slip through; it should trip
        the same auto-detect path as the empty string."""
        monkeypatch.setattr(
            git_ops, "git_remote_repo",
            lambda path=None: ("from-remote", "ws"),
        )
        _, ws, slug = mcp_server._resolve_repo("   ")
        assert ws == "from-remote"

    def test_none_treated_as_empty(
        self,
        stub_client: bb_api.BBClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """JSON `null` from the MCP client deserialises to None.
        Without normalisation, .strip() would crash uncaught with
        AttributeError."""
        monkeypatch.setattr(
            git_ops, "git_remote_repo",
            lambda path=None: ("from-remote", "ws"),
        )
        _, ws, _ = mcp_server._resolve_repo(None)
        assert ws == "from-remote"

    def test_inner_slug_parts_stripped(
        self, stub_client: bb_api.BBClient
    ) -> None:
        """'acme/ widget' (whitespace on the slug-half after split)
        must not slip through as ws='acme', slug=' widget' and 404
        on `/repositories/acme/%20widget`."""
        client, ws, slug = mcp_server._resolve_repo("acme/ widget")
        assert ws == "acme"
        assert slug == "widget"

    def test_repo_validated_before_get_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh-machine user without config + a malformed slug
        should see the ValueError (real cause) not BBConfigError
        (masking failure)."""
        mcp_server._reset_client_cache()
        def raise_config(*_args: Any, **_kwargs: Any) -> Any:
            raise bb_api.BBConfigError("Missing BB_USER")
        monkeypatch.setattr(bb_api, "load_config", raise_config)
        # Malformed repo: three parts. Should raise ValueError, NOT
        # BBConfigError — proves the shape check runs before
        # _get_client().
        with pytest.raises(ValueError, match="repo must be"):
            mcp_server._resolve_repo("a/b/c")

    @pytest.mark.parametrize("bare", [".", ".."])
    def test_bare_dot_slug_validated_before_get_client(
        self, monkeypatch: pytest.MonkeyPatch, bare: str
    ) -> None:
        """Round-3 finding: the bare-slug fallback bypassed the same
        validation the slash-containing branch got. A fresh-machine
        user with `repo='.'` should see the actual ValueError, not
        BBConfigError masking it."""
        mcp_server._reset_client_cache()
        def raise_config(*_args: Any, **_kwargs: Any) -> Any:
            raise bb_api.BBConfigError("Missing BB_USER")
        monkeypatch.setattr(bb_api, "load_config", raise_config)
        with pytest.raises(ValueError, match=r"'\.'"):
            mcp_server._resolve_repo(bare)


# ---------------------------------------------------------------------------
# _error_dict
# ---------------------------------------------------------------------------


class TestErrorDict:
    def test_bbapierror_carries_status_url_body(self) -> None:
        e = bb_api.BBApiError(404, "https://x/y", '{"error":{"message":"nope"}}')
        d = mcp_server._error_dict(e)
        assert d["ok"] is False
        assert d["kind"] == "BBApiError"
        assert d["status"] == 404
        assert d["url"] == "https://x/y"
        assert "nope" in d["body"]

    def test_bbapierror_redacts_signed_s3_url(self) -> None:
        """Round-2/3/4 SECURITY findings: pipeline_logs / pr_diff follow
        Bitbucket's 307 to a signed S3 URL. If S3 then returns non-3xx
        (clock skew, expired, network hiccup), BBApiError carries the
        signed URL with AWS credentials in the query string. The agent
        error dict must NOT propagate it through ANY field.

        Round 2 fixed `url`; round 3 found `message` still leaked;
        round 4 found `body` still leaked. Pin EVERY string field that
        could carry the URL so a future regression can't add a new
        unredacted field without the test catching it."""
        signed = (
            "https://bbuseruploads.s3.amazonaws.com/path/to/log?"
            "X-Amz-Signature=abcd1234supersecret&X-Amz-Credential=AKIAEXAMPLE"
            "&Expires=12345"
        )
        # Realistic API body that echoes the upstream URL — typical for
        # nginx / proxy-layered error pages.
        body_with_url = (
            f"<html>Bad gateway: upstream {signed} failed: connection reset</html>"
        )
        e = bb_api.BBApiError(403, signed, body_with_url)
        d = mcp_server._error_dict(e)
        # EVERY string field free of every secret bit.
        for field in ("url", "message", "body"):
            assert "abcd1234supersecret" not in d[field], (
                f"signature leaked through {field}: {d[field]!r}"
            )
            assert "AKIAEXAMPLE" not in d[field], (
                f"AWS access key leaked through {field}: {d[field]!r}"
            )
        # Path part preserved in url so the agent knows what host was called.
        assert "bbuseruploads.s3.amazonaws.com" in d["url"]
        assert "redacted-signed-url-params" in d["url"]

    def test_bbapierror_redacts_embedded_creds(self) -> None:
        # Body field can also carry the credentialed URL (e.g.
        # `curl` showing the failing URL back in its error output).
        e = bb_api.BBApiError(
            401,
            "https://user:supersecret@api.bitbucket.org/2.0/foo",
            "Auth failed for https://user:supersecret@api.bitbucket.org/2.0/foo",
        )
        d = mcp_server._error_dict(e)
        for field in ("url", "message", "body"):
            assert "supersecret" not in d[field], (
                f"credential leaked through {field}: {d[field]!r}"
            )
        assert "[redacted]" in d["url"]
        assert "[redacted]" in d["message"]
        assert "[redacted]" in d["body"]

    def test_giterror_stderr_redacted(self) -> None:
        """Phase 4.7+ will add git wrappers that touch remote repos
        (fetch / push / ls-remote). Their stderr commonly contains
        `fatal: unable to access 'https://x-token-auth:TOKEN@bb.org/...'`.
        _error_dict must redact stderr the same way it redacts message
        and body."""
        e = git_ops.GitOpError(
            ["git", "fetch"],
            128,
            "fatal: unable to access 'https://x-token-auth:SECRETTOKEN@bitbucket.org/foo/bar.git/': The requested URL returned error: 401",
        )
        d = mcp_server._error_dict(e)
        for field in ("message", "stderr"):
            assert "SECRETTOKEN" not in d[field], (
                f"git token leaked through {field}: {d[field]!r}"
            )
            assert "x-token-auth" not in d[field], (
                f"git username leaked through {field}: {d[field]!r}"
            )
        assert "[redacted]" in d["stderr"]

    def test_signed_url_indicators_case_insensitive(self) -> None:
        """Round-3 finding: MinIO / R2 / Backblaze / mixed-case AWS
        variants may use different capitalisations of the signature
        param. Match case-insensitively."""
        # Lowercase variant.
        e1 = bb_api.BBApiError(
            403,
            "https://example.com/log?x-amz-signature=secret123",
            "AccessDenied",
        )
        d1 = mcp_server._error_dict(e1)
        assert "secret123" not in d1["url"]
        assert "secret123" not in d1["message"]

        # Plain `Signature=` (used by some non-AWS S3-compatible
        # services).
        e2 = bb_api.BBApiError(
            403,
            "https://r2.example.com/log?Signature=secret456",
            "Forbidden",
        )
        d2 = mcp_server._error_dict(e2)
        assert "secret456" not in d2["url"]
        assert "secret456" not in d2["message"]

    def test_redacts_azure_sas_url(self) -> None:
        """Round-4 finding: Azure Blob SAS URLs use ?sv=...&sig=...&se=...
        (no `signature=` suffix). The substring check must catch the
        `sig=` short form."""
        sas = "https://acct.blob.core.windows.net/c/blob?sv=2020&sig=ABCSECRET&se=2026"
        e = bb_api.BBApiError(403, sas, "AuthorizationFailed")
        d = mcp_server._error_dict(e)
        for field in ("url", "message"):
            assert "ABCSECRET" not in d[field]

    def test_redacts_bearer_token_in_url(self) -> None:
        """Bearer tokens in query string (`?access_token=...` /
        `?api_key=...`) also redacted."""
        for param in ("access_token", "api_key"):
            e = bb_api.BBApiError(
                401,
                f"https://api.example.com/endpoint?{param}=SECRET_BEARER",
                "Unauthorized",
            )
            d = mcp_server._error_dict(e)
            assert "SECRET_BEARER" not in d["url"]
            assert "SECRET_BEARER" not in d["message"]

    def test_safe_text_redacts_ssh_url_in_free_text(self) -> None:
        """Round-4 finding: _redact_message only matched http(s)://.
        ssh:// URLs with embedded passphrases (and other schemes) must
        also be caught. Validates the broadened _safe_text helper."""
        # Construct an error message that embeds an ssh:// URL with auth.
        text = "Could not read from remote repository ssh://x-token:SSHPASS@bb.org/foo.git: connection refused"
        redacted = mcp_server._safe_text(text)
        assert "SSHPASS" not in redacted
        assert "x-token" not in redacted

    def test_bbopnotfound_kind(self) -> None:
        e = bb_ops.BBOpNotFound("pipeline #42 not found")
        d = mcp_server._error_dict(e)
        assert d["ok"] is False
        assert d["kind"] == "BBOpNotFound"
        # No HTTP fields — distinct from BBApiError.
        assert "status" not in d

    def test_giterror_carries_returncode_stderr(self) -> None:
        e = git_ops.GitOpError(["git", "status"], 128, "fatal: not a git repo")
        d = mcp_server._error_dict(e)
        assert d["kind"] == "GitOpError"
        assert d["returncode"] == 128
        assert "not a git repo" in d["stderr"]

    def test_value_error_kind(self) -> None:
        e = ValueError("bad input")
        d = mcp_server._error_dict(e)
        assert d["kind"] == "ValueError"
        assert d["message"] == "bad input"


# ---------------------------------------------------------------------------
# Per-tool wiring tests
# ---------------------------------------------------------------------------
#
# Each pipeline / PR / repo tool dispatches to bb_ops.<func>(...). We patch
# the bb_ops function to a recorder, call the tool, then assert (a) what
# the tool passed to bb_ops, and (b) the response-dict shape.
#
# These tests deliberately do NOT exercise bb_ops's own logic — that's
# covered comprehensively in test_bb_ops_*.py. This file pins the WIRING.


def _recorder(return_value: Any) -> Any:
    """Build a stub that records its calls and returns a fixed value."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fn(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return return_value

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


class TestPipelineTools:
    def test_pipelines_list_dispatches_and_shapes(
        self, stub_client: bb_api.BBClient
    ) -> None:
        recorder = _recorder([{"build_number": 42}])
        with patch.object(bb_ops, "pipelines_list", recorder):
            out = mcp_server.pipelines_list(repo="my-repo", count=5, branch="main")
        # bb_ops.pipelines_list received the resolved (workspace, repo, count, branch, sort).
        assert recorder.calls[0][0] == (stub_client, "acme", "my-repo")
        assert recorder.calls[0][1] == {
            "count": 5,
            "branch": "main",
            "sort": "-created_on",  # default
        }
        assert out == {
            "ok": True,
            "workspace": "acme",
            "repo": "my-repo",
            "pipelines": [{"build_number": 42}],
        }

    def test_pipelines_list_sort_kwarg(self, stub_client: bb_api.BBClient) -> None:
        """`sort=` lets the agent ask for oldest-first or sort-by-completion."""
        recorder = _recorder([])
        with patch.object(bb_ops, "pipelines_list", recorder):
            mcp_server.pipelines_list(repo="my-repo", sort="created_on")
        assert recorder.calls[0][1]["sort"] == "created_on"

    def test_pipelines_list_empty_branch_passes_none(
        self, stub_client: bb_api.BBClient
    ) -> None:
        # Empty-string `branch` → None at the bb_ops boundary (so the
        # `target.ref_name` query param is omitted, not sent as "").
        recorder = _recorder([])
        with patch.object(bb_ops, "pipelines_list", recorder):
            mcp_server.pipelines_list(repo="my-repo")
        assert recorder.calls[0][1]["branch"] is None

    def test_pipeline_show_wraps_bbopnotfound(
        self, stub_client: bb_api.BBClient
    ) -> None:
        def raise_not_found(*_args: Any, **_kwargs: Any) -> Any:
            raise bb_ops.BBOpNotFound("pipeline #999 not found")

        with patch.object(bb_ops, "pipeline_show", raise_not_found):
            out = mcp_server.pipeline_show(number=999, repo="my-repo")
        assert out["ok"] is False
        assert out["kind"] == "BBOpNotFound"
        assert "#999" in out["message"]
        # Request identifier threaded into the error dict so an agent
        # running parallel pipeline_show calls can correlate failures
        # with originating requests.
        assert out["number"] == 999

    def test_pipeline_show_wraps_bbapierror(
        self, stub_client: bb_api.BBClient
    ) -> None:
        def raise_api(*_args: Any, **_kwargs: Any) -> Any:
            raise bb_api.BBApiError(403, "https://x", '{"error":"forbidden"}')

        with patch.object(bb_ops, "pipeline_show", raise_api):
            out = mcp_server.pipeline_show(number=42, repo="my-repo")
        assert out["ok"] is False
        assert out["status"] == 403
        assert "forbidden" in out["body"]
        assert out["number"] == 42

    def test_pipeline_trigger_empty_pattern_passes_none(
        self, stub_client: bb_api.BBClient
    ) -> None:
        recorder = _recorder({"build_number": 100})
        with patch.object(bb_ops, "pipeline_trigger", recorder):
            mcp_server.pipeline_trigger(branch="main", repo="my-repo", pattern="")
        # Empty pattern at MCP boundary becomes None at bb_ops boundary —
        # matters because bb_ops treats None as "default pipeline" but
        # would raise on empty string.
        assert recorder.calls[0][1]["pattern"] is None

    @pytest.mark.parametrize("bad_branch", ["", "   ", "\n\t"])
    def test_pipeline_trigger_rejects_empty_or_whitespace_branch(
        self, stub_client: bb_api.BBClient, bad_branch: str
    ) -> None:
        """Round-3 finding: pipeline_trigger forwarded branch verbatim,
        unlike pipelines_list / commits_list which funnel through
        _opt_str. Whitespace-only branch would silently POST
        target.ref_name='   ' and 4xx with an opaque body."""
        recorder = _recorder({})
        with patch.object(bb_ops, "pipeline_trigger", recorder):
            out = mcp_server.pipeline_trigger(branch=bad_branch, repo="my-repo")
        assert out["ok"] is False
        assert out["kind"] == "ValueError"
        assert recorder.calls == []  # bb_ops not reached

    def test_pipeline_trigger_strips_branch_whitespace(
        self, stub_client: bb_api.BBClient
    ) -> None:
        """Trailing/leading whitespace on a real branch name gets
        stripped so the API call uses the clean value."""
        recorder = _recorder({"build_number": 101})
        with patch.object(bb_ops, "pipeline_trigger", recorder):
            mcp_server.pipeline_trigger(branch="  main  ", repo="my-repo")
        assert recorder.calls[0][1]["branch"] == "main"

    def test_pipeline_logs_returns_log_text(
        self, stub_client: bb_api.BBClient
    ) -> None:
        recorder = _recorder("+ echo hello\nhello\n")
        with patch.object(bb_ops, "pipeline_logs", recorder):
            out = mcp_server.pipeline_logs(number=42, step_index=0, repo="my-repo")
        assert out["log"] == "+ echo hello\nhello\n"
        assert out["step_index"] == 0
        # Default timeout passed through.
        assert recorder.calls[0][1]["timeout"] == 120.0

    def test_pipeline_logs_custom_timeout(
        self, stub_client: bb_api.BBClient
    ) -> None:
        """Agent can extend timeout for pipelines with huge log payloads."""
        recorder = _recorder("")
        with patch.object(bb_ops, "pipeline_logs", recorder):
            mcp_server.pipeline_logs(
                number=42, step_index=0, repo="my-repo", timeout=600.0
            )
        assert recorder.calls[0][1]["timeout"] == 600.0


class TestPullRequestTools:
    def test_pr_create_auto_detects_source_branch(
        self,
        stub_client: bb_api.BBClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When source_branch is empty, pr_create auto-detects via
        git_ops.git_current_branch — matches bash `bb pr-create`."""
        monkeypatch.setattr(
            git_ops, "git_current_branch",
            lambda path=None: "feat/auto-detected",
        )
        recorder = _recorder({"id": 7})
        with patch.object(bb_ops, "pr_create", recorder):
            mcp_server.pr_create(title="Hi", repo="my-repo")
        # The resolved source_branch comes from git_current_branch.
        assert recorder.calls[0][1]["source_branch"] == "feat/auto-detected"

    def test_pr_create_explicit_source_branch_used_as_is(
        self,
        stub_client: bb_api.BBClient,
    ) -> None:
        recorder = _recorder({"id": 8})
        with patch.object(bb_ops, "pr_create", recorder):
            mcp_server.pr_create(
                title="Hi",
                source_branch="feat/explicit",
                repo="my-repo",
            )
        assert recorder.calls[0][1]["source_branch"] == "feat/explicit"

    def test_pr_create_whitespace_source_branch_triggers_autodetect(
        self,
        stub_client: bb_api.BBClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Whitespace-only source_branch should NOT slip through to
        bb_ops (which would then raise) — the whitespace trips the
        auto-detect path."""
        monkeypatch.setattr(
            git_ops, "git_current_branch",
            lambda path=None: "feat/detected",
        )
        recorder = _recorder({"id": 9})
        with patch.object(bb_ops, "pr_create", recorder):
            mcp_server.pr_create(title="Hi", source_branch="   ", repo="my-repo")
        assert recorder.calls[0][1]["source_branch"] == "feat/detected"

    def test_pr_create_rejects_detached_head_autodetect(
        self,
        stub_client: bb_api.BBClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """git_current_branch returns the literal 'HEAD' for both
        detached and unborn state — Bitbucket would accept it silently
        and create a degenerate PR. Surface a local error instead."""
        monkeypatch.setattr(
            git_ops, "git_current_branch",
            lambda path=None: "HEAD",
        )
        # bb_ops.pr_create must NOT be called when source_branch can't
        # be resolved cleanly.
        recorder = _recorder({"id": 10})
        with patch.object(bb_ops, "pr_create", recorder):
            out = mcp_server.pr_create(title="Hi", repo="my-repo")
        assert out["ok"] is False
        assert out["kind"] == "ValueError"
        assert "HEAD" in out["message"]
        # Identifier threading: title surfaces on the error path so
        # parallel pr_create fan-outs can correlate.
        assert out["title"] == "Hi"
        assert recorder.calls == []  # bb_ops.pr_create not reached

    def test_pr_create_rejects_explicit_head_source_branch(
        self,
        stub_client: bb_api.BBClient,
    ) -> None:
        """Round-3 finding: round-2 fix rejected 'HEAD' from auto-detect
        but the user-supplied path forwarded it verbatim. The check
        must apply to BOTH entry points."""
        recorder = _recorder({"id": 11})
        with patch.object(bb_ops, "pr_create", recorder):
            out = mcp_server.pr_create(
                title="Hi", source_branch="HEAD", repo="my-repo"
            )
        assert out["ok"] is False
        assert out["kind"] == "ValueError"
        assert "HEAD" in out["message"]
        assert recorder.calls == []  # bb_ops.pr_create not reached

    def test_pr_unapprove_dispatches(
        self, stub_client: bb_api.BBClient
    ) -> None:
        recorder = _recorder(None)
        with patch.object(bb_ops, "pr_unapprove", recorder):
            out = mcp_server.pr_unapprove(pr_id=42, repo="my-repo")
        assert recorder.calls[0][0] == (stub_client, "acme", "my-repo", 42)
        assert out["pr_id"] == 42

    def test_pr_comment_add_shape(
        self, stub_client: bb_api.BBClient
    ) -> None:
        recorder = _recorder({"id": 99, "content": {"raw": "LGTM"}})
        with patch.object(bb_ops, "pr_comment_add", recorder):
            out = mcp_server.pr_comment_add(pr_id=42, body="LGTM", repo="my-repo")
        assert recorder.calls[0][0] == (stub_client, "acme", "my-repo", 42, "LGTM")
        assert out["comment"]["id"] == 99

    def test_pr_merge_empty_message_passes_none(
        self, stub_client: bb_api.BBClient
    ) -> None:
        recorder = _recorder({"state": "MERGED"})
        with patch.object(bb_ops, "pr_merge", recorder):
            mcp_server.pr_merge(pr_id=42, repo="my-repo", message="")
        assert recorder.calls[0][1]["message"] is None


class TestRepoTools:
    def test_repos_list_uses_config_workspace_when_omitted(
        self, stub_client: bb_api.BBClient
    ) -> None:
        recorder = _recorder([])
        with patch.object(bb_ops, "repos_list", recorder):
            mcp_server.repos_list()
        # workspace= defaulted to client.config.workspace ("acme").
        assert recorder.calls[0][1]["workspace"] == "acme"

    def test_repos_list_explicit_workspace(
        self, stub_client: bb_api.BBClient
    ) -> None:
        recorder = _recorder([])
        with patch.object(bb_ops, "repos_list", recorder):
            mcp_server.repos_list(workspace="other")
        assert recorder.calls[0][1]["workspace"] == "other"

    def test_repos_list_strips_workspace_whitespace(
        self, stub_client: bb_api.BBClient
    ) -> None:
        """' acme' / 'acme ' must not slip through and 404 on
        `/repositories/%20acme`."""
        recorder = _recorder([])
        with patch.object(bb_ops, "repos_list", recorder):
            mcp_server.repos_list(workspace="  other-org  ")
        assert recorder.calls[0][1]["workspace"] == "other-org"

    def test_repos_list_whitespace_only_workspace_falls_back(
        self, stub_client: bb_api.BBClient
    ) -> None:
        """Whitespace-only workspace falls back to config workspace."""
        recorder = _recorder([])
        with patch.object(bb_ops, "repos_list", recorder):
            mcp_server.repos_list(workspace="   ")
        assert recorder.calls[0][1]["workspace"] == "acme"  # from config

    def test_branch_show_passes_name(self, stub_client: bb_api.BBClient) -> None:
        recorder = _recorder({"name": "feat/widget"})
        with patch.object(bb_ops, "branch_show", recorder):
            out = mcp_server.branch_show(name="feat/widget", repo="my-repo")
        assert recorder.calls[0][0] == (stub_client, "acme", "my-repo", "feat/widget")
        assert out["name"] == "feat/widget"

    def test_commits_list_empty_branch_passes_none(
        self, stub_client: bb_api.BBClient
    ) -> None:
        recorder = _recorder([])
        with patch.object(bb_ops, "commits_list", recorder):
            mcp_server.commits_list(repo="my-repo")
        assert recorder.calls[0][1]["branch"] is None


class TestGitTools:
    def test_git_current_branch_uses_default_path(
        self,
        stub_client: bb_api.BBClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorder = _recorder("main")
        with patch.object(git_ops, "git_current_branch", recorder):
            out = mcp_server.git_current_branch()
        # Default path from BB_DEFAULT_REPO_PATH env (set by _reset_state).
        assert recorder.calls[0][1]["path"] == "/test/cwd"
        assert out == {"ok": True, "path": "/test/cwd", "branch": "main"}

    def test_git_current_branch_explicit_path(
        self, stub_client: bb_api.BBClient
    ) -> None:
        recorder = _recorder("main")
        with patch.object(git_ops, "git_current_branch", recorder):
            mcp_server.git_current_branch(path="/explicit/dir")
        assert recorder.calls[0][1]["path"] == "/explicit/dir"

    def test_git_status_payload_under_working_tree_key(
        self, stub_client: bb_api.BBClient
    ) -> None:
        """Payload is keyed under `working_tree`, not `status`, to
        avoid colliding with the HTTP-status field _error_dict uses
        for BBApiError."""
        status = {"branch": "main", "clean": True}
        with patch.object(git_ops, "git_status", _recorder(status)):
            out = mcp_server.git_status()
        assert out["working_tree"] == status
        assert "status" not in out  # no collision risk

    def test_git_recent_commits_passes_count_and_ref(
        self, stub_client: bb_api.BBClient
    ) -> None:
        recorder = _recorder([{"sha": "a" * 40}])
        with patch.object(git_ops, "git_recent_commits", recorder):
            mcp_server.git_recent_commits(count=5, ref="origin/main")
        assert recorder.calls[0][1]["count"] == 5
        assert recorder.calls[0][1]["ref"] == "origin/main"

    def test_git_op_error_wrapped_in_error_dict(
        self, stub_client: bb_api.BBClient
    ) -> None:
        def raise_git(*_args: Any, **_kwargs: Any) -> Any:
            raise git_ops.GitOpError(["git", "status"], 128, "fatal: not a git repo")

        with patch.object(git_ops, "git_status", raise_git):
            out = mcp_server.git_status()
        assert out["ok"] is False
        assert out["kind"] == "GitOpError"
        assert out["returncode"] == 128


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------


class TestWhoami:
    """whoami has three phases — (1) config, (2) git context, (3) workspace
    reachability via a single low-cost GET. Every test stubs phase (3)'s
    HTTP layer so the suite stays hermetic — without the stub, an unpatched
    BBClient.get would hit api.bitbucket.org for real."""

    @staticmethod
    def _stub_auth_ok(client: bb_api.BBClient) -> list[tuple[str, dict]]:
        """Replace client.get with a recorder that returns success.
        Returns the call-log so tests can assert the right endpoint
        was hit with the right query."""
        calls: list[tuple[str, dict]] = []
        def fake_get(path: str, *, query=None, timeout=None):
            calls.append((path, dict(query or {})))
            return {"slug": "acme", "type": "workspace"}
        client.get = fake_get  # type: ignore[method-assign]
        return calls

    def test_reports_config_and_git_context(
        self,
        stub_client: bb_api.BBClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            git_ops, "git_current_branch",
            lambda path=None: "feat/test",
        )
        monkeypatch.setattr(
            git_ops, "git_remote_repo",
            lambda path=None: ("acme", "widget-service"),
        )
        calls = self._stub_auth_ok(stub_client)
        out = mcp_server.whoami()
        assert out["ok"] is True
        assert out["user"] == "alice@example.com"
        assert out["workspace"] == "acme"
        assert out["git_branch"] == "feat/test"
        assert out["git_workspace"] == "acme"
        assert out["git_repo"] == "widget-service"
        assert out["auth"] == {"ok": True, "workspace": "acme"}
        # Reachability probe hit the right endpoint with the cheap pagelen.
        assert calls == [("/repositories/acme", {"pagelen": "1"})]
        # Token must NEVER be echoed.
        assert "tok-xyz" not in str(out)
        assert "token" not in {k.lower() for k in out.keys()}

    def test_handles_git_failure_gracefully(
        self,
        stub_client: bb_api.BBClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Running outside a git repo shouldn't flip ok=False — the
        server is useful (config-reachable) even there."""
        def raise_git(*_args: Any, **_kwargs: Any) -> Any:
            raise git_ops.GitOpError(["git"], 128, "not a git repo")

        monkeypatch.setattr(git_ops, "git_current_branch", raise_git)
        monkeypatch.setattr(git_ops, "git_remote_repo", raise_git)
        self._stub_auth_ok(stub_client)
        out = mcp_server.whoami()
        assert out["ok"] is True  # config still loaded
        # The autouse fixture sets BB_DEFAULT_REPO_PATH so cwd resolves
        # cleanly — both git probes run and both capture their failures.
        assert "git_branch_error" in out
        assert "git_remote_error" in out

    def test_cwd_error_skips_git_probes(
        self,
        stub_client: bb_api.BBClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When _default_repo_path raises (e.g. cwd was deleted out from
        under the process), the per-probe git calls must be SKIPPED — not
        called with path=None and not silently swallowed. cwd_error
        captures the failure; no git_branch_error / git_remote_error
        keys are written."""
        def raise_cwd() -> str:
            raise OSError("[Errno 2] No such file or directory")

        monkeypatch.setattr(mcp_server, "_default_repo_path", raise_cwd)
        # Tripwires — if either gets called we want a loud test failure,
        # not a silent pass.
        def boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("git probe ran despite cwd_error")
        monkeypatch.setattr(git_ops, "git_current_branch", boom)
        monkeypatch.setattr(git_ops, "git_remote_repo", boom)
        self._stub_auth_ok(stub_client)
        out = mcp_server.whoami()
        assert out["ok"] is True
        assert "cwd_error" in out
        assert "git_branch_error" not in out
        assert "git_remote_error" not in out
        assert "cwd" not in out
        # Phase 3 still runs — auth is independent of cwd. probe_ws comes
        # from the configured workspace ("acme") since git_workspace was
        # never set (cwd_error skipped Phase 2's git probes).
        assert out["auth"] == {"ok": True, "workspace": "acme"}

    def test_auth_probe_skipped_when_no_workspace(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """v1.2.0: BB_WORKSPACE is optional, so config.workspace can be ""
        AND git auto-detect can fail to yield one. The probe must be
        SKIPPED (auth.ok=None) — not run against an empty workspace,
        which would build `GET /repositories/` (the global public-repos
        endpoint) and return a false-positive auth.ok=True. Mirrors the
        bash cmd_whoami skip behavior."""
        mcp_server._reset_client_cache()
        cfg = bb_api.BBConfig(
            user="alice@example.com", token="tok-xyz",
            workspace="",  # optional + absent
            api_base=bb_api.DEFAULT_API_BASE,
        )
        client = bb_api.BBClient(cfg)
        monkeypatch.setattr(mcp_server, "_client_cache", client)
        # No git context either (so git_workspace stays unset).
        def raise_git(*_a: Any, **_k: Any) -> Any:
            raise git_ops.GitOpError(["git"], 128, "not a git repo")
        monkeypatch.setattr(git_ops, "git_current_branch", raise_git)
        monkeypatch.setattr(git_ops, "git_remote_repo", raise_git)
        # Tripwire: the HTTP probe must NOT fire when there's no workspace.
        def boom_get(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("auth probe ran against an empty workspace")
        client.get = boom_get  # type: ignore[method-assign]
        out = mcp_server.whoami()
        assert out["ok"] is True
        assert out["auth"]["ok"] is None
        assert "skipped" in out["auth"]
        assert "tok-xyz" not in str(out)

    def test_auth_probe_falls_back_to_git_workspace(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When BB_WORKSPACE is empty but we're in a git checkout, the
        probe targets the git-detected workspace (not skipped). Mirrors
        bash cmd_whoami's probe_ws fallback."""
        mcp_server._reset_client_cache()
        cfg = bb_api.BBConfig(
            user="alice@example.com", token="tok-xyz",
            workspace="", api_base=bb_api.DEFAULT_API_BASE,
        )
        client = bb_api.BBClient(cfg)
        monkeypatch.setattr(mcp_server, "_client_cache", client)
        monkeypatch.setattr(git_ops, "git_current_branch", lambda path=None: "main")
        monkeypatch.setattr(
            git_ops, "git_remote_repo",
            lambda path=None: ("git-detected-ws", "widget-service"),
        )
        calls: list[tuple[str, dict]] = []
        def fake_get(path: str, *, query=None, timeout=None):
            calls.append((path, dict(query or {})))
            return {"slug": "widget-service"}
        client.get = fake_get  # type: ignore[method-assign]
        out = mcp_server.whoami()
        assert out["auth"] == {"ok": True, "workspace": "git-detected-ws"}
        # Probe used the git workspace, not an empty string.
        assert calls == [("/repositories/git-detected-ws", {"pagelen": "1"})]

    def test_config_error_flips_ok_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If config is missing, ok=False AND we still surface git context
        (best-effort) so the user can debug. The auth probe is skipped
        (no client to probe with) so out['auth'] is absent."""
        mcp_server._reset_client_cache()
        def raise_config(*_args: Any, **_kwargs: Any) -> Any:
            raise bb_api.BBConfigError("Missing BB_USER")

        monkeypatch.setattr(bb_api, "load_config", raise_config)
        monkeypatch.setattr(git_ops, "git_current_branch", lambda path=None: "main")
        monkeypatch.setattr(
            git_ops, "git_remote_repo",
            lambda path=None: ("acme", "widget-service"),
        )
        out = mcp_server.whoami()
        assert out["ok"] is False
        assert out["kind"] == "BBConfigError"
        assert "BB_USER" in out["message"]
        # Still reports git context.
        assert out["git_branch"] == "main"
        # Auth probe MUST be skipped when there's no client — never let
        # a None-deref slip in by refactor.
        assert "auth" not in out

    def test_auth_probe_failure_does_not_flip_ok(
        self,
        stub_client: bb_api.BBClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 401 from the workspace endpoint means the token is invalid
        for THIS workspace. Surface it as out['auth']['ok']=False but
        keep the outer ok=True — config + git context are still useful
        for debugging the credential."""
        monkeypatch.setattr(git_ops, "git_current_branch", lambda path=None: "main")
        monkeypatch.setattr(
            git_ops, "git_remote_repo",
            lambda path=None: ("acme", "widget-service"),
        )

        def fake_get(path: str, *, query=None, timeout=None):
            raise bb_api.BBApiError(
                status=401,
                url="https://api.bitbucket.org/2.0/repositories/acme?pagelen=1",
                body="",
            )

        stub_client.get = fake_get  # type: ignore[method-assign]
        out = mcp_server.whoami()
        assert out["ok"] is True  # outer call still ok
        assert out["auth"]["ok"] is False
        assert out["auth"]["kind"] == "BBApiError"
        assert out["auth"]["status"] == 401
        # Token must NEVER be echoed, even on the auth-failure path.
        assert "tok-xyz" not in str(out)

    def test_auth_probe_url_encodes_workspace(
        self,
        stub_client: bb_api.BBClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the workspace slug has a character that needs URL
        encoding (rare, but `/` would break path parsing), the probe
        must encode it — bash uses raw curl, Python uses urllib.quote
        with safe=''. Test the encoding rather than the raw substitution."""
        # BBConfig is frozen; swap the whole client in.
        weird_cfg = bb_api.BBConfig(
            user="alice@example.com",
            token="tok-xyz",
            workspace="ws/with-slash",
            api_base=bb_api.DEFAULT_API_BASE,
        )
        stub_client = bb_api.BBClient(weird_cfg)
        monkeypatch.setattr(mcp_server, "_client_cache", stub_client)
        monkeypatch.setattr(git_ops, "git_current_branch", lambda path=None: "main")
        monkeypatch.setattr(
            git_ops, "git_remote_repo",
            lambda path=None: ("acme", "widget-service"),
        )
        calls = self._stub_auth_ok(stub_client)
        out = mcp_server.whoami()
        assert out["ok"] is True
        # `/` must be encoded as %2F so it doesn't fragment the path.
        assert calls == [("/repositories/ws%2Fwith-slash", {"pagelen": "1"})]


# ---------------------------------------------------------------------------
# Bootstrap stub
# ---------------------------------------------------------------------------


class TestBootstrapStub:
    def test_fastmcp_stub_run_raises(self) -> None:
        """The stub MCP must never accidentally serve in production —
        .run() raises a clear error so a test that imports mcp_server
        and calls .run() fails loud instead of hanging on stdio."""
        with pytest.raises(RuntimeError, match="BB_MCP_SKIP_BOOTSTRAP"):
            mcp_server.mcp.run()

    def test_skip_bootstrap_env_is_set(self) -> None:
        """conftest.py sets BB_MCP_SKIP_BOOTSTRAP=1 unconditionally; pin
        that we actually loaded the stub path (not the real FastMCP)."""
        import os as _os
        assert _os.environ.get("BB_MCP_SKIP_BOOTSTRAP") == "1"
        assert mcp_server._MCP_SKIP_BOOTSTRAP is True


class TestVenvLocation:
    """Pin the durable XDG-spec venv location so a regression to the
    old /tmp/bbenv path (which gets wiped at every boot, forcing a
    rebuild) doesn't slip through. Mirrors the zenhub-cli pattern."""

    def test_venv_dir_is_under_xdg_data_home(self) -> None:
        """The venv path must end in `bitbucket-cli/venv` so it lives
        alongside other XDG-spec app state."""
        from pathlib import Path
        assert mcp_server._VENV_DIR.parts[-2:] == ("bitbucket-cli", "venv")
        # NOT /tmp (which would re-bootstrap every reboot).
        assert not str(mcp_server._VENV_DIR).startswith("/tmp")

    def test_xdg_data_home_env_var_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`XDG_DATA_HOME=/custom/path` must place the venv at
        `/custom/path/bitbucket-cli/venv` — per the XDG Base Dir spec.
        Calls the helper directly so the test doesn't need to reload
        the module."""
        from pathlib import Path
        monkeypatch.setenv("XDG_DATA_HOME", "/custom/xdg")
        assert mcp_server._xdg_data_home() == Path("/custom/xdg")

    def test_xdg_data_home_default_under_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When XDG_DATA_HOME is unset, fall back to ~/.local/share."""
        from pathlib import Path
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert mcp_server._xdg_data_home() == Path.home() / ".local" / "share"

    def test_venv_ready_sentinel_inside_venv_dir(self) -> None:
        """The ready-sentinel must live INSIDE the venv dir so removing
        the whole venv (`rm -rf $venv_dir`) also removes the sentinel
        — otherwise a stale sentinel could survive and claim a missing
        venv is ready."""
        assert mcp_server._VENV_READY.parent == mcp_server._VENV_DIR
