"""
Tests for bb_api.

Discipline: every test that touches HTTP asserts the request URL, method,
auth header, and JSON body shape — not just the response status. This catches
the "mock returns 200 regardless of request body" anti-pattern called out in
the testing methodology. If one of these tests surfaces a bug, the fix lands
in bb_api.py AND in the bash `bb` script if `bb` has parallel logic.

All fixture data is fictional: workspace `acme`, repo `widget-service`,
users `alice` / `bob`. Real personal identifiers must not appear in this
repository (see CONTRIBUTING.md).
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import bb_api
from bb_api import (
    BBApiError,
    BBClient,
    BBConfig,
    BBConfigError,
    DEFAULT_API_BASE,
    detect_repo,
    load_config,
    parse_remote_url,
    repo_path,
)


# =========================================================================
# load_config
# =========================================================================


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config"
    p.write_text(body)
    return p


def test_load_config_env_only(tmp_path: Path) -> None:
    cfg = load_config(
        env={
            "BB_USER": "alice@example.com",
            "BB_TOKEN": "tok-xyz",
            "BB_WORKSPACE": "acme",
        },
        config_path=tmp_path / "does-not-exist",
    )
    assert cfg.user == "alice@example.com"
    assert cfg.token == "tok-xyz"
    assert cfg.workspace == "acme"
    assert cfg.api_base == DEFAULT_API_BASE


def test_load_config_file_only(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        "BB_USER=bob@example.com\n"
        "BB_TOKEN=tok-from-file\n"
        "BB_WORKSPACE=widget-co\n",
    )
    cfg = load_config(env={}, config_path=cfg_path)
    assert cfg.user == "bob@example.com"
    assert cfg.token == "tok-from-file"
    assert cfg.workspace == "widget-co"


def test_load_config_env_overrides_file(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        "BB_USER=bob@example.com\n"
        "BB_TOKEN=file-token\n"
        "BB_WORKSPACE=widget-co\n",
    )
    cfg = load_config(
        env={"BB_TOKEN": "env-token"},
        config_path=cfg_path,
    )
    assert cfg.user == "bob@example.com"  # from file
    assert cfg.token == "env-token"  # env wins
    assert cfg.workspace == "widget-co"  # from file


def test_load_config_handles_export_prefix(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        "export BB_USER=alice@example.com\n"
        "export BB_TOKEN=tok\n"
        "BB_WORKSPACE=acme\n",
    )
    cfg = load_config(env={}, config_path=cfg_path)
    assert cfg.user == "alice@example.com"
    assert cfg.token == "tok"


def test_load_config_handles_quotes(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        'BB_USER="alice@example.com"\n'
        "BB_TOKEN='tok with spaces'\n"
        "BB_WORKSPACE=acme\n",
    )
    cfg = load_config(env={}, config_path=cfg_path)
    assert cfg.user == "alice@example.com"
    assert cfg.token == "tok with spaces"


def test_load_config_ignores_blank_and_comment_lines(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        "# Bitbucket config\n"
        "\n"
        "BB_USER=alice@example.com\n"
        "  # indented comment\n"
        "BB_TOKEN=tok\n"
        "BB_WORKSPACE=acme\n",
    )
    cfg = load_config(env={}, config_path=cfg_path)
    assert cfg.user == "alice@example.com"


def test_load_config_dotenv_overrides_home_config(tmp_path: Path) -> None:
    """Mirrors bash's load order: ~/.config/bb/config is sourced first,
    then .env, so .env overwrites the home config. Repo-local .env is
    intentionally the highest-priority file source because it's the
    development-override knob."""
    cfg_path = tmp_path / "home.cfg"
    cfg_path.write_text(
        "BB_USER=alice@example.com\nBB_TOKEN=home-tok\nBB_WORKSPACE=acme\n"
    )
    dotenv = tmp_path / ".env"
    dotenv.write_text("BB_TOKEN=dotenv-tok\nBB_WORKSPACE=widget-co\n")

    cfg = load_config(env={}, config_path=cfg_path, dotenv_path=dotenv)
    # .env wins. user is only in home, so it's carried through unchanged.
    assert cfg.user == "alice@example.com"
    assert cfg.token == "dotenv-tok"
    assert cfg.workspace == "widget-co"


def test_load_config_env_beats_dotenv(tmp_path: Path) -> None:
    """Process env still wins over .env, which still wins over home config."""
    cfg_path = tmp_path / "home.cfg"
    cfg_path.write_text(
        "BB_USER=alice@example.com\nBB_TOKEN=home-tok\nBB_WORKSPACE=acme\n"
    )
    dotenv = tmp_path / ".env"
    dotenv.write_text("BB_TOKEN=dotenv-tok\n")

    cfg = load_config(
        env={"BB_TOKEN": "env-tok"},
        config_path=cfg_path,
        dotenv_path=dotenv,
    )
    assert cfg.token == "env-tok"


def test_load_config_empty_env_var_does_not_fall_through(tmp_path: Path) -> None:
    """An explicitly-set empty env var should NOT silently let the file
    value through (the old `or` falsy-coalesce did this). The required-key
    check then catches the empty value and raises."""
    cfg_path = tmp_path / "home.cfg"
    cfg_path.write_text(
        "BB_USER=alice@example.com\nBB_TOKEN=file-tok\nBB_WORKSPACE=acme\n"
    )
    with pytest.raises(BBConfigError, match="BB_TOKEN"):
        load_config(env={"BB_TOKEN": ""}, config_path=cfg_path)


def test_load_config_normalises_trailing_slash_on_api_base(tmp_path: Path) -> None:
    cfg = load_config(
        env={
            "BB_USER": "alice@example.com",
            "BB_TOKEN": "tok",
            "BB_WORKSPACE": "acme",
            "BB_API_BASE": "https://bitbucket.example.com/2.0/",
        },
        config_path=tmp_path / "nope",
    )
    # No trailing slash — guards against `api_base + "/path"` producing "//path".
    assert cfg.api_base == "https://bitbucket.example.com/2.0"


def test_load_config_missing_keys_lists_all(tmp_path: Path) -> None:
    with pytest.raises(BBConfigError) as exc:
        load_config(env={}, config_path=tmp_path / "no-such-file")
    msg = str(exc.value)
    assert "BB_USER" in msg
    assert "BB_TOKEN" in msg
    assert "BB_WORKSPACE" in msg


def test_load_config_custom_api_base(tmp_path: Path) -> None:
    cfg = load_config(
        env={
            "BB_USER": "alice@example.com",
            "BB_TOKEN": "tok",
            "BB_WORKSPACE": "acme",
            "BB_API_BASE": "https://bitbucket.example.com/2.0",
        },
        config_path=tmp_path / "nope",
    )
    assert cfg.api_base == "https://bitbucket.example.com/2.0"


# =========================================================================
# parse_remote_url + detect_repo
# =========================================================================


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://bitbucket.org/acme/widget-service.git", ("acme", "widget-service")),
        ("https://bitbucket.org/acme/widget-service", ("acme", "widget-service")),
        ("git@bitbucket.org:acme/widget-service.git", ("acme", "widget-service")),
        ("git@bitbucket.org:acme/widget-service", ("acme", "widget-service")),
        (
            "https://alice@bitbucket.org/acme/widget-service.git",
            ("acme", "widget-service"),
        ),
        (
            "https://bitbucket.org/acme/widget-service.git/",
            ("acme", "widget-service"),
        ),
        (
            # Repo slug with a dot inside should not be truncated at the dot.
            "https://bitbucket.org/acme/my.cool.repo.git",
            ("acme", "my.cool.repo"),
        ),
    ],
)
def test_parse_remote_url_known_shapes(url: str, expected: tuple[str, str]) -> None:
    assert parse_remote_url(url) == expected


def test_parse_remote_url_returns_none_for_unparseable() -> None:
    assert parse_remote_url("not-a-url") is None
    assert parse_remote_url("") is None


def _fake_runner(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> Any:
    """Build a stand-in for the `subprocess` module that load_config /
    detect_repo can call via its `runner=` parameter. We only need .run."""

    class _Fake:
        @staticmethod
        def run(*args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                args=args[0] if args else kwargs.get("args"),
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )

    return _Fake


def test_detect_repo_https_remote() -> None:
    runner = _fake_runner(stdout="https://bitbucket.org/acme/widget-service.git\n")
    assert detect_repo(runner=runner) == "widget-service"


def test_detect_repo_ssh_remote() -> None:
    runner = _fake_runner(stdout="git@bitbucket.org:acme/widget-service.git\n")
    assert detect_repo(runner=runner) == "widget-service"


def test_detect_repo_invokes_git_remote_get_url() -> None:
    """Verifies the exact subprocess call shape. A future refactor that
    changes the command (e.g. to `git config remote.origin.url`), drops
    text=True, or skips cwd propagation, would silently break — without
    this assertion the canned-output tests would still pass.
    """
    captured: dict[str, Any] = {}

    class _Recording:
        @staticmethod
        def run(args: Any, **kwargs: Any) -> Any:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(
                returncode=0,
                stdout="https://bitbucket.org/acme/widget-service.git\n",
                stderr="",
            )

    assert detect_repo(path="/some/dir", runner=_Recording) == "widget-service"
    assert captured["args"] == ["git", "remote", "get-url", "origin"]
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["cwd"] == "/some/dir"
    assert captured["kwargs"]["check"] is False


def test_detect_repo_not_a_git_dir() -> None:
    runner = _fake_runner(returncode=128, stderr="fatal: not a git repository\n")
    with pytest.raises(BBConfigError, match="Not a git repository"):
        detect_repo(runner=runner)


def test_detect_repo_unparseable_remote() -> None:
    runner = _fake_runner(stdout="totally-not-a-url\n")
    with pytest.raises(BBConfigError, match="Could not parse"):
        detect_repo(runner=runner)


def test_detect_repo_no_git_binary() -> None:
    class _NoGit:
        @staticmethod
        def run(*_args: Any, **_kwargs: Any) -> Any:
            raise FileNotFoundError("git not on PATH")

    with pytest.raises(BBConfigError, match="git executable not found"):
        detect_repo(runner=_NoGit)


# =========================================================================
# repo_path
# =========================================================================


def test_repo_path_simple() -> None:
    assert repo_path("acme", "widget-service") == "/repositories/acme/widget-service"


def test_repo_path_rejects_slashes() -> None:
    with pytest.raises(ValueError):
        repo_path("acme/sub", "widget")
    with pytest.raises(ValueError):
        repo_path("acme", "widget/sub")


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_repo_path_rejects_empty_or_whitespace(bad: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        repo_path(bad, "widget")
    with pytest.raises(ValueError, match="non-empty"):
        repo_path("acme", bad)


@pytest.mark.parametrize("bad", [".", ".."])
def test_repo_path_rejects_dot_segments(bad: str) -> None:
    """`/repositories/../widget` after URL normalisation can resolve to
    `/repositories/widget` with the wrong workspace — path-traversal."""
    with pytest.raises(ValueError, match=r"'\.'|'\.\.'"):
        repo_path(bad, "widget")
    with pytest.raises(ValueError, match=r"'\.'|'\.\.'"):
        repo_path("acme", bad)


# =========================================================================
# BBClient HTTP transport
# =========================================================================


class _CaptureOpener:
    """Fake urllib opener that records each request and returns canned JSON.

    Tests use this in place of a real network. Each call to .open() pops one
    response off `responses` and records the Request the caller built so the
    test can assert on URL, method, headers, and body.
    """

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def open(self, req: urllib.request.Request, timeout: float = 30.0) -> Any:
        body = req.data
        # urllib.request.Request stores header keys via `.capitalize()`
        # ("Content-Type" -> "Content-type"). Re-titlecase here so test
        # assertions can use the conventional canonical form.
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
                f"opener received an unexpected request: {req.get_method()} {req.full_url}"
            )
        resp = self.responses.pop(0)
        body_bytes = json.dumps(resp).encode("utf-8") if resp is not None else b""
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


def _expected_basic_auth(user: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


def test_get_constructs_correct_url_and_auth() -> None:
    opener = _CaptureOpener([{"slug": "widget-service"}])
    client = _client(opener)
    result = client.get("/repositories/acme/widget-service")
    assert result == {"slug": "widget-service"}
    assert len(opener.calls) == 1
    call = opener.calls[0]
    assert call["url"] == DEFAULT_API_BASE + "/repositories/acme/widget-service"
    assert call["method"] == "GET"
    assert call["headers"]["Authorization"] == _expected_basic_auth(
        "alice@example.com", "tok-xyz"
    )
    assert call["headers"]["Accept"] == "application/json"
    assert call["body"] is None


def test_get_with_query_params() -> None:
    opener = _CaptureOpener([{"values": []}])
    client = _client(opener)
    client.get(
        "/repositories/acme/widget-service/pullrequests",
        query={"state": "OPEN", "pagelen": 25, "skip": None},
    )
    url = opener.calls[0]["url"]
    assert url.startswith(
        DEFAULT_API_BASE + "/repositories/acme/widget-service/pullrequests?"
    )
    # None values are dropped.
    assert "skip=" not in url
    assert "state=OPEN" in url
    assert "pagelen=25" in url


def test_post_sends_json_body() -> None:
    opener = _CaptureOpener([{"id": 42}])
    client = _client(opener)
    client.post(
        "/repositories/acme/widget-service/pullrequests",
        json_body={
            "title": "Add widget",
            "source": {"branch": {"name": "feat/widget"}},
            "destination": {"branch": {"name": "main"}},
        },
    )
    call = opener.calls[0]
    assert call["method"] == "POST"
    assert call["headers"]["Content-Type"] == "application/json"
    # Assert FULL body shape, not just presence — guards against the
    # mock-returns-success-regardless-of-body anti-pattern.
    assert call["body"] == {
        "title": "Add widget",
        "source": {"branch": {"name": "feat/widget"}},
        "destination": {"branch": {"name": "main"}},
    }


def test_post_without_body() -> None:
    opener = _CaptureOpener([{"approved": True}])
    client = _client(opener)
    client.post("/repositories/acme/widget-service/pullrequests/1/approve")
    call = opener.calls[0]
    assert call["method"] == "POST"
    assert call["body"] is None
    # No Content-Type when there's no body.
    assert "Content-Type" not in call["headers"]


def test_put_sends_json_body() -> None:
    opener = _CaptureOpener([{"ok": True}])
    client = _client(opener)
    client.put("/some/path", json_body={"x": 1})
    call = opener.calls[0]
    assert call["method"] == "PUT"
    assert call["body"] == {"x": 1}


def test_delete_no_body() -> None:
    opener = _CaptureOpener([None])
    client = _client(opener)
    result = client.delete("/repositories/acme/widget-service/pullrequests/1")
    assert result is None
    call = opener.calls[0]
    assert call["method"] == "DELETE"
    assert call["body"] is None


def test_http_error_surfaces_as_bbapierror() -> None:
    class _ErrorOpener:
        def open(self, req: urllib.request.Request, timeout: float = 30.0) -> Any:
            raise urllib.error.HTTPError(
                url=req.full_url,
                code=404,
                msg="Not Found",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"error": {"message": "Repository not found"}}'),
            )

    client = _client(_CaptureOpener([]))
    client._opener = _ErrorOpener()  # type: ignore[assignment]
    with pytest.raises(BBApiError) as exc:
        client.get("/repositories/acme/missing")
    assert exc.value.status == 404
    assert "Repository not found" in exc.value.body


def test_urlerror_surfaces_as_bbapierror_with_status_zero() -> None:
    """A DNS / TLS / connection / timeout failure raises urllib.error.URLError
    (which is HTTPError's parent class). Without an explicit handler, this
    would escape the BBApiError contract. Wrap with status=0 (the documented
    transport-error sentinel)."""

    class _NetworkDownOpener:
        def open(self, req: urllib.request.Request, timeout: float = 30.0) -> Any:
            raise urllib.error.URLError("Name or service not known")

    client = _client(_CaptureOpener([]))
    client._opener = _NetworkDownOpener()  # type: ignore[assignment]
    with pytest.raises(BBApiError) as exc:
        client.get("/repos")
    assert exc.value.status == 0
    assert "network error" in exc.value.body.lower()


def test_request_uses_default_timeout() -> None:
    opener = _CaptureOpener([{"ok": True}])
    cfg = BBConfig(
        user="alice@example.com", token="tok", workspace="acme", api_base=DEFAULT_API_BASE
    )
    client = BBClient(cfg, opener=opener, timeout=12.5)
    client.get("/repos")
    assert opener.calls[0]["timeout"] == 12.5


def test_request_per_call_timeout_overrides_default() -> None:
    """A long-running call (log streaming, large diff) needs to extend the
    default timeout. Each public method exposes a per-call override."""
    opener = _CaptureOpener([{"ok": True}, {"ok": True}, {"ok": True}, None])
    cfg = BBConfig(
        user="alice@example.com", token="tok", workspace="acme", api_base=DEFAULT_API_BASE
    )
    client = BBClient(cfg, opener=opener, timeout=5.0)
    client.get("/a", timeout=120.0)
    client.post("/b", json_body={"x": 1}, timeout=60.0)
    client.put("/c", json_body={"y": 2}, timeout=30.0)
    client.delete("/d", timeout=15.0)
    assert [c["timeout"] for c in opener.calls] == [120.0, 60.0, 30.0, 15.0]


@pytest.mark.parametrize(
    "bad_value",
    [{"nested": "dict"}, ["list", {"of": "stuff"}], object()],
)
def test_query_rejects_non_scalar_values(bad_value: Any) -> None:
    opener = _CaptureOpener([])
    client = _client(opener)
    with pytest.raises(TypeError, match="query"):
        client.get("/repos", query={"q": bad_value})
    # Confirm nothing went out on the wire.
    assert opener.calls == []


def test_query_accepts_scalar_list() -> None:
    opener = _CaptureOpener([{"values": []}])
    client = _client(opener)
    client.get("/repos", query={"tag": ["a", "b"]})
    url = opener.calls[0]["url"]
    # urllib serialises lists as repeated query keys with doseq=True.
    assert "tag=a" in url
    assert "tag=b" in url


# =========================================================================
# Pagination
# =========================================================================


def test_paginate_walks_pages() -> None:
    base = DEFAULT_API_BASE + "/repositories/acme/widget-service/pullrequests"
    opener = _CaptureOpener(
        [
            {"values": [{"id": 1}, {"id": 2}], "next": base + "?page=2"},
            {"values": [{"id": 3}], "next": base + "?page=3"},
            {"values": [{"id": 4}]},  # last page, no `next`
        ]
    )
    client = _client(opener)
    items = list(client.paginate("/repositories/acme/widget-service/pullrequests"))
    assert [i["id"] for i in items] == [1, 2, 3, 4]
    assert len(opener.calls) == 3


def test_paginate_stops_on_stuck_cursor() -> None:
    # Server returns the same `next` URL twice — defend against the loop.
    base = DEFAULT_API_BASE + "/repos"
    opener = _CaptureOpener(
        [
            {"values": [{"id": 1}], "next": base + "?page=2"},
            {"values": [{"id": 2}], "next": base + "?page=2"},  # stuck
        ]
    )
    client = _client(opener)
    items = list(client.paginate("/repos"))
    assert [i["id"] for i in items] == [1, 2]
    # Two requests, then the cursor-equality check breaks the loop before
    # the third request goes out.
    assert len(opener.calls) == 2


def test_paginate_iteration_cap_raises() -> None:
    base = DEFAULT_API_BASE + "/repos"
    # Each response advances the cursor by one, so stuck-cursor detection
    # never trips. We bound the test at a low max_iterations to keep it fast.
    responses = [
        {"values": [{"i": i}], "next": f"{base}?page={i + 2}"} for i in range(10)
    ]
    opener = _CaptureOpener(responses)
    client = _client(opener)
    with pytest.raises(BBApiError, match="exceeded"):
        list(client.paginate("/repos", max_iterations=5))


def test_paginate_refuses_cross_host_next() -> None:
    """If `next` points at a different host than api_base, we refuse to
    follow it. Defends against an upstream-bug or man-in-the-middle scenario
    where the cursor URL has been mangled."""
    opener = _CaptureOpener(
        [
            {
                "values": [{"id": 1}],
                "next": "https://evil.example.com/2.0/repos?page=2",
            },
        ]
    )
    client = _client(opener)
    with pytest.raises(BBApiError, match="host mismatch"):
        list(client.paginate("/repos"))


def test_paginate_refuses_prefix_trick_next() -> None:
    """A `next` URL that string-prefixes api_base but continues into a
    different host slips past a naive `startswith()` check. Defends by
    requiring the separator after api_base to be `/` or `?`."""
    sneaky = DEFAULT_API_BASE + "evil.example.com/repos?page=2"
    opener = _CaptureOpener(
        [
            {"values": [{"id": 1}], "next": sneaky},
        ]
    )
    client = _client(opener)
    with pytest.raises(BBApiError, match="host mismatch"):
        list(client.paginate("/repos"))


def test_paginate_missing_values_key_raises() -> None:
    """A malformed page (or proxy-corrupted response) without `values` should
    be loud, not silently treated as empty. The previous .get('values', [])
    would have advanced through `next` with a silent hole in the result set."""
    opener = _CaptureOpener(
        [
            {"next": DEFAULT_API_BASE + "/repos?page=2"},  # no `values`
        ]
    )
    client = _client(opener)
    with pytest.raises(BBApiError, match="missing 'values'"):
        list(client.paginate("/repos"))


def test_paginate_non_string_next_raises() -> None:
    """`next: 123` would otherwise crash on `.startswith()` with
    AttributeError, bypassing the BBApiError contract."""
    opener = _CaptureOpener(
        [
            {"values": [{"id": 1}], "next": 12345},
        ]
    )
    client = _client(opener)
    with pytest.raises(BBApiError, match="must be a string"):
        list(client.paginate("/repos"))


def test_paginate_yields_nothing_on_empty_first_page() -> None:
    opener = _CaptureOpener([{"values": []}])
    client = _client(opener)
    assert list(client.paginate("/repos")) == []
