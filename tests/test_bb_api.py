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


def test_load_config_dotenv_lower_precedence(tmp_path: Path) -> None:
    cfg_path = tmp_path / "primary.cfg"
    cfg_path.write_text(
        "BB_USER=alice@example.com\nBB_TOKEN=primary-tok\nBB_WORKSPACE=acme\n"
    )
    dotenv = tmp_path / ".env"
    dotenv.write_text("BB_TOKEN=dotenv-tok\nBB_WORKSPACE=other\n")

    cfg = load_config(env={}, config_path=cfg_path, dotenv_path=dotenv)
    # Primary config wins over dotenv (matches the bash script's load order:
    # ~/.config/bb/config is loaded last, so its values overwrite .env).
    assert cfg.token == "primary-tok"
    assert cfg.workspace == "acme"


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


def test_paginate_yields_nothing_on_empty_first_page() -> None:
    opener = _CaptureOpener([{"values": []}])
    client = _client(opener)
    assert list(client.paginate("/repos")) == []
