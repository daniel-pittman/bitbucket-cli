"""
Tests for bb_ops pipeline operations.

Discipline: every test asserts the request URL, method, and body shape
the function emits — not just the response value. A test that only checks
the return value would pass against a function that hits the wrong
endpoint or sends a malformed payload but happens to get a 200 back from
the mock. That's the "mock returns success regardless of request body"
anti-pattern called out in the testing methodology.

All fixtures are fictional: workspace `acme`, repo `widget-service`,
users `alice` / `bob`.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

import bb_api
import bb_ops
from bb_api import BBApiError, BBClient, BBConfig, DEFAULT_API_BASE


# ---------------------------------------------------------------------------
# Test scaffolding (same shape as test_bb_api.py's _CaptureOpener)
# ---------------------------------------------------------------------------


class _CaptureOpener:
    """Records each request and returns canned JSON. Reusing the same
    pattern as test_bb_api to keep cognitive overhead low across the
    suite."""

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
        # Three response shapes:
        #   dict | list -> JSON-encoded body
        #   None        -> empty body (204 No Content)
        #   bytes       -> raw body (for fetch_redirected_text tests)
        #   Exception   -> raised on open (for redirect / error tests)
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


def _pipelines_url() -> str:
    return DEFAULT_API_BASE + "/repositories/acme/widget-service/pipelines/"


def _make_pipeline(build_number: int, uuid: str = "p-uuid") -> dict[str, Any]:
    """Realistic-shape pipeline record. Matches what Bitbucket returns
    enough to exercise the parsing in bb_ops without being a fixture
    burden."""
    return {
        "build_number": build_number,
        "uuid": f"{{{uuid}}}",
        "state": {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}},
        "target": {"ref_name": "main"},
        "created_on": "2026-05-26T12:00:00Z",
        "duration_in_seconds": 42,
    }


def _make_step(name: str, uuid: str) -> dict[str, Any]:
    return {
        "uuid": f"{{{uuid}}}",
        "name": name,
        "state": {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}},
        "duration_in_seconds": 30,
    }


# ===========================================================================
# Internal helpers
# ===========================================================================


class TestWrapUuid:
    def test_strips_braces_and_url_encodes(self) -> None:
        # Bitbucket UUIDs come in `{uuid}` shape on some endpoints and bare
        # on others; _wrap_uuid normalises to the URL-encoded brace form.
        assert bb_ops._wrap_uuid("abc-123") == "%7Babc-123%7D"
        assert bb_ops._wrap_uuid("{abc-123}") == "%7Babc-123%7D"

    def test_handles_whitespace(self) -> None:
        assert bb_ops._wrap_uuid("  abc-123  ") == "%7Babc-123%7D"


class TestStripUuidBraces:
    def test_bare_uuid(self) -> None:
        assert bb_ops._strip_uuid_braces("abc-123") == "abc-123"

    def test_braced_uuid(self) -> None:
        assert bb_ops._strip_uuid_braces("{abc-123}") == "abc-123"

    def test_empty_raises(self) -> None:
        # The bash equivalent silently produces "" and then misroutes the
        # next URL — we make it loud instead.
        with pytest.raises(BBApiError, match="missing uuid"):
            bb_ops._strip_uuid_braces("")
        with pytest.raises(BBApiError, match="missing uuid"):
            bb_ops._strip_uuid_braces(None)


# ===========================================================================
# pipelines_list
# ===========================================================================


class TestPipelinesList:
    def test_single_page_default_count(self) -> None:
        opener = _CaptureOpener(
            [
                {
                    "values": [_make_pipeline(n) for n in range(10, 0, -1)],
                    # No `next` -> single page.
                },
            ]
        )
        result = bb_ops.pipelines_list(_client(opener), "acme", "widget-service")
        assert len(result) == 10
        assert [p["build_number"] for p in result] == list(range(10, 0, -1))
        # Assert the EXACT request shape.
        call = opener.calls[0]
        assert call["method"] == "GET"
        assert call["url"].startswith(_pipelines_url() + "?")
        assert "sort=-created_on" in call["url"]
        # Default count=10 → pagelen=10 (matches bash exactly).
        assert "pagelen=10" in call["url"]

    def test_count_capped_at_bitbucket_max(self) -> None:
        # Bitbucket's pagelen cap is 100. Caller wants 250 → pagelen=100
        # per page, two pages walked.
        opener = _CaptureOpener(
            [
                {
                    "values": [_make_pipeline(n) for n in range(250, 150, -1)],
                    "next": _pipelines_url() + "?page=2",
                },
                {
                    "values": [_make_pipeline(n) for n in range(150, 0, -1)],
                },
            ]
        )
        result = bb_ops.pipelines_list(
            _client(opener), "acme", "widget-service", count=250
        )
        assert len(result) == 250
        # First request used pagelen=100 (the cap), NOT 250.
        assert "pagelen=100" in opener.calls[0]["url"]

    def test_stops_at_count_mid_page(self) -> None:
        # Caller asks for 7 — we honour exactly 7 even though the first
        # page returned 10.
        opener = _CaptureOpener(
            [{"values": [_make_pipeline(n) for n in range(10, 0, -1)]}]
        )
        result = bb_ops.pipelines_list(
            _client(opener), "acme", "widget-service", count=7
        )
        assert len(result) == 7

    def test_branch_filter(self) -> None:
        opener = _CaptureOpener([{"values": []}])
        bb_ops.pipelines_list(
            _client(opener), "acme", "widget-service", branch="feat/widget"
        )
        url = opener.calls[0]["url"]
        # Bitbucket's contract for filtering by branch is `target.ref_name=...`.
        assert "target.ref_name=feat%2Fwidget" in url

    def test_rejects_non_positive_count(self) -> None:
        opener = _CaptureOpener([])
        for bad in (0, -1, "ten"):
            with pytest.raises(ValueError, match="count"):
                bb_ops.pipelines_list(
                    _client(opener), "acme", "widget-service", count=bad  # type: ignore[arg-type]
                )
        assert opener.calls == []  # no request emitted for any bad input


# ===========================================================================
# _resolve_pipeline_uuid (covered indirectly via pipeline_show, but worth
# direct tests for the not-found and pagination-walk paths)
# ===========================================================================


class TestResolvePipelineUuid:
    def test_finds_on_first_page(self) -> None:
        opener = _CaptureOpener(
            [
                {
                    "values": [
                        _make_pipeline(42, uuid="target-uuid"),
                        _make_pipeline(41),
                    ]
                }
            ]
        )
        uuid = bb_ops._resolve_pipeline_uuid(
            _client(opener), "acme", "widget-service", 42
        )
        assert uuid == "target-uuid"

    def test_walks_pages_to_find(self) -> None:
        opener = _CaptureOpener(
            [
                {
                    "values": [_make_pipeline(n) for n in range(100, 50, -1)],
                    "next": _pipelines_url() + "?page=2",
                },
                {
                    "values": [_make_pipeline(n, uuid=f"u-{n}") for n in range(50, 0, -1)],
                },
            ]
        )
        uuid = bb_ops._resolve_pipeline_uuid(
            _client(opener), "acme", "widget-service", 5
        )
        assert uuid == "u-5"
        assert len(opener.calls) == 2

    def test_not_found_raises_bbopnotfound(self) -> None:
        opener = _CaptureOpener([{"values": [_make_pipeline(n) for n in range(10, 0, -1)]}])
        with pytest.raises(bb_ops.BBOpNotFound, match="#999"):
            bb_ops._resolve_pipeline_uuid(
                _client(opener), "acme", "widget-service", 999
            )

    def test_rejects_invalid_build_number(self) -> None:
        opener = _CaptureOpener([])
        for bad in (0, -1, "42", None):
            with pytest.raises(ValueError, match="build_number"):
                bb_ops._resolve_pipeline_uuid(
                    _client(opener), "acme", "widget-service", bad  # type: ignore[arg-type]
                )
        assert opener.calls == []

    def test_scan_limit_caps_search(self) -> None:
        # The walker should stop after scan_limit items even if more pages
        # are available. Defend against an unbounded search.
        opener = _CaptureOpener(
            [
                {
                    "values": [_make_pipeline(n) for n in range(100, 0, -1)],
                    # Server says there's more — we should NOT request page 2
                    # if scan_limit < 100 has already been hit.
                    "next": _pipelines_url() + "?page=2",
                },
            ]
        )
        with pytest.raises(bb_ops.BBOpNotFound):
            bb_ops._resolve_pipeline_uuid(
                _client(opener), "acme", "widget-service", 999, scan_limit=50
            )
        # Only the first page should have been fetched (scan_limit=50 hit
        # before exhausting page 1).
        assert len(opener.calls) == 1


# ===========================================================================
# pipeline_show
# ===========================================================================


class TestPipelineShow:
    def test_fetches_by_uuid_after_lookup(self) -> None:
        # 1) list-walk to find build 42's uuid
        # 2) GET the pipeline by uuid
        target_uuid = "abc-123-def"
        opener = _CaptureOpener(
            [
                {"values": [_make_pipeline(42, uuid=target_uuid)]},
                _make_pipeline(42, uuid=target_uuid),  # the show response
            ]
        )
        result = bb_ops.pipeline_show(_client(opener), "acme", "widget-service", 42)
        assert result["build_number"] == 42

        # Two requests: list, then individual show.
        assert len(opener.calls) == 2
        # Show URL uses %7B...%7D bracketed UUID — the bash contract.
        assert (
            opener.calls[1]["url"]
            == _pipelines_url() + "%7Babc-123-def%7D"
        )
        assert opener.calls[1]["method"] == "GET"


# ===========================================================================
# pipeline_steps
# ===========================================================================


class TestPipelineSteps:
    def test_lists_steps_for_build(self) -> None:
        opener = _CaptureOpener(
            [
                {"values": [_make_pipeline(7, uuid="pipe-uuid")]},  # uuid lookup
                {"values": [_make_step("build", "s1"), _make_step("deploy", "s2")]},
            ]
        )
        result = bb_ops.pipeline_steps(_client(opener), "acme", "widget-service", 7)
        assert [s["name"] for s in result] == ["build", "deploy"]
        # Step list URL hits /pipelines/{uuid}/steps/
        assert "/pipelines/%7Bpipe-uuid%7D/steps/" in opener.calls[1]["url"]


# ===========================================================================
# pipeline_trigger
# ===========================================================================


class TestPipelineTrigger:
    def test_default_pipeline_payload_shape(self) -> None:
        opener = _CaptureOpener([_make_pipeline(99)])
        bb_ops.pipeline_trigger(
            _client(opener), "acme", "widget-service", branch="feat/widget"
        )
        call = opener.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == _pipelines_url()
        # Default pipeline: no `selector`, no `variables` key.
        assert call["body"] == {
            "target": {"ref_name": "feat/widget", "ref_type": "branch"}
        }

    def test_custom_pipeline_payload_shape(self) -> None:
        opener = _CaptureOpener([_make_pipeline(100)])
        bb_ops.pipeline_trigger(
            _client(opener),
            "acme",
            "widget-service",
            branch="main",
            pattern="deploy-prod",
        )
        assert opener.calls[0]["body"] == {
            "target": {
                "ref_name": "main",
                "ref_type": "branch",
                "selector": {"type": "custom", "pattern": "deploy-prod"},
            }
        }

    def test_variables_dict_payload_shape(self) -> None:
        opener = _CaptureOpener([_make_pipeline(101)])
        bb_ops.pipeline_trigger(
            _client(opener),
            "acme",
            "widget-service",
            branch="main",
            variables={"REGION": "us-west-2", "DEPLOY_TAG": "v2.3"},
        )
        body = opener.calls[0]["body"]
        # Bitbucket's contract: list of {"key", "value"} objects.
        assert sorted(body["variables"], key=lambda v: v["key"]) == [
            {"key": "DEPLOY_TAG", "value": "v2.3"},
            {"key": "REGION", "value": "us-west-2"},
        ]

    def test_variables_iterable_of_pairs(self) -> None:
        opener = _CaptureOpener([_make_pipeline(102)])
        bb_ops.pipeline_trigger(
            _client(opener),
            "acme",
            "widget-service",
            branch="main",
            variables=[("A", "1"), ("B", "2")],
        )
        assert opener.calls[0]["body"]["variables"] == [
            {"key": "A", "value": "1"},
            {"key": "B", "value": "2"},
        ]

    def test_empty_variables_omitted_from_payload(self) -> None:
        opener = _CaptureOpener([_make_pipeline(103)])
        bb_ops.pipeline_trigger(
            _client(opener), "acme", "widget-service", branch="main", variables={}
        )
        # Empty variables → no `variables` key in the request body, NOT an
        # empty list. Matches Bitbucket's "absence is default" contract.
        assert "variables" not in opener.calls[0]["body"]

    def test_rejects_non_string_variable_value(self) -> None:
        # Bitbucket only accepts string values; defend at the boundary.
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="variable value"):
            bb_ops.pipeline_trigger(
                _client(opener),
                "acme",
                "widget-service",
                branch="main",
                variables={"COUNT": 42},  # type: ignore[dict-item]
            )
        assert opener.calls == []

    def test_rejects_empty_pattern(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="pattern"):
            bb_ops.pipeline_trigger(
                _client(opener), "acme", "widget-service", branch="main", pattern=""
            )
        assert opener.calls == []

    def test_rejects_empty_branch(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="branch"):
            bb_ops.pipeline_trigger(
                _client(opener), "acme", "widget-service", branch=""
            )
        assert opener.calls == []


# ===========================================================================
# pipeline_stop
# ===========================================================================


class TestPipelineStop:
    def test_posts_to_stop_endpoint(self) -> None:
        # Two responses: the uuid-lookup list, then the stop POST (returns
        # None to simulate Bitbucket's 204 No Content).
        opener = _CaptureOpener(
            [
                {"values": [_make_pipeline(7, uuid="stoppable")]},
                None,
            ]
        )
        result = bb_ops.pipeline_stop(_client(opener), "acme", "widget-service", 7)
        assert result is None  # 204
        # The stop call hits /pipelines/{uuid}/stopPipeline and is a POST
        # with no body.
        assert opener.calls[1]["method"] == "POST"
        assert opener.calls[1]["url"].endswith(
            "/pipelines/%7Bstoppable%7D/stopPipeline"
        )
        assert opener.calls[1]["body"] is None


# ===========================================================================
# pipeline_logs (the redirect-with-auth-strip path)
# ===========================================================================


class TestPipelineLogs:
    def test_inline_log_body_returned(self) -> None:
        # Three requests:
        #  1. pipelines list (find build_number's uuid)
        #  2. steps list (find step_index's uuid)
        #  3. log fetch — server returns the log body inline (200), no redirect
        opener = _CaptureOpener(
            [
                {"values": [_make_pipeline(42, uuid="pipe-uuid")]},
                {"values": [_make_step("build", "step-uuid")]},
                b"+ echo hello\nhello\n",  # raw log body
            ]
        )
        result = bb_ops.pipeline_logs(
            _client(opener), "acme", "widget-service", 42, 0
        )
        assert result == "+ echo hello\nhello\n"
        log_call = opener.calls[2]
        assert log_call["method"] == "GET"
        assert log_call["url"].endswith(
            "/pipelines/%7Bpipe-uuid%7D/steps/%7Bstep-uuid%7D/log"
        )
        # The log call carries Authorization (no redirect happened).
        assert "Authorization" in log_call["headers"]

    def test_follows_s3_redirect_and_strips_auth(self) -> None:
        # Models the real Bitbucket behaviour: log endpoint returns 307 to
        # a signed S3 URL. We follow, and the second call MUST NOT carry
        # Authorization (S3 would reject it, and we don't want our
        # Bitbucket Basic credential going anywhere else).
        s3_url = (
            "https://bbci-pipeline-logs.s3.amazonaws.com/"
            "acme/widget-service/42/build.log?X-Amz-Signature=abc"
        )
        redirect_response = urllib.error.HTTPError(
            url=DEFAULT_API_BASE
            + "/repositories/acme/widget-service/pipelines/%7Bp%7D/steps/%7Bs%7D/log",
            code=307,
            msg="Temporary Redirect",
            hdrs={"Location": s3_url},  # type: ignore[arg-type]
            fp=io.BytesIO(b""),
        )

        opener = _CaptureOpener(
            [
                {"values": [_make_pipeline(42, uuid="p")]},  # build_number lookup
                {"values": [_make_step("build", "s")]},      # step index lookup
                redirect_response,                            # log fetch → 307
                b"log content from s3\n",                     # follow-up to S3
            ]
        )
        result = bb_ops.pipeline_logs(
            _client(opener), "acme", "widget-service", 42, 0
        )
        assert result == "log content from s3\n"
        # Four total requests; the 4th was the S3 follow-up.
        assert len(opener.calls) == 4
        s3_call = opener.calls[3]
        assert s3_call["url"] == s3_url
        # CRITICAL: Authorization must NOT have been sent to S3.
        assert "Authorization" not in s3_call["headers"], (
            "Bitbucket Basic auth was sent to the S3 host — this is the "
            "credential-leak the cross-host strip is meant to prevent."
        )

    def test_too_many_redirects_raises(self) -> None:
        def _redirect_to(target: str) -> urllib.error.HTTPError:
            return urllib.error.HTTPError(
                url="https://x",
                code=307,
                msg="Temporary Redirect",
                hdrs={"Location": target},  # type: ignore[arg-type]
                fp=io.BytesIO(b""),
            )

        # Three responses for the uuid lookups + a chain of redirects that
        # exceeds the default max_redirects (5).
        opener = _CaptureOpener(
            [
                {"values": [_make_pipeline(1, uuid="p")]},
                {"values": [_make_step("build", "s")]},
                _redirect_to("https://api.bitbucket.org/2.0/hop/1"),
                _redirect_to("https://api.bitbucket.org/2.0/hop/2"),
                _redirect_to("https://api.bitbucket.org/2.0/hop/3"),
                _redirect_to("https://api.bitbucket.org/2.0/hop/4"),
                _redirect_to("https://api.bitbucket.org/2.0/hop/5"),
                _redirect_to("https://api.bitbucket.org/2.0/hop/6"),  # >5 hops
            ]
        )
        with pytest.raises(BBApiError, match="redirect chain exceeded"):
            bb_ops.pipeline_logs(_client(opener), "acme", "widget-service", 1, 0)

    def test_redirect_without_location_raises(self) -> None:
        broken_redirect = urllib.error.HTTPError(
            url="https://x",
            code=302,
            msg="Found",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(b""),
        )
        opener = _CaptureOpener(
            [
                {"values": [_make_pipeline(1, uuid="p")]},
                {"values": [_make_step("build", "s")]},
                broken_redirect,
            ]
        )
        with pytest.raises(BBApiError, match="missing Location"):
            bb_ops.pipeline_logs(_client(opener), "acme", "widget-service", 1, 0)


# ===========================================================================
# _resolve_step_uuid
# ===========================================================================


class TestResolveStepUuid:
    def test_valid_index(self) -> None:
        opener = _CaptureOpener(
            [{"values": [_make_step("build", "s1"), _make_step("test", "s2")]}]
        )
        uuid, name = bb_ops._resolve_step_uuid(
            _client(opener), "acme", "widget-service", "pipe-uuid", 1
        )
        assert uuid == "s2"
        assert name == "test"

    def test_index_out_of_range_raises(self) -> None:
        opener = _CaptureOpener(
            [{"values": [_make_step("build", "s1")]}]
        )
        with pytest.raises(bb_ops.BBOpNotFound, match="out of range"):
            bb_ops._resolve_step_uuid(
                _client(opener), "acme", "widget-service", "pipe-uuid", 5
            )

    def test_rejects_negative_index(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="step_index"):
            bb_ops._resolve_step_uuid(
                _client(opener), "acme", "widget-service", "pipe-uuid", -1
            )
        assert opener.calls == []
