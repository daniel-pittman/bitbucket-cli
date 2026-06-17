"""
Tests for bb_ops repos / branches / vars / downloads / commits operations.

Same discipline as the pipelines and PRs test files: assert URL +
method + body shape per HTTP touchpoint; never just response value.
Boundary-validation rejections assert `opener.calls == []` to prove no
network IO happened on bad input.

All fixture data is fictional (acme / widget-service / alice / bob).
"""

from __future__ import annotations

import io
import json
import urllib.error
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
# workspaces_list
# ===========================================================================


def _workspace_value(slug: str, *, admin: bool = False) -> dict[str, Any]:
    """Mirror the shape Bitbucket's /user/workspaces actually returns.

    The new endpoint (CHANGE-3022) uses a sparse `workspace_access`
    envelope — no `name` / no `permission` string, just slug + uuid +
    links under `.workspace`, plus a top-level `administrator` bool.
    Tests pin the shape so a refactor that flattens or renames keys
    breaks loudly instead of silently changing the agent surface.
    """
    return {
        "type": "workspace_access",
        "administrator": admin,
        "workspace": {
            "type": "workspace_base",
            "uuid": "{" + slug + "-uuid}",
            "slug": slug,
            "links": {
                "self": {
                    "href": f"https://api.bitbucket.org/2.0/workspaces/{slug}"
                }
            },
        },
    }


class TestWorkspacesList:
    def test_hits_user_workspaces_endpoint(self) -> None:
        # The whole point: this op does NOT use BB_WORKSPACE — it's a
        # user-scoped listing. The URL must be /user/workspaces, not
        # /workspaces (deprecated CHANGE-2770) and not anything
        # workspace-scoped.
        opener = _CaptureOpener(
            [{"values": [_workspace_value("acme"), _workspace_value("widget-co", admin=True)]}]
        )
        result = bb_ops.workspaces_list(_client(opener))
        assert len(result) == 2
        assert result[0]["workspace"]["slug"] == "acme"
        assert result[1]["administrator"] is True
        url = opener.calls[0]["url"]
        assert url.startswith(DEFAULT_API_BASE + "/user/workspaces?")
        assert "pagelen=100" in url
        assert opener.calls[0]["method"] == "GET"
        # No body on GETs.
        assert opener.calls[0]["body"] is None

    def test_returns_raw_envelope_not_just_slug(self) -> None:
        """Callers (agent, bash) decide how to render — we surface the
        full Bitbucket envelope including the administrator bool and
        the workspace.uuid that downstream tools may want for explicit
        targeting. Don't pre-flatten."""
        opener = _CaptureOpener(
            [{"values": [_workspace_value("daniel-pittman", admin=True)]}]
        )
        result = bb_ops.workspaces_list(_client(opener))
        assert result[0]["workspace"]["uuid"] == "{daniel-pittman-uuid}"
        assert result[0]["administrator"] is True
        # The legacy fields callers might expect MUST stay absent — the
        # new schema doesn't carry them. Pin this so a future "helpful"
        # mutation that injects defaults doesn't mask the change.
        assert "name" not in result[0]["workspace"]
        assert "permission" not in result[0]

    def test_count_walks_pages(self) -> None:
        opener = _CaptureOpener(
            [
                {
                    "values": [_workspace_value(f"ws-{i}") for i in range(100)],
                    "next": DEFAULT_API_BASE + "/user/workspaces?page=2",
                },
                {"values": [_workspace_value(f"ws-{i}") for i in range(100, 150)]},
            ]
        )
        result = bb_ops.workspaces_list(_client(opener), count=150)
        assert len(result) == 150
        assert "pagelen=100" in opener.calls[0]["url"]
        # Pagination must follow the `next` link.
        assert len(opener.calls) == 2
        assert "page=2" in opener.calls[1]["url"]

    def test_count_caps_response(self) -> None:
        # Even if Bitbucket returns more than `count`, the caller
        # gets exactly `count` items (matches repos_list semantics).
        opener = _CaptureOpener(
            [{"values": [_workspace_value(f"ws-{i}") for i in range(10)]}]
        )
        result = bb_ops.workspaces_list(_client(opener), count=3)
        assert len(result) == 3

    def test_403_no_scope_translates_from_httperror(self) -> None:
        """A token without `read:workspace:bitbucket` returns 403 with
        Bitbucket's "credentials lack one or more required privilege
        scopes" body. This test raises the *real* urllib HTTPError from
        the opener (not a pre-built BBApiError) so it exercises the
        bb_api._request HTTPError→BBApiError translation layer — a
        regression there would surface here, not only in test_bb_api.
        The scope name must survive into BBApiError.body so the MCP
        wrapper / bash command can tell the user which scope to add."""
        from bb_api import BBApiError
        body = (
            '{"error": {"message": "Your credentials lack one or more required '
            'privilege scopes.", "detail": {"required": ["read:workspace:bitbucket"]}}}'
        )
        http_err = urllib.error.HTTPError(
            url=DEFAULT_API_BASE + "/user/workspaces?pagelen=100",
            code=403,
            msg="Forbidden",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(body.encode("utf-8")),
        )
        opener = _CaptureOpener([http_err])
        with pytest.raises(BBApiError) as exc:
            bb_ops.workspaces_list(_client(opener))
        assert exc.value.status == 403
        # Body must survive the translation so the scope name is recoverable.
        assert "read:workspace:bitbucket" in exc.value.body

    @pytest.mark.parametrize("bad", [0, -1, True, False, "ten", None, 1.5])
    def test_rejects_non_positive_count(self, bad: Any) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="count"):
            bb_ops.workspaces_list(_client(opener), count=bad)
        assert opener.calls == []


# ===========================================================================
# projects_list
# ===========================================================================


def _project_value(key: str, name: str) -> dict[str, Any]:
    """Mirror the shape Bitbucket's /workspaces/{ws}/projects returns:
    a project record keyed by the short `.key` used in repo bodies,
    plus `.name` and `.uuid`."""
    return {
        "type": "project",
        "key": key,
        "name": name,
        "uuid": "{" + key.lower() + "-uuid}",
        "links": {
            "self": {
                "href": f"https://api.bitbucket.org/2.0/workspaces/acme/projects/{key}"
            }
        },
    }


class TestProjectsList:
    def test_hits_workspace_projects_endpoint(self) -> None:
        # Must be /workspaces/{ws}/projects (the workspace-scoped projects
        # collection), NOT /repositories/... — projects are a workspace
        # resource, not a repo one.
        opener = _CaptureOpener(
            [{"values": [_project_value("WID", "Widget"), _project_value("SVC", "Services")]}]
        )
        result = bb_ops.projects_list(_client(opener))
        assert len(result) == 2
        assert result[0]["key"] == "WID"
        assert result[1]["name"] == "Services"
        url = opener.calls[0]["url"]
        assert url.startswith(DEFAULT_API_BASE + "/workspaces/acme/projects?")
        assert "pagelen=100" in url
        assert opener.calls[0]["method"] == "GET"
        assert opener.calls[0]["body"] is None

    def test_default_workspace_from_client(self) -> None:
        # workspace=None defaults to client.config.workspace ("acme").
        opener = _CaptureOpener([{"values": [_project_value("WID", "Widget")]}])
        bb_ops.projects_list(_client(opener))
        assert opener.calls[0]["url"].startswith(
            DEFAULT_API_BASE + "/workspaces/acme/projects?"
        )

    def test_explicit_workspace_overrides_config(self) -> None:
        opener = _CaptureOpener([{"values": [_project_value("WID", "Widget")]}])
        bb_ops.projects_list(_client(opener), workspace="widget-co")
        assert opener.calls[0]["url"].startswith(
            DEFAULT_API_BASE + "/workspaces/widget-co/projects?"
        )

    def test_returns_raw_records_not_just_key(self) -> None:
        # Callers (agent, bash) decide how to render — surface the full
        # record including uuid so downstream tools can target by uuid.
        opener = _CaptureOpener([{"values": [_project_value("WID", "Widget")]}])
        result = bb_ops.projects_list(_client(opener))
        assert result[0]["uuid"] == "{wid-uuid}"

    def test_count_walks_pages(self) -> None:
        opener = _CaptureOpener(
            [
                {
                    "values": [_project_value(f"P{i}", f"Proj {i}") for i in range(100)],
                    "next": DEFAULT_API_BASE + "/workspaces/acme/projects?page=2",
                },
                {"values": [_project_value(f"P{i}", f"Proj {i}") for i in range(100, 150)]},
            ]
        )
        result = bb_ops.projects_list(_client(opener), count=150)
        assert len(result) == 150
        assert len(opener.calls) == 2
        assert "page=2" in opener.calls[1]["url"]

    def test_count_caps_response(self) -> None:
        opener = _CaptureOpener(
            [{"values": [_project_value(f"P{i}", f"Proj {i}") for i in range(10)]}]
        )
        result = bb_ops.projects_list(_client(opener), count=3)
        assert len(result) == 3

    def test_403_no_scope_translates_from_httperror(self) -> None:
        """A token without `read:project:bitbucket` returns 403 with
        Bitbucket's scope-error body. Raise the real urllib HTTPError so
        the HTTPError→BBApiError translation layer is exercised; the
        scope name must survive into BBApiError.body so the caller can
        tell the user which scope to add."""
        from bb_api import BBApiError
        body = (
            '{"error": {"message": "Your credentials lack one or more required '
            'privilege scopes.", "detail": {"required": ["read:project:bitbucket"]}}}'
        )
        http_err = urllib.error.HTTPError(
            url=DEFAULT_API_BASE + "/workspaces/acme/projects?pagelen=100",
            code=403,
            msg="Forbidden",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(body.encode("utf-8")),
        )
        opener = _CaptureOpener([http_err])
        with pytest.raises(BBApiError) as exc:
            bb_ops.projects_list(_client(opener))
        assert exc.value.status == 403
        assert "read:project:bitbucket" in exc.value.body

    @pytest.mark.parametrize("bad_ws", ["", "   ", "acme/widget", ".", ".."])
    def test_rejects_bad_workspace_no_network(self, bad_ws: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError):
            bb_ops.projects_list(_client(opener), workspace=bad_ws)
        assert opener.calls == []

    @pytest.mark.parametrize("bad", [0, -1, True, False, "ten", None, 1.5])
    def test_rejects_non_positive_count(self, bad: Any) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="count"):
            bb_ops.projects_list(_client(opener), count=bad)
        assert opener.calls == []


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
# repo_create
# ===========================================================================


class TestRepoCreate:
    def test_posts_to_repo_path_with_default_private_git(self) -> None:
        opener = _CaptureOpener(
            [{"full_name": "acme/widget-service", "is_private": True}]
        )
        result = bb_ops.repo_create(
            _client(opener), "acme", "widget-service"
        )
        assert result["full_name"] == "acme/widget-service"
        call = opener.calls[0]
        # POST to the SAME path repo_show GETs.
        assert call["url"] == _repo_url()
        assert call["method"] == "POST"
        # Default body: scm git, private true, no project / description.
        assert call["body"] == {"scm": "git", "is_private": True}

    def test_public_flag(self) -> None:
        opener = _CaptureOpener([{"full_name": "acme/widget-service"}])
        bb_ops.repo_create(
            _client(opener), "acme", "widget-service", is_private=False
        )
        assert opener.calls[0]["body"]["is_private"] is False

    def test_project_key_in_body(self) -> None:
        opener = _CaptureOpener([{"full_name": "acme/widget-service"}])
        bb_ops.repo_create(
            _client(opener), "acme", "widget-service", project_key="WID"
        )
        assert opener.calls[0]["body"]["project"] == {"key": "WID"}

    def test_description_in_body(self) -> None:
        opener = _CaptureOpener([{"full_name": "acme/widget-service"}])
        bb_ops.repo_create(
            _client(opener),
            "acme",
            "widget-service",
            description="A service for widgets",
        )
        assert opener.calls[0]["body"]["description"] == "A service for widgets"

    def test_omits_project_and_description_when_none(self) -> None:
        # Parity with the bash omit-when-empty body. The keys must be
        # ABSENT, not present-with-null — Bitbucket treats a null project
        # differently from a missing one.
        opener = _CaptureOpener([{"full_name": "acme/widget-service"}])
        bb_ops.repo_create(_client(opener), "acme", "widget-service")
        body = opener.calls[0]["body"]
        assert "project" not in body
        assert "description" not in body

    def test_project_strips_whitespace(self) -> None:
        opener = _CaptureOpener([{"full_name": "acme/widget-service"}])
        bb_ops.repo_create(
            _client(opener), "acme", "widget-service", project_key="  WID  "
        )
        assert opener.calls[0]["body"]["project"] == {"key": "WID"}

    @pytest.mark.parametrize("bad_slug", ["", "   ", "a/b", ".", ".."])
    def test_rejects_bad_slug_no_network(self, bad_slug: str) -> None:
        # repo_path enforces the slug contract at the boundary; no POST
        # may fire on a malformed slug.
        opener = _CaptureOpener([])
        with pytest.raises(ValueError):
            bb_ops.repo_create(_client(opener), "acme", bad_slug)
        assert opener.calls == []

    @pytest.mark.parametrize("bad_project", ["", "   "])
    def test_rejects_empty_project_when_provided(self, bad_project: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="project_key"):
            bb_ops.repo_create(
                _client(opener), "acme", "widget-service", project_key=bad_project
            )
        assert opener.calls == []


# ===========================================================================
# repo_update
# ===========================================================================


class TestRepoUpdate:
    def test_puts_project_to_repo_path(self) -> None:
        # The move use case: PUT to the SAME path repo_show GETs, with a
        # body carrying only the new project key.
        opener = _CaptureOpener(
            [{"full_name": "acme/widget-service", "project": {"key": "WID"}}]
        )
        result = bb_ops.repo_update(
            _client(opener), "acme", "widget-service", project_key="WID"
        )
        assert result["project"]["key"] == "WID"
        call = opener.calls[0]
        assert call["url"] == _repo_url()
        assert call["method"] == "PUT"
        # Body carries ONLY the project — description absent (not sent).
        assert call["body"] == {"project": {"key": "WID"}}

    def test_puts_description_only(self) -> None:
        opener = _CaptureOpener([{"full_name": "acme/widget-service"}])
        bb_ops.repo_update(
            _client(opener), "acme", "widget-service", description="New desc"
        )
        body = opener.calls[0]["body"]
        assert body == {"description": "New desc"}
        assert "project" not in body

    def test_puts_both_fields(self) -> None:
        opener = _CaptureOpener([{"full_name": "acme/widget-service"}])
        bb_ops.repo_update(
            _client(opener),
            "acme",
            "widget-service",
            project_key="WID",
            description="New desc",
        )
        assert opener.calls[0]["body"] == {
            "project": {"key": "WID"},
            "description": "New desc",
        }

    def test_project_strips_whitespace(self) -> None:
        opener = _CaptureOpener([{"full_name": "acme/widget-service"}])
        bb_ops.repo_update(
            _client(opener), "acme", "widget-service", project_key="  WID  "
        )
        assert opener.calls[0]["body"]["project"] == {"key": "WID"}

    def test_empty_description_is_a_change_and_is_sent(self) -> None:
        # An empty-string description is an intentional clear, NOT "no
        # field supplied". It must be sent (body present) and must NOT
        # trip the "at least one field" guard. project_key=None means the
        # project key stays absent from the body.
        opener = _CaptureOpener([{"full_name": "acme/widget-service"}])
        bb_ops.repo_update(
            _client(opener), "acme", "widget-service", description=""
        )
        assert opener.calls[0]["body"] == {"description": ""}

    def test_rejects_no_fields_no_network(self) -> None:
        # A PUT with an empty body is a no-op round-trip. Reject at the
        # boundary before any API call fires.
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="at least one field"):
            bb_ops.repo_update(_client(opener), "acme", "widget-service")
        assert opener.calls == []

    @pytest.mark.parametrize("bad_slug", ["", "   ", "a/b", ".", ".."])
    def test_rejects_bad_slug_no_network(self, bad_slug: str) -> None:
        # repo_path enforces the slug contract; no PUT may fire on a
        # malformed slug. Pass a valid field so the rejection is the slug,
        # not the empty-body guard.
        opener = _CaptureOpener([])
        with pytest.raises(ValueError):
            bb_ops.repo_update(
                _client(opener), "acme", bad_slug, project_key="WID"
            )
        assert opener.calls == []

    @pytest.mark.parametrize("bad_project", ["", "   "])
    def test_rejects_empty_project_when_provided(self, bad_project: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="project_key"):
            bb_ops.repo_update(
                _client(opener), "acme", "widget-service", project_key=bad_project
            )
        assert opener.calls == []


# ===========================================================================
# vars_set (create-or-update)
# ===========================================================================


def _vars_url() -> str:
    return _repo_url() + "/pipelines_config/variables/"


class TestVarsSet:
    def test_creates_when_not_found_posts(self) -> None:
        # First call: list (empty) → not found. Second call: POST create.
        opener = _CaptureOpener(
            [
                {"values": []},
                {"key": "AWS_REGION", "uuid": "{new-uuid}", "value": "us-east-1"},
            ]
        )
        result = bb_ops.vars_set(
            _client(opener), "acme", "widget-service", "AWS_REGION", "us-east-1"
        )
        assert result["key"] == "AWS_REGION"
        # Lookup is a GET to the variables list.
        assert opener.calls[0]["method"] == "GET"
        assert opener.calls[0]["url"].startswith(_vars_url() + "?")
        # Create is a POST to the collection (trailing slash, no uuid).
        create = opener.calls[1]
        assert create["method"] == "POST"
        assert create["url"] == _vars_url()
        assert create["body"] == {
            "key": "AWS_REGION",
            "value": "us-east-1",
            "secured": False,
        }

    def test_updates_when_found_puts_to_uuid(self) -> None:
        opener = _CaptureOpener(
            [
                {"values": [{"key": "AWS_REGION", "uuid": "{abc-123}"}]},
                {"key": "AWS_REGION", "uuid": "{abc-123}", "value": "us-west-2"},
            ]
        )
        bb_ops.vars_set(
            _client(opener), "acme", "widget-service", "AWS_REGION", "us-west-2"
        )
        update = opener.calls[1]
        assert update["method"] == "PUT"
        # uuid is URL-encoded into the path; braces become %7B / %7D.
        assert update["url"] == _vars_url() + "%7Babc-123%7D"
        assert update["body"]["value"] == "us-west-2"

    def test_secured_flag_in_body(self) -> None:
        opener = _CaptureOpener(
            [{"values": []}, {"key": "AWS_SECRET", "uuid": "{u}", "value": None}]
        )
        bb_ops.vars_set(
            _client(opener),
            "acme",
            "widget-service",
            "AWS_SECRET",
            "super-secret",
            secured=True,
        )
        assert opener.calls[1]["body"]["secured"] is True

    def test_existing_passed_in_skips_lookup(self) -> None:
        # When the caller pre-fetches the existing var, vars_set must NOT
        # paginate the list again — only the PUT fires.
        opener = _CaptureOpener(
            [{"key": "AWS_REGION", "uuid": "{abc-123}", "value": "us-west-2"}]
        )
        bb_ops.vars_set(
            _client(opener),
            "acme",
            "widget-service",
            "AWS_REGION",
            "us-west-2",
            existing={"key": "AWS_REGION", "uuid": "{abc-123}"},
        )
        assert len(opener.calls) == 1
        assert opener.calls[0]["method"] == "PUT"

    def test_find_walks_all_pages_before_create(self) -> None:
        # The match is on page 2. vars_set must follow `next` and find it
        # (PUT), not stop at page 1 and POST a duplicate.
        opener = _CaptureOpener(
            [
                {
                    "values": [{"key": "OTHER", "uuid": "{x}"}],
                    "next": _vars_url() + "?page=2",
                },
                {"values": [{"key": "TARGET", "uuid": "{target-uuid}"}]},
                {"key": "TARGET", "uuid": "{target-uuid}"},
            ]
        )
        bb_ops.vars_set(
            _client(opener), "acme", "widget-service", "TARGET", "v"
        )
        # 2 GETs (page 1 + page 2) then a PUT.
        assert [c["method"] for c in opener.calls] == ["GET", "GET", "PUT"]
        assert opener.calls[2]["url"] == _vars_url() + "%7Btarget-uuid%7D"

    @pytest.mark.parametrize("bad_key", ["", "   ", "\n"])
    def test_rejects_empty_key_no_network(self, bad_key: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="key"):
            bb_ops.vars_set(
                _client(opener), "acme", "widget-service", bad_key, "v"
            )
        assert opener.calls == []

    def test_rejects_non_string_value_no_network(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="value"):
            bb_ops.vars_set(
                _client(opener), "acme", "widget-service", "KEY", 123  # type: ignore[arg-type]
            )
        assert opener.calls == []

    def test_matched_key_with_null_uuid_raises_not_create(self) -> None:
        # A matched entry whose uuid is null/missing must raise rather than
        # fall through to POST (which would create a duplicate of a key that
        # already exists). Pin BBOpNotFound and assert no POST fired.
        opener = _CaptureOpener(
            [{"values": [{"key": "AWS_REGION", "uuid": None}]}]
        )
        with pytest.raises(bb_ops.BBOpNotFound, match="no uuid"):
            bb_ops.vars_set(
                _client(opener), "acme", "widget-service", "AWS_REGION", "v"
            )
        # Only the lookup GET happened; no POST / PUT.
        assert [c["method"] for c in opener.calls] == ["GET"]

    def test_prefetched_none_skips_second_lookup_on_create(self) -> None:
        # When the caller pre-fetched and found the key ABSENT (existing=None),
        # vars_set must go straight to POST without re-paginating. Only ONE
        # call (the POST) should fire.
        opener = _CaptureOpener([{"key": "NEW", "uuid": "{u}"}])
        bb_ops.vars_set(
            _client(opener), "acme", "widget-service", "NEW", "v",
            existing=None,
        )
        assert len(opener.calls) == 1
        assert opener.calls[0]["method"] == "POST"

    def test_omitted_existing_does_lookup_then_create(self) -> None:
        # Default (existing omitted = _NOT_PREFETCHED): vars_set looks up
        # (GET, empty) then creates (POST).
        opener = _CaptureOpener([{"values": []}, {"key": "NEW", "uuid": "{u}"}])
        bb_ops.vars_set(
            _client(opener), "acme", "widget-service", "NEW", "v"
        )
        assert [c["method"] for c in opener.calls] == ["GET", "POST"]

    def test_base_with_inconsistent_scope_rejected_no_network(self) -> None:
        # Passing base= must not bypass scope/environment validation: a
        # deployment scope with no environment is rejected even with base.
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="environment"):
            bb_ops.vars_set(
                _client(opener), "acme", "widget-service", "K", "v",
                scope="deployment", environment=None,
                base="/some/base/variables/",
            )
        assert opener.calls == []


# ===========================================================================
# vars_delete (repo scope)
# ===========================================================================


class TestVarsDelete:
    def test_resolves_key_then_deletes(self) -> None:
        # First: list (find by key). Second: DELETE to .../variables/{uuid}.
        opener = _CaptureOpener(
            [
                {"values": [{"key": "AWS_ACCESS_KEY_ID", "uuid": "{abc-123}"}]},
                None,  # DELETE → 204 / no body
            ]
        )
        result = bb_ops.vars_delete(
            _client(opener), "acme", "widget-service", "AWS_ACCESS_KEY_ID"
        )
        assert result == {
            "key": "AWS_ACCESS_KEY_ID",
            "scope": "repo",
            "environment": None,
            "uuid": "{abc-123}",
        }
        # Lookup is a GET to the variables list.
        assert opener.calls[0]["method"] == "GET"
        assert opener.calls[0]["url"].startswith(_vars_url() + "?")
        # Delete is a DELETE to the uuid path (braces URL-encoded).
        delete = opener.calls[1]
        assert delete["method"] == "DELETE"
        assert delete["url"] == _vars_url() + "%7Babc-123%7D"
        # No body on a DELETE.
        assert delete["body"] is None

    def test_find_walks_all_pages_before_delete(self) -> None:
        # The match is on page 2. vars_delete must follow `next` and find
        # it, not stop at page 1 and report not-found.
        opener = _CaptureOpener(
            [
                {
                    "values": [{"key": "OTHER", "uuid": "{x}"}],
                    "next": _vars_url() + "?page=2",
                },
                {"values": [{"key": "TARGET", "uuid": "{target-uuid}"}]},
                None,  # DELETE
            ]
        )
        bb_ops.vars_delete(_client(opener), "acme", "widget-service", "TARGET")
        assert [c["method"] for c in opener.calls] == ["GET", "GET", "DELETE"]
        assert opener.calls[2]["url"] == _vars_url() + "%7Btarget-uuid%7D"

    def test_not_found_raises_before_any_delete(self) -> None:
        # The key isn't present → BBOpNotFound, and NO DELETE fires (only
        # the lookup GET happened). A typo can't silently no-op or write.
        opener = _CaptureOpener([{"values": [{"key": "OTHER", "uuid": "{x}"}]}])
        with pytest.raises(bb_ops.BBOpNotFound, match="MISSING"):
            bb_ops.vars_delete(
                _client(opener), "acme", "widget-service", "MISSING"
            )
        assert [c["method"] for c in opener.calls] == ["GET"]

    def test_matched_but_no_uuid_raises_before_delete(self) -> None:
        # An entry that matches by key but has no uuid can't be addressed
        # for DELETE — raise rather than build a collection-path DELETE.
        opener = _CaptureOpener([{"values": [{"key": "WEIRD", "uuid": None}]}])
        with pytest.raises(bb_ops.BBOpNotFound, match="no uuid"):
            bb_ops.vars_delete(
                _client(opener), "acme", "widget-service", "WEIRD"
            )
        assert [c["method"] for c in opener.calls] == ["GET"]

    def test_key_stripped_before_lookup(self) -> None:
        opener = _CaptureOpener(
            [{"values": [{"key": "AWS_REGION", "uuid": "{u}"}]}, None]
        )
        bb_ops.vars_delete(
            _client(opener), "acme", "widget-service", "  AWS_REGION  "
        )
        # The DELETE happened, meaning the stripped key matched the stored
        # "AWS_REGION" (an unstripped key would have been not-found).
        assert opener.calls[1]["method"] == "DELETE"

    @pytest.mark.parametrize("bad_key", ["", "   ", "\n"])
    def test_rejects_empty_key_no_network(self, bad_key: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="key"):
            bb_ops.vars_delete(_client(opener), "acme", "widget-service", bad_key)
        assert opener.calls == []


# ===========================================================================
# Variable SCOPES: workspace + deployment (env-UUID resolution)
# ===========================================================================


def _ws_vars_url() -> str:
    # HYPHEN form, verified against the live API.
    return DEFAULT_API_BASE + "/workspaces/acme/pipelines-config/variables/"


def _env_list_url() -> str:
    return _repo_url() + "/environments/"


def _dep_vars_url(enc_uuid: str) -> str:
    return _repo_url() + f"/deployments_config/environments/{enc_uuid}/variables/"


class TestVariablesBaseRouting:
    def test_repo_scope_path(self) -> None:
        opener = _CaptureOpener([])
        base = bb_ops._variables_base(
            _client(opener), "acme", "widget-service", scope="repo"
        )
        assert base == "/repositories/acme/widget-service/pipelines_config/variables/"
        # Pure path construction; no network.
        assert opener.calls == []

    def test_workspace_scope_path_uses_hyphen(self) -> None:
        opener = _CaptureOpener([])
        base = bb_ops._variables_base(
            _client(opener), "acme", None, scope="workspace"
        )
        # HYPHEN, not underscore (underscore 404s at the workspace scope).
        assert base == "/workspaces/acme/pipelines-config/variables/"
        assert "pipelines-config" in base and "pipelines_config" not in base
        assert opener.calls == []

    def test_bad_scope_rejected(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="scope"):
            bb_ops._variables_base(_client(opener), "acme", "r", scope="nope")
        assert opener.calls == []

    def test_repo_scope_rejects_environment(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="environment"):
            bb_ops._variables_base(
                _client(opener), "acme", "r", scope="repo", environment="Dev"
            )
        assert opener.calls == []

    def test_deployment_scope_requires_environment(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="environment"):
            bb_ops._variables_base(
                _client(opener), "acme", "r", scope="deployment"
            )
        assert opener.calls == []

    def test_deployment_scope_requires_repo(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="repo"):
            bb_ops._variables_base(
                _client(opener), "acme", None, scope="deployment", environment="Dev"
            )
        assert opener.calls == []


class TestResolveEnvironmentUuid:
    _ENVS = {
        "values": [
            {"name": "Development", "slug": "development", "uuid": "{env-dev}"},
            {"name": "Production", "slug": "production", "uuid": "{env-prod}"},
        ]
    }

    def test_matches_by_name_case_insensitive(self) -> None:
        opener = _CaptureOpener([self._ENVS])
        uuid = bb_ops._resolve_environment_uuid(
            _client(opener), "acme", "widget-service", "development"
        )
        assert uuid == "{env-dev}"
        assert opener.calls[0]["url"].startswith(_env_list_url() + "?")

    def test_matches_by_slug(self) -> None:
        opener = _CaptureOpener([self._ENVS])
        uuid = bb_ops._resolve_environment_uuid(
            _client(opener), "acme", "widget-service", "production"
        )
        assert uuid == "{env-prod}"

    def test_no_match_raises_with_available_list(self) -> None:
        opener = _CaptureOpener([self._ENVS])
        with pytest.raises(bb_ops.BBOpNotFound, match="Staging"):
            bb_ops._resolve_environment_uuid(
                _client(opener), "acme", "widget-service", "Staging"
            )

    @pytest.mark.parametrize("bad", ["", "   ", "\n"])
    def test_rejects_empty_env_name_no_network(self, bad: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="environment"):
            bb_ops._resolve_environment_uuid(
                _client(opener), "acme", "widget-service", bad
            )
        assert opener.calls == []

    def test_matched_env_with_null_uuid_raises_distinct_error(self) -> None:
        # A matched env whose uuid is null must raise "found but has no uuid",
        # NOT the misleading "no environment named X" (which would imply the
        # name didn't match at all).
        opener = _CaptureOpener(
            [{"values": [{"name": "Production", "slug": "production", "uuid": None}]}]
        )
        with pytest.raises(bb_ops.BBOpNotFound, match="no uuid"):
            bb_ops._resolve_environment_uuid(
                _client(opener), "acme", "widget-service", "Production"
            )


class TestVarsListWorkspaceScope:
    def test_lists_at_workspace_scope(self) -> None:
        opener = _CaptureOpener(
            [{"values": [{"key": "GLOBAL_VAR", "secured": False, "value": "x"}]}]
        )
        result = bb_ops.vars_list(
            _client(opener), "acme", None, scope="workspace"
        )
        assert result[0]["key"] == "GLOBAL_VAR"
        assert opener.calls[0]["url"].startswith(_ws_vars_url() + "?")
        assert "pagelen=100" in opener.calls[0]["url"]


class TestVarsListDeploymentScope:
    def test_resolves_env_then_lists(self) -> None:
        opener = _CaptureOpener(
            [
                {"values": [{"name": "Development", "slug": "development", "uuid": "{env-dev}"}]},
                {"values": [{"key": "DEPLOY_ONLY", "secured": True, "value": None}]},
            ]
        )
        result = bb_ops.vars_list(
            _client(opener), "acme", "widget-service",
            scope="deployment", environment="Development",
        )
        assert result[0]["key"] == "DEPLOY_ONLY"
        # First call: env list. Second: the deployment variables collection
        # keyed by the URL-encoded env uuid.
        assert opener.calls[0]["url"].startswith(_env_list_url() + "?")
        assert opener.calls[1]["url"].startswith(_dep_vars_url("%7Benv-dev%7D") + "?")


class TestVarsSetWorkspaceScope:
    def test_create_posts_to_workspace_collection(self) -> None:
        opener = _CaptureOpener(
            [{"values": []}, {"key": "GLOBAL_VAR", "uuid": "{u}"}]
        )
        bb_ops.vars_set(
            _client(opener), "acme", None, "GLOBAL_VAR", "v", scope="workspace"
        )
        # Lookup GET then POST, both at the workspace (hyphen) collection.
        assert opener.calls[0]["url"].startswith(_ws_vars_url() + "?")
        assert opener.calls[1]["method"] == "POST"
        assert opener.calls[1]["url"] == _ws_vars_url()
        assert opener.calls[1]["body"] == {
            "key": "GLOBAL_VAR", "value": "v", "secured": False,
        }

    def test_update_puts_to_workspace_uuid(self) -> None:
        opener = _CaptureOpener(
            [
                {"values": [{"key": "GLOBAL_VAR", "uuid": "{ws-uuid}"}]},
                {"key": "GLOBAL_VAR", "uuid": "{ws-uuid}"},
            ]
        )
        bb_ops.vars_set(
            _client(opener), "acme", None, "GLOBAL_VAR", "v2", scope="workspace"
        )
        assert opener.calls[1]["method"] == "PUT"
        assert opener.calls[1]["url"] == _ws_vars_url() + "%7Bws-uuid%7D"


class TestVarsSetDeploymentScope:
    def test_create_resolves_env_then_posts(self) -> None:
        opener = _CaptureOpener(
            [
                {"values": [{"name": "Production", "slug": "production", "uuid": "{env-prod}"}]},
                {"values": []},
                {"key": "DEPLOY_VAR", "uuid": "{u}"},
            ]
        )
        bb_ops.vars_set(
            _client(opener), "acme", "widget-service", "DEPLOY_VAR", "v",
            scope="deployment", environment="Production", secured=True,
        )
        # env list, lookup GET, then POST to the deployment collection.
        assert opener.calls[0]["url"].startswith(_env_list_url() + "?")
        assert opener.calls[1]["url"].startswith(
            _dep_vars_url("%7Benv-prod%7D") + "?"
        )
        assert opener.calls[2]["method"] == "POST"
        assert opener.calls[2]["url"] == _dep_vars_url("%7Benv-prod%7D")
        assert opener.calls[2]["body"]["secured"] is True

    def test_unknown_env_raises_before_any_write(self) -> None:
        opener = _CaptureOpener(
            [{"values": [{"name": "Development", "slug": "development", "uuid": "{d}"}]}]
        )
        with pytest.raises(bb_ops.BBOpNotFound):
            bb_ops.vars_set(
                _client(opener), "acme", "widget-service", "K", "v",
                scope="deployment", environment="Nonexistent",
            )
        # Only the env-list GET happened; no variable lookup, no write.
        assert len(opener.calls) == 1
        assert opener.calls[0]["method"] == "GET"


class TestVarsDeleteWorkspaceScope:
    def test_deletes_at_workspace_hyphen_endpoint(self) -> None:
        opener = _CaptureOpener(
            [{"values": [{"key": "SHARED", "uuid": "{ws-uuid}"}]}, None]
        )
        result = bb_ops.vars_delete(
            _client(opener), "acme", None, "SHARED", scope="workspace"
        )
        assert result["scope"] == "workspace"
        # Lookup + DELETE both hit the workspace HYPHEN endpoint, no repo.
        assert opener.calls[0]["url"].startswith(_ws_vars_url() + "?")
        assert opener.calls[1]["method"] == "DELETE"
        assert opener.calls[1]["url"] == _ws_vars_url() + "%7Bws-uuid%7D"


class TestVarsDeleteDeploymentScope:
    def test_resolves_env_then_deletes(self) -> None:
        opener = _CaptureOpener(
            [
                {"values": [{"name": "Production", "slug": "production", "uuid": "{env-prod}"}]},
                {"values": [{"key": "DEPLOY_VAR", "uuid": "{v-uuid}"}]},
                None,  # DELETE
            ]
        )
        bb_ops.vars_delete(
            _client(opener), "acme", "widget-service", "DEPLOY_VAR",
            scope="deployment", environment="Production",
        )
        # env list (resolve name→uuid), var lookup, then DELETE to the
        # deployment collection's uuid path.
        assert opener.calls[0]["url"].startswith(_env_list_url() + "?")
        assert opener.calls[1]["url"].startswith(
            _dep_vars_url("%7Benv-prod%7D") + "?"
        )
        assert opener.calls[2]["method"] == "DELETE"
        assert opener.calls[2]["url"] == _dep_vars_url("%7Benv-prod%7D") + "%7Bv-uuid%7D"

    def test_unknown_env_raises_before_any_lookup_or_delete(self) -> None:
        opener = _CaptureOpener(
            [{"values": [{"name": "Development", "slug": "development", "uuid": "{d}"}]}]
        )
        with pytest.raises(bb_ops.BBOpNotFound):
            bb_ops.vars_delete(
                _client(opener), "acme", "widget-service", "K",
                scope="deployment", environment="Nonexistent",
            )
        # Only the env-list GET happened; no var lookup, no DELETE.
        assert len(opener.calls) == 1
        assert opener.calls[0]["method"] == "GET"


# ===========================================================================
# environments (list / create / delete)
# ===========================================================================


class TestEnvironmentsList:
    def test_lists_environments(self) -> None:
        opener = _CaptureOpener(
            [{"values": [
                {"name": "Production", "slug": "production", "uuid": "{p}",
                 "environment_type": {"name": "Production"}},
                {"name": "Test", "slug": "test", "uuid": "{t}",
                 "environment_type": {"name": "Test"}},
            ]}]
        )
        result = bb_ops.environments_list(_client(opener), "acme", "widget-service")
        assert [e["name"] for e in result] == ["Production", "Test"]
        call = opener.calls[0]
        # Trailing-slash collection path + pagelen.
        assert call["url"].startswith(_env_list_url() + "?")
        assert "pagelen=100" in call["url"]
        assert call["method"] == "GET"

    def test_count_caps_response(self) -> None:
        opener = _CaptureOpener(
            [{"values": [{"name": f"e{i}", "uuid": f"{{{i}}}"} for i in range(10)]}]
        )
        result = bb_ops.environments_list(
            _client(opener), "acme", "widget-service", count=3
        )
        assert len(result) == 3

    @pytest.mark.parametrize("bad", [0, -1, True, "ten", None])
    def test_rejects_non_positive_count(self, bad: Any) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="count"):
            bb_ops.environments_list(
                _client(opener), "acme", "widget-service", count=bad
            )
        assert opener.calls == []


class TestEnvironmentCreate:
    def test_posts_name_and_type(self) -> None:
        opener = _CaptureOpener(
            [{"name": "ci-smoke", "uuid": "{new}", "environment_type": {"name": "Test"}}]
        )
        result = bb_ops.environment_create(
            _client(opener), "acme", "widget-service", "ci-smoke"
        )
        assert result["uuid"] == "{new}"
        call = opener.calls[0]
        assert call["url"] == _env_list_url()
        assert call["method"] == "POST"
        # Default type is Test; body shape verified against the live API.
        assert call["body"] == {
            "name": "ci-smoke",
            "environment_type": {"name": "Test"},
        }

    @pytest.mark.parametrize(
        "given,canonical",
        [("staging", "Staging"), ("PRODUCTION", "Production"), ("Test", "Test")],
    )
    def test_type_canonicalised_case_insensitive(
        self, given: str, canonical: str
    ) -> None:
        opener = _CaptureOpener([{"name": "e", "uuid": "{u}"}])
        bb_ops.environment_create(
            _client(opener), "acme", "widget-service", "e",
            environment_type=given,
        )
        assert opener.calls[0]["body"]["environment_type"] == {"name": canonical}

    def test_name_stripped(self) -> None:
        opener = _CaptureOpener([{"name": "e", "uuid": "{u}"}])
        bb_ops.environment_create(
            _client(opener), "acme", "widget-service", "  prod  "
        )
        assert opener.calls[0]["body"]["name"] == "prod"

    @pytest.mark.parametrize("bad_name", ["", "   "])
    def test_rejects_empty_name_no_network(self, bad_name: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="name"):
            bb_ops.environment_create(
                _client(opener), "acme", "widget-service", bad_name
            )
        assert opener.calls == []

    @pytest.mark.parametrize("bad_type", ["Bogus", "prod", "", "   "])
    def test_rejects_bad_type_no_network(self, bad_type: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="environment_type"):
            bb_ops.environment_create(
                _client(opener), "acme", "widget-service", "e",
                environment_type=bad_type,
            )
        assert opener.calls == []

    @pytest.mark.parametrize("bad_slug", ["", "   ", "a/b", ".", ".."])
    def test_rejects_bad_slug_no_network(self, bad_slug: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError):
            bb_ops.environment_create(
                _client(opener), "acme", bad_slug, "e"
            )
        assert opener.calls == []


class TestEnvironmentDelete:
    def test_resolves_name_then_deletes(self) -> None:
        opener = _CaptureOpener(
            [
                {"values": [
                    {"name": "ci-smoke", "slug": "ci-smoke", "uuid": "{env-cs}"},
                ]},
                None,  # DELETE → 204 / no body
            ]
        )
        bb_ops.environment_delete(
            _client(opener), "acme", "widget-service", "ci-smoke"
        )
        # First: env list GET (resolve name → uuid). Second: DELETE to the
        # uuid path (braces URL-encoded, trailing slash).
        assert opener.calls[0]["url"].startswith(_env_list_url() + "?")
        assert opener.calls[0]["method"] == "GET"
        delete = opener.calls[1]
        assert delete["method"] == "DELETE"
        assert delete["url"] == _env_list_url() + "%7Benv-cs%7D/"

    def test_case_insensitive_name_match(self) -> None:
        opener = _CaptureOpener(
            [
                {"values": [{"name": "Production", "slug": "production", "uuid": "{p}"}]},
                None,
            ]
        )
        bb_ops.environment_delete(
            _client(opener), "acme", "widget-service", "production"
        )
        assert opener.calls[1]["url"] == _env_list_url() + "%7Bp%7D/"

    def test_unknown_env_raises_before_delete(self) -> None:
        opener = _CaptureOpener(
            [{"values": [{"name": "Production", "slug": "production", "uuid": "{p}"}]}]
        )
        with pytest.raises(bb_ops.BBOpNotFound):
            bb_ops.environment_delete(
                _client(opener), "acme", "widget-service", "no-such-env"
            )
        # Only the resolve GET fired; no DELETE.
        assert len(opener.calls) == 1
        assert opener.calls[0]["method"] == "GET"


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
