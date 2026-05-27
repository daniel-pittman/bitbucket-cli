"""
Tests for bb_ops repos / branches / vars / downloads / commits operations.

Same discipline as the pipelines and PRs test files: assert URL +
method + body shape per HTTP touchpoint; never just response value.
Boundary-validation rejections assert `opener.calls == []` to prove no
network IO happened on bad input.

All fixture data is fictional (acme / widget-service / alice / bob).
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

import pytest

import bb_ops
from bb_api import BBClient, BBConfig, DEFAULT_API_BASE


# ---------------------------------------------------------------------------
# Test scaffolding (duplicated across test files for end-to-end readability)
# ---------------------------------------------------------------------------


class _CaptureOpener:
    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def open(self, req: urllib.request.Request, timeout: float = 30.0) -> Any:
        body = req.data
        normalised_headers = {k.title(): v for k, v in req.header_items()}
        self.calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "headers": normalised_headers,
                "body": json.loads(body.decode("utf-8")) if body else None,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(
                f"opener received an unexpected request: "
                f"{req.get_method()} {req.full_url}"
            )
        resp = self.responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        if resp is None:
            body_bytes: bytes = b""
        elif isinstance(resp, (bytes, bytearray)):
            body_bytes = bytes(resp)
        else:
            body_bytes = json.dumps(resp).encode("utf-8")
        return _FakeResponse(body_bytes)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: Any) -> None:
        pass


def _client(opener: _CaptureOpener) -> BBClient:
    cfg = BBConfig(
        user="alice@example.com",
        token="tok-xyz",
        workspace="acme",
        api_base=DEFAULT_API_BASE,
    )
    return BBClient(cfg, opener=opener)


def _repo_url() -> str:
    return DEFAULT_API_BASE + "/repositories/acme/widget-service"


# ===========================================================================
# repos_list
# ===========================================================================


class TestReposList:
    def test_default_workspace_from_client(self) -> None:
        # workspace=None should default to client.config.workspace ("acme").
        opener = _CaptureOpener([{"values": [{"slug": "widget-service"}]}])
        result = bb_ops.repos_list(_client(opener))
        assert len(result) == 1
        url = opener.calls[0]["url"]
        assert url.startswith(DEFAULT_API_BASE + "/repositories/acme?")
        assert "sort=-updated_on" in url
        assert "pagelen=100" in url

    def test_explicit_workspace_override(self) -> None:
        opener = _CaptureOpener([{"values": []}])
        bb_ops.repos_list(_client(opener), workspace="other-org")
        assert opener.calls[0]["url"].startswith(
            DEFAULT_API_BASE + "/repositories/other-org?"
        )

    def test_count_walks_pages(self) -> None:
        opener = _CaptureOpener(
            [
                {
                    "values": [{"slug": f"repo-{i}"} for i in range(100)],
                    "next": DEFAULT_API_BASE + "/repositories/acme?page=2",
                },
                {"values": [{"slug": f"repo-{i}"} for i in range(100, 250)]},
            ]
        )
        result = bb_ops.repos_list(_client(opener), count=250)
        assert len(result) == 250
        assert "pagelen=100" in opener.calls[0]["url"]

    def test_query_filter(self) -> None:
        opener = _CaptureOpener([{"values": []}])
        bb_ops.repos_list(_client(opener), query='name ~ "widget"')
        # urlencode emits `q=name+~+%22widget%22`: spaces -> `+`, `~`
        # stays as-is (unreserved), `"` -> `%22`. Assert the exact form
        # so a regression in either the validator or the urlencode
        # behaviour is visible.
        url = opener.calls[0]["url"]
        assert "q=name+~+%22widget%22" in url

    @pytest.mark.parametrize("bad", [0, -1, True, False, "ten"])
    def test_rejects_non_positive_count(self, bad: Any) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="count"):
            bb_ops.repos_list(_client(opener), count=bad)
        assert opener.calls == []

    @pytest.mark.parametrize("bad_workspace", ["", "   ", "\n\t"])
    def test_rejects_empty_workspace(self, bad_workspace: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="workspace"):
            bb_ops.repos_list(_client(opener), workspace=bad_workspace)
        assert opener.calls == []

    @pytest.mark.parametrize("bad_workspace", ["acme/widget", "a/b/c"])
    def test_rejects_workspace_with_slash(self, bad_workspace: str) -> None:
        """Without this, `workspace="acme/widget"` would silently build
        a single-repo endpoint URL and paginate against a response that
        lacks `values` — confusing failure. Symmetric with bb_api.repo_path."""
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="workspace.*'/'"):
            bb_ops.repos_list(_client(opener), workspace=bad_workspace)
        assert opener.calls == []

    @pytest.mark.parametrize("bad_workspace", [".", ".."])
    def test_rejects_workspace_dot_segments(self, bad_workspace: str) -> None:
        """Path-traversal defense — `/repositories/../widget` after URL
        normalisation could resolve to the wrong workspace."""
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match=r"'\.'|'\.\.'"):
            bb_ops.repos_list(_client(opener), workspace=bad_workspace)
        assert opener.calls == []

    @pytest.mark.parametrize("bad_query", ["", "   "])
    def test_rejects_empty_query(self, bad_query: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="query"):
            bb_ops.repos_list(_client(opener), query=bad_query)
        assert opener.calls == []


# ===========================================================================
# repo_show
# ===========================================================================


class TestRepoShow:
    def test_fetches_repo_metadata(self) -> None:
        opener = _CaptureOpener(
            [{"full_name": "acme/widget-service", "language": "python"}]
        )
        result = bb_ops.repo_show(_client(opener), "acme", "widget-service")
        assert result["language"] == "python"
        call = opener.calls[0]
        assert call["url"] == _repo_url()
        assert call["method"] == "GET"


# ===========================================================================
# branches_list + branch_show
# ===========================================================================


class TestBranchesList:
    def test_default_sort_and_pagelen(self) -> None:
        opener = _CaptureOpener([{"values": [{"name": "main"}, {"name": "develop"}]}])
        result = bb_ops.branches_list(_client(opener), "acme", "widget-service")
        assert [b["name"] for b in result] == ["main", "develop"]
        url = opener.calls[0]["url"]
        assert url.startswith(_repo_url() + "/refs/branches?")
        assert "sort=-target.date" in url
        assert "pagelen=50" in url

    def test_count_walks_pages(self) -> None:
        opener = _CaptureOpener(
            [
                {
                    "values": [{"name": f"branch-{i}"} for i in range(100)],
                    "next": DEFAULT_API_BASE + "/x?page=2",
                },
                {"values": [{"name": f"branch-{i}"} for i in range(100, 200)]},
            ]
        )
        result = bb_ops.branches_list(
            _client(opener), "acme", "widget-service", count=200
        )
        assert len(result) == 200

    def test_query_filter(self) -> None:
        opener = _CaptureOpener([{"values": []}])
        bb_ops.branches_list(
            _client(opener), "acme", "widget-service", query='name ~ "feat"'
        )
        # Exact-form assertion (symmetric with TestReposList.test_query_filter):
        # a regression that silently mangled or dropped the BBQL string
        # would otherwise pass a `"q=" in url` weak check.
        assert "q=name+~+%22feat%22" in opener.calls[0]["url"]

    @pytest.mark.parametrize("bad", [0, -1, True, False])
    def test_rejects_non_positive_count(self, bad: Any) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="count"):
            bb_ops.branches_list(
                _client(opener), "acme", "widget-service", count=bad
            )
        assert opener.calls == []

    @pytest.mark.parametrize("bad_query", ["", "   "])
    def test_rejects_empty_query(self, bad_query: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="query"):
            bb_ops.branches_list(
                _client(opener), "acme", "widget-service", query=bad_query
            )
        assert opener.calls == []


class TestBranchShow:
    def test_fetches_single_branch(self) -> None:
        opener = _CaptureOpener([{"name": "main", "target": {"hash": "abc123"}}])
        result = bb_ops.branch_show(
            _client(opener), "acme", "widget-service", "main"
        )
        assert result["name"] == "main"
        assert opener.calls[0]["url"] == _repo_url() + "/refs/branches/main"

    def test_url_encodes_slash_in_branch_name(self) -> None:
        # feat/widget would otherwise be interpreted as a sub-resource
        # path; must URL-encode the slash.
        opener = _CaptureOpener([{"name": "feat/widget"}])
        bb_ops.branch_show(
            _client(opener), "acme", "widget-service", "feat/widget"
        )
        assert opener.calls[0]["url"] == _repo_url() + "/refs/branches/feat%2Fwidget"

    @pytest.mark.parametrize("bad_name", ["", "   ", "\n"])
    def test_rejects_empty_name(self, bad_name: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="name"):
            bb_ops.branch_show(
                _client(opener), "acme", "widget-service", bad_name
            )
        assert opener.calls == []

    def test_strips_whitespace_around_branch_name(self) -> None:
        # Caller might pass branch name with stray whitespace from copy-paste.
        # We strip but do not silently accept whitespace-only (rejected above).
        opener = _CaptureOpener([{"name": "main"}])
        bb_ops.branch_show(
            _client(opener), "acme", "widget-service", "  main  "
        )
        assert opener.calls[0]["url"] == _repo_url() + "/refs/branches/main"


# ===========================================================================
# vars_list
# ===========================================================================


class TestVarsList:
    def test_lists_pipeline_variables(self) -> None:
        opener = _CaptureOpener(
            [
                {
                    "values": [
                        {"key": "DEPLOY_TAG", "secured": False, "value": "latest"},
                        {"key": "BB_TOKEN", "secured": True, "value": None},
                    ]
                }
            ]
        )
        result = bb_ops.vars_list(_client(opener), "acme", "widget-service")
        assert [v["key"] for v in result] == ["DEPLOY_TAG", "BB_TOKEN"]
        # Secured value comes back as None from the API; we don't mask
        # at this layer (the MCP tool surfaces the `secured` flag).
        assert result[1]["value"] is None
        url = opener.calls[0]["url"]
        assert url.startswith(_repo_url() + "/pipelines_config/variables/?")
        assert "pagelen=100" in url

    @pytest.mark.parametrize("bad", [0, -1, True, False])
    def test_rejects_non_positive_count(self, bad: Any) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="count"):
            bb_ops.vars_list(_client(opener), "acme", "widget-service", count=bad)
        assert opener.calls == []


# ===========================================================================
# downloads_list
# ===========================================================================


class TestDownloadsList:
    def test_lists_downloads(self) -> None:
        opener = _CaptureOpener(
            [
                {
                    "values": [
                        {"name": "release-v1.0.zip", "size": 102400},
                        {"name": "install.sh", "size": 5120},
                    ]
                }
            ]
        )
        result = bb_ops.downloads_list(
            _client(opener), "acme", "widget-service"
        )
        assert [d["name"] for d in result] == ["release-v1.0.zip", "install.sh"]
        url = opener.calls[0]["url"]
        assert url.startswith(_repo_url() + "/downloads?")
        assert "pagelen=25" in url

    @pytest.mark.parametrize("bad", [0, -1, True, False])
    def test_rejects_non_positive_count(self, bad: Any) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="count"):
            bb_ops.downloads_list(
                _client(opener), "acme", "widget-service", count=bad
            )
        assert opener.calls == []


# ===========================================================================
# commits_list
# ===========================================================================


class TestCommitsList:
    def test_all_branches_when_branch_none(self) -> None:
        opener = _CaptureOpener(
            [
                {
                    "values": [
                        {"hash": "abc1", "message": "Commit 1"},
                        {"hash": "abc2", "message": "Commit 2"},
                    ]
                }
            ]
        )
        result = bb_ops.commits_list(
            _client(opener), "acme", "widget-service", count=2
        )
        assert [c["hash"] for c in result] == ["abc1", "abc2"]
        url = opener.calls[0]["url"]
        # Without branch, hits /commits (not /commits/{branch}).
        assert url.startswith(_repo_url() + "/commits?")
        assert "pagelen=2" in url

    def test_specific_branch(self) -> None:
        opener = _CaptureOpener([{"values": []}])
        bb_ops.commits_list(
            _client(opener), "acme", "widget-service", branch="main"
        )
        assert opener.calls[0]["url"].startswith(_repo_url() + "/commits/main?")

    def test_branch_name_with_slash_is_url_encoded(self) -> None:
        opener = _CaptureOpener([{"values": []}])
        bb_ops.commits_list(
            _client(opener),
            "acme",
            "widget-service",
            branch="feat/widget",
        )
        assert opener.calls[0]["url"].startswith(
            _repo_url() + "/commits/feat%2Fwidget?"
        )

    @pytest.mark.parametrize("bad_branch", ["", "   ", "\n"])
    def test_rejects_empty_branch(self, bad_branch: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="branch"):
            bb_ops.commits_list(
                _client(opener),
                "acme",
                "widget-service",
                branch=bad_branch,
            )
        assert opener.calls == []

    @pytest.mark.parametrize("bad", [0, -1, True, False])
    def test_rejects_non_positive_count(self, bad: Any) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="count"):
            bb_ops.commits_list(
                _client(opener), "acme", "widget-service", count=bad
            )
        assert opener.calls == []

    def test_count_walks_pages(self) -> None:
        """commits_list has the most complex path shape (branch vs
        no-branch); pin its pagination behaviour symmetrically with
        the other list ops."""
        opener = _CaptureOpener(
            [
                {
                    "values": [{"hash": f"c{i:03}"} for i in range(100)],
                    "next": _repo_url() + "/commits?page=2",
                },
                {"values": [{"hash": f"c{i:03}"} for i in range(100, 175)]},
            ]
        )
        result = bb_ops.commits_list(
            _client(opener), "acme", "widget-service", count=175
        )
        assert len(result) == 175
        assert "pagelen=100" in opener.calls[0]["url"]
