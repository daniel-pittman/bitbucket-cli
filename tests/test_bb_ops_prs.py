"""
Tests for bb_ops pull-request operations.

Discipline: every HTTP-touching test asserts URL, method, AND body shape.
All fixtures are fictional (acme / widget-service / alice / bob).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

import bb_ops
from bb_api import BBClient, BBConfig, DEFAULT_API_BASE


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _CaptureOpener:
    """Records each request and returns canned JSON. Same shape as the
    helper in test_bb_ops_pipelines, intentionally duplicated rather than
    extracted to a shared fixture module: each test file should be
    readable end-to-end without jumping to another file."""

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


def _prs_url() -> str:
    return DEFAULT_API_BASE + "/repositories/acme/widget-service/pullrequests"


def _make_pr(id_: int, state: str = "OPEN") -> dict[str, Any]:
    return {
        "id": id_,
        "title": f"PR {id_}",
        "state": state,
        "author": {"display_name": "Alice"},
        "source": {"branch": {"name": "feat/widget"}},
        "destination": {"branch": {"name": "main"}},
        "created_on": "2026-05-26T12:00:00Z",
        "updated_on": "2026-05-26T13:00:00Z",
        "links": {"html": {"href": f"https://bitbucket.org/acme/widget-service/pull-requests/{id_}"}},
    }


# ===========================================================================
# prs_list
# ===========================================================================


class TestPrsList:
    def test_default_state_open_count_25(self) -> None:
        opener = _CaptureOpener([{"values": [_make_pr(i) for i in range(1, 26)]}])
        result = bb_ops.prs_list(_client(opener), "acme", "widget-service")
        assert len(result) == 25
        url = opener.calls[0]["url"]
        assert url.startswith(_prs_url() + "?")
        assert "state=OPEN" in url
        assert "pagelen=25" in url

    def test_state_filter(self) -> None:
        opener = _CaptureOpener([{"values": []}])
        bb_ops.prs_list(_client(opener), "acme", "widget-service", state="MERGED")
        assert "state=MERGED" in opener.calls[0]["url"]

    def test_count_walks_pages(self) -> None:
        # Caller wants 150 PRs; Bitbucket caps pagelen at 100. Walk two pages.
        opener = _CaptureOpener(
            [
                {
                    "values": [_make_pr(i) for i in range(1, 101)],
                    "next": _prs_url() + "?page=2",
                },
                {"values": [_make_pr(i) for i in range(101, 201)]},
            ]
        )
        result = bb_ops.prs_list(
            _client(opener), "acme", "widget-service", count=150
        )
        assert len(result) == 150
        assert "pagelen=100" in opener.calls[0]["url"]

    def test_rejects_non_positive_count(self) -> None:
        opener = _CaptureOpener([])
        # True/False included: bool is a subclass of int but its URL
        # stringification is "True"/"False" — symmetric with the bool
        # rejection in TestPrActivity / TestPrCommentsList / pr_id checks.
        for bad in (0, -1, True, False, "ten"):
            with pytest.raises(ValueError, match="count"):
                bb_ops.prs_list(
                    _client(opener),
                    "acme",
                    "widget-service",
                    count=bad,  # type: ignore[arg-type]
                )
        assert opener.calls == []  # no request emitted for bad input

    def test_rejects_empty_state(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="state"):
            bb_ops.prs_list(_client(opener), "acme", "widget-service", state="")
        assert opener.calls == []

    @pytest.mark.parametrize("bad_state", ["OPENED", "open", "OPEN,MERGED", "INVALID"])
    def test_rejects_unknown_state(self, bad_state: str) -> None:
        """A typo like OPENED, a case bug like 'open', or the
        comma-separated compound form (which Bitbucket does NOT accept
        on the simple ?state= filter) all need to fail at the boundary
        — otherwise the API call burns a quota slot returning empty
        results or a 400. _KNOWN_PR_STATES is the symmetric guard to
        pr_merge's strategy validation."""
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="state must be one of"):
            bb_ops.prs_list(
                _client(opener), "acme", "widget-service", state=bad_state
            )
        assert opener.calls == []


def _make_rich_pr(id_: int) -> dict[str, Any]:
    """A PR object carrying the bulky fields Bitbucket actually returns
    (rendered HTML description + summary variants + participant array +
    full reviewer account blobs) — the payload shape that pushes the
    MCP response past the 25k-token cap on real repos."""
    pr = _make_pr(id_)
    pr["description"] = "x" * 5000  # raw markdown body
    pr["summary"] = {
        "raw": "y" * 3000,
        "markup": "markdown",
        "html": "<p>" + ("z" * 3000) + "</p>",
    }
    pr["rendered"] = {"title": {"html": "..."}, "description": {"html": "w" * 4000}}
    pr["participants"] = [
        {"user": {"display_name": f"User {n}", "uuid": f"{{u{n}}}"}, "role": "PARTICIPANT"}
        for n in range(20)
    ]
    pr["reviewers"] = [
        {
            "display_name": f"Reviewer {n}",
            "uuid": f"{{r{n}}}",
            "account_id": f"acct-{n}",
            "nickname": f"rev{n}",
            "type": "user",
            "links": {"avatar": {"href": "https://example.com/a.png"}},
        }
        for n in range(3)
    ]
    return pr


class TestPrsListSlimProjection:
    """#27 — prs_list slims each PR by default so the list view fits the
    MCP 25k-token response cap on rich-PR repos. pr_show stays full."""

    def test_default_strips_bulky_fields(self) -> None:
        opener = _CaptureOpener([{"values": [_make_rich_pr(1)]}])
        result = bb_ops.prs_list(_client(opener), "acme", "widget-service")
        pr = result[0]
        # Bulky fields gone by default.
        for field in ("description", "summary", "rendered", "participants"):
            assert field not in pr, f"{field} should be stripped in slim mode"
        # Identity / triage fields preserved.
        assert pr["id"] == 1
        assert pr["title"] == "PR 1"
        assert pr["state"] == "OPEN"
        assert pr["source"]["branch"]["name"] == "feat/widget"
        assert pr["links"]["html"]["href"].endswith("/pull-requests/1")

    def test_reviewers_projected_to_uuid_and_name(self) -> None:
        opener = _CaptureOpener([{"values": [_make_rich_pr(1)]}])
        result = bb_ops.prs_list(_client(opener), "acme", "widget-service")
        reviewers = result[0]["reviewers"]
        assert len(reviewers) == 3
        # Only uuid + display_name survive — the account_id / nickname /
        # links blobs (the bulk) are dropped.
        for r in reviewers:
            assert set(r.keys()) == {"uuid", "display_name"}
        assert reviewers[0]["display_name"] == "Reviewer 0"
        assert reviewers[0]["uuid"] == "{r0}"

    def test_verbose_preserves_full_objects(self) -> None:
        opener = _CaptureOpener([{"values": [_make_rich_pr(1)]}])
        result = bb_ops.prs_list(
            _client(opener), "acme", "widget-service", verbose=True
        )
        pr = result[0]
        # Everything intact in verbose mode.
        assert len(pr["description"]) == 5000
        assert "html" in pr["summary"]
        assert "rendered" in pr
        assert len(pr["participants"]) == 20
        # Reviewers keep their full account blobs (not projected).
        assert "account_id" in pr["reviewers"][0]

    def test_slim_meaningfully_smaller(self) -> None:
        """Concrete size guard: the slim projection must cut the
        serialized payload by an order of magnitude on a rich PR, or the
        25k-cap fix isn't actually buying anything."""
        import json
        opener = _CaptureOpener(
            [{"values": [_make_rich_pr(i) for i in range(1, 4)]}]
        )
        slim = bb_ops.prs_list(_client(opener), "acme", "widget-service")
        opener2 = _CaptureOpener(
            [{"values": [_make_rich_pr(i) for i in range(1, 4)]}]
        )
        full = bb_ops.prs_list(
            _client(opener2), "acme", "widget-service", verbose=True
        )
        slim_bytes = len(json.dumps(slim))
        full_bytes = len(json.dumps(full))
        # 3 rich PRs full = tens of KB; slim should be a small fraction.
        assert slim_bytes * 5 < full_bytes, (
            f"slim ({slim_bytes}B) not meaningfully smaller than full "
            f"({full_bytes}B)"
        )

    def test_source_dict_not_mutated(self) -> None:
        """Slimming must not mutate the original API response dict —
        a verbose caller iterating the same objects later must still
        see the full fields."""
        original = _make_rich_pr(1)
        opener = _CaptureOpener([{"values": [original]}])
        bb_ops.prs_list(_client(opener), "acme", "widget-service")
        # The fixture dict the opener returned still has its bulky fields.
        assert "description" in original
        assert "participants" in original


# ===========================================================================
# pr_show + pr_activity
# ===========================================================================


class TestPrShow:
    def test_get_url_and_id_validation(self) -> None:
        opener = _CaptureOpener([_make_pr(42)])
        result = bb_ops.pr_show(_client(opener), "acme", "widget-service", 42)
        assert result["id"] == 42
        assert opener.calls[0]["url"] == _prs_url() + "/42"
        assert opener.calls[0]["method"] == "GET"

    def test_rejects_invalid_pr_id(self) -> None:
        opener = _CaptureOpener([])
        # True/False included explicitly: bool is a subclass of int in
        # Python, so a naive isinstance(x, int) check would accept them
        # and stringify them in URLs as "True"/"False" — the regression
        # this validator now defends against.
        for bad in (0, -5, "42", 1.5, None, True, False):
            with pytest.raises(ValueError, match="pr_id"):
                bb_ops.pr_show(
                    _client(opener), "acme", "widget-service", bad  # type: ignore[arg-type]
                )
        assert opener.calls == []


class TestPrActivity:
    def test_walks_activity_endpoint(self) -> None:
        opener = _CaptureOpener(
            [
                {
                    "values": [{"approval": {"user": {"display_name": "Bob"}}}],
                }
            ]
        )
        result = bb_ops.pr_activity(_client(opener), "acme", "widget-service", 42)
        assert len(result) == 1
        url = opener.calls[0]["url"]
        assert url.startswith(_prs_url() + "/42/activity?")
        assert "pagelen=50" in url

    def test_count_walks_pages(self) -> None:
        # Same paginate-with-count semantics as prs_list; verify the
        # behaviour symmetrically.
        opener = _CaptureOpener(
            [
                {
                    "values": [{"i": i} for i in range(100)],
                    "next": _prs_url() + "/42/activity?page=2",
                },
                {"values": [{"i": i} for i in range(100, 150)]},
            ]
        )
        result = bb_ops.pr_activity(
            _client(opener), "acme", "widget-service", 42, count=150
        )
        assert len(result) == 150
        assert "pagelen=100" in opener.calls[0]["url"]

    def test_rejects_non_positive_count(self) -> None:
        opener = _CaptureOpener([])
        for bad in (0, -1, True, False, "ten"):
            with pytest.raises(ValueError, match="count"):
                bb_ops.pr_activity(
                    _client(opener),
                    "acme",
                    "widget-service",
                    42,
                    count=bad,  # type: ignore[arg-type]
                )
        assert opener.calls == []

    def test_rejects_invalid_pr_id(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="pr_id"):
            bb_ops.pr_activity(_client(opener), "acme", "widget-service", 0)
        assert opener.calls == []


# ===========================================================================
# pr_create
# ===========================================================================


class TestPrCreate:
    def test_minimal_payload(self) -> None:
        opener = _CaptureOpener([_make_pr(7)])
        bb_ops.pr_create(
            _client(opener),
            "acme",
            "widget-service",
            title="Add widget",
            source_branch="feat/widget",
        )
        call = opener.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == _prs_url()
        assert call["body"] == {
            "title": "Add widget",
            "source": {"branch": {"name": "feat/widget"}},
            "destination": {"branch": {"name": "main"}},
            "close_source_branch": True,
        }

    def test_with_description_and_destination(self) -> None:
        opener = _CaptureOpener([_make_pr(8)])
        bb_ops.pr_create(
            _client(opener),
            "acme",
            "widget-service",
            title="Add widget",
            source_branch="feat/widget",
            destination_branch="develop",
            description="Adds the widget service.",
        )
        body = opener.calls[0]["body"]
        assert body["destination"] == {"branch": {"name": "develop"}}
        assert body["description"] == "Adds the widget service."

    def test_close_source_branch_override(self) -> None:
        opener = _CaptureOpener([_make_pr(9)])
        bb_ops.pr_create(
            _client(opener),
            "acme",
            "widget-service",
            title="Add widget",
            source_branch="feat/widget",
            close_source_branch=False,
        )
        assert opener.calls[0]["body"]["close_source_branch"] is False

    def test_reviewers_payload_shape(self) -> None:
        opener = _CaptureOpener([_make_pr(10)])
        bb_ops.pr_create(
            _client(opener),
            "acme",
            "widget-service",
            title="Add widget",
            source_branch="feat/widget",
            reviewers=["{alice-uuid}", "{bob-uuid}"],
        )
        body = opener.calls[0]["body"]
        # Bitbucket's contract: list of {"uuid": "..."} objects.
        assert body["reviewers"] == [
            {"uuid": "{alice-uuid}"},
            {"uuid": "{bob-uuid}"},
        ]

    def test_empty_reviewers_omitted(self) -> None:
        opener = _CaptureOpener([_make_pr(11)])
        bb_ops.pr_create(
            _client(opener),
            "acme",
            "widget-service",
            title="Add widget",
            source_branch="feat/widget",
            reviewers=[],
        )
        # Empty list → no `reviewers` key (matches the empty-variables
        # contract in pipeline_trigger).
        assert "reviewers" not in opener.calls[0]["body"]

    def test_empty_description_omitted(self) -> None:
        opener = _CaptureOpener([_make_pr(12)])
        bb_ops.pr_create(
            _client(opener),
            "acme",
            "widget-service",
            title="t",
            source_branch="s",
        )
        # Empty description → no `description` key. Bash includes it
        # always (even empty); Python omits — flagged as 4.7 alignment.
        assert "description" not in opener.calls[0]["body"]

    @pytest.mark.parametrize(
        "field,value",
        [
            # Empty string AND whitespace-only must both reject for every
            # required string field. Without .strip(), whitespace-only
            # values create degenerate PRs with visually-blank fields
            # in any list view.
            ("title", ""),
            ("title", "   "),
            ("title", "\n\t"),
            ("source_branch", ""),
            ("source_branch", "   "),
            ("destination_branch", ""),
            ("destination_branch", "\t"),
        ],
    )
    def test_rejects_empty_required_fields(self, field: str, value: str) -> None:
        opener = _CaptureOpener([])
        kwargs = {
            "title": "t",
            "source_branch": "s",
            "destination_branch": "main",
        }
        kwargs[field] = value
        with pytest.raises(ValueError, match=field):
            bb_ops.pr_create(_client(opener), "acme", "widget-service", **kwargs)
        assert opener.calls == []

    @pytest.mark.parametrize(
        "bad_reviewer",
        [
            "",                     # empty string
            None,                   # non-string sentinel
            123,                    # non-string scalar
            {"uuid": "alice-uuid"}, # the most plausible caller mistake — pre-shaping the payload
        ],
    )
    def test_rejects_invalid_reviewer(self, bad_reviewer: Any) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="reviewer"):
            bb_ops.pr_create(
                _client(opener),
                "acme",
                "widget-service",
                title="t",
                source_branch="s",
                reviewers=[bad_reviewer],
            )
        assert opener.calls == []

    def test_rejects_bare_string_reviewers(self) -> None:
        """A bare string is technically an Iterable[str] (yields chars).
        Without the early-reject, `reviewers="alice-uuid"` would silently
        produce `[{"uuid":"a"}, {"uuid":"l"}, {"uuid":"i"}, ...]`."""
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="reviewers must be a list"):
            bb_ops.pr_create(
                _client(opener),
                "acme",
                "widget-service",
                title="t",
                source_branch="s",
                reviewers="alice-uuid",  # type: ignore[arg-type]
            )
        assert opener.calls == []

    def test_rejects_non_string_description(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="description"):
            bb_ops.pr_create(
                _client(opener),
                "acme",
                "widget-service",
                title="t",
                source_branch="s",
                description={"foo": "bar"},  # type: ignore[arg-type]
            )
        assert opener.calls == []

    def test_rejects_non_bool_close_source_branch(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="close_source_branch"):
            bb_ops.pr_create(
                _client(opener),
                "acme",
                "widget-service",
                title="t",
                source_branch="s",
                close_source_branch="yes",  # type: ignore[arg-type]
            )
        assert opener.calls == []

    def test_whitespace_only_description_omitted(self) -> None:
        opener = _CaptureOpener([_make_pr(13)])
        bb_ops.pr_create(
            _client(opener),
            "acme",
            "widget-service",
            title="t",
            source_branch="s",
            description="   \n\t  ",
        )
        # Whitespace-only descriptions don't carry information; omit
        # rather than ship a meaningless empty-ish field.
        assert "description" not in opener.calls[0]["body"]


# ===========================================================================
# pr_approve / pr_unapprove
# ===========================================================================


class TestPrApprove:
    def test_post_to_approve_endpoint(self) -> None:
        opener = _CaptureOpener([{"approved": True, "user": {"display_name": "Alice"}}])
        result = bb_ops.pr_approve(_client(opener), "acme", "widget-service", 7)
        assert result["approved"] is True
        # The MCP layer now sees the response — bash discards it with > /dev/null.
        call = opener.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == _prs_url() + "/7/approve"
        assert call["body"] is None


class TestPrUnapprove:
    def test_delete_to_approve_endpoint(self) -> None:
        # Bitbucket contract: DELETE the same /approve subpath that POST
        # uses for approval. Not exposed by bash today (4.7 parity gap).
        opener = _CaptureOpener([None])  # 204 No Content
        result = bb_ops.pr_unapprove(_client(opener), "acme", "widget-service", 7)
        assert result is None
        call = opener.calls[0]
        assert call["method"] == "DELETE"
        assert call["url"] == _prs_url() + "/7/approve"


# ===========================================================================
# pr_merge
# ===========================================================================


class TestPrMerge:
    def test_default_strategy_payload(self) -> None:
        opener = _CaptureOpener([_make_pr(7, state="MERGED")])
        bb_ops.pr_merge(_client(opener), "acme", "widget-service", 7)
        call = opener.calls[0]
        # Bitbucket's PR merge endpoint is POST per the REST docs. An
        # earlier version used PUT and this test pinned that; PUT now
        # 403s with "endpoint does not support token-based authentication"
        # (a misleading error that actually meant "wrong method"). See
        # bb_ops.pr_merge for the full history. Pin POST going forward.
        assert call["method"] == "POST"
        assert call["url"] == _prs_url() + "/7/merge"
        assert call["body"] == {
            "type": "pullrequest",
            "merge_strategy": "merge_commit",
            "close_source_branch": True,
        }

    def test_each_strategy(self) -> None:
        for strategy in ("merge_commit", "squash", "fast_forward"):
            opener = _CaptureOpener([_make_pr(7, state="MERGED")])
            bb_ops.pr_merge(
                _client(opener), "acme", "widget-service", 7, strategy=strategy
            )
            assert opener.calls[0]["body"]["merge_strategy"] == strategy

    @pytest.mark.parametrize(
        "bad_strategy",
        [
            "rebase",       # not in Bitbucket's set
            "",             # empty string
            None,           # non-string sentinel
            123,            # non-string scalar
            ["squash"],     # unhashable type — would have raised TypeError
            {"squash"},     # unhashable type
        ],
    )
    def test_rejects_invalid_strategy(self, bad_strategy: Any) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="strategy"):
            bb_ops.pr_merge(
                _client(opener),
                "acme",
                "widget-service",
                7,
                strategy=bad_strategy,
            )
        assert opener.calls == []

    def test_optional_message(self) -> None:
        opener = _CaptureOpener([_make_pr(7, state="MERGED")])
        bb_ops.pr_merge(
            _client(opener),
            "acme",
            "widget-service",
            7,
            message="Custom merge message",
        )
        assert opener.calls[0]["body"]["message"] == "Custom merge message"

    def test_close_source_branch_override(self) -> None:
        opener = _CaptureOpener([_make_pr(7, state="MERGED")])
        bb_ops.pr_merge(
            _client(opener),
            "acme",
            "widget-service",
            7,
            close_source_branch=False,
        )
        assert opener.calls[0]["body"]["close_source_branch"] is False

    def test_rejects_non_string_message(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="message"):
            bb_ops.pr_merge(
                _client(opener),
                "acme",
                "widget-service",
                7,
                message=123,  # type: ignore[arg-type]
            )
        assert opener.calls == []

    @pytest.mark.parametrize("bad_message", ["", "   ", "\n\t"])
    def test_rejects_empty_or_whitespace_message(self, bad_message: str) -> None:
        # Symmetric with pr_comment_add: an empty (or whitespace-only)
        # message would produce a blank merge-commit subject line,
        # visually empty in any `git log --oneline` view.
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="message"):
            bb_ops.pr_merge(
                _client(opener), "acme", "widget-service", 7, message=bad_message
            )
        assert opener.calls == []

    def test_rejects_non_bool_close_source_branch(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="close_source_branch"):
            bb_ops.pr_merge(
                _client(opener),
                "acme",
                "widget-service",
                7,
                close_source_branch="yes",  # type: ignore[arg-type]
            )
        assert opener.calls == []


# ===========================================================================
# pr_decline
# ===========================================================================


class TestPrDecline:
    def test_post_to_decline_endpoint(self) -> None:
        opener = _CaptureOpener([_make_pr(7, state="DECLINED")])
        bb_ops.pr_decline(_client(opener), "acme", "widget-service", 7)
        call = opener.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == _prs_url() + "/7/decline"
        assert call["body"] is None


# ===========================================================================
# Boundary-validation symmetry — every PR-id-taking op rejects bad IDs
# ===========================================================================


@pytest.mark.parametrize(
    "fn,extra_args",
    [
        (bb_ops.pr_approve, ()),
        (bb_ops.pr_unapprove, ()),
        (bb_ops.pr_decline, ()),
        (bb_ops.pr_comments_list, ()),
        (bb_ops.pr_comment_add, ("body",)),
        # pr_merge has required strategy default; works with no extra args.
        (bb_ops.pr_merge, ()),
        (bb_ops.pr_diff, ()),
        (bb_ops.pr_show, ()),
        (bb_ops.pr_activity, ()),
        # pr_update validates pr_id FIRST (before the empty-payload check),
        # so a bad id raises "pr_id" even with no title/description supplied.
        (bb_ops.pr_update, ()),
    ],
)
def test_every_pr_op_rejects_bad_pr_id(fn: Any, extra_args: tuple[Any, ...]) -> None:
    """If any future refactor removes `_validate_pr_id(pr_id)` from a
    function, this catches it. Without this matrix the validator was
    only directly tested on pr_show / pr_diff."""
    opener = _CaptureOpener([])
    for bad in (0, -5, True, False, "42", 1.5, None):
        with pytest.raises(ValueError, match="pr_id"):
            fn(
                _client(opener),
                "acme",
                "widget-service",
                bad,
                *extra_args,
            )
    assert opener.calls == []


# ===========================================================================
# pr_diff
# ===========================================================================


class TestPrDiff:
    def test_returns_raw_diff_text(self) -> None:
        diff_body = (
            "diff --git a/widget.py b/widget.py\n"
            "+++ b/widget.py\n"
            "@@ -1 +1,2 @@\n"
            "+# new line\n"
        )
        opener = _CaptureOpener([diff_body.encode("utf-8")])
        result = bb_ops.pr_diff(_client(opener), "acme", "widget-service", 42)
        assert result == diff_body
        assert opener.calls[0]["url"] == _prs_url() + "/42/diff"
        assert opener.calls[0]["method"] == "GET"
        # The first hop must carry Authorization (we own the request to
        # api.bitbucket.org). A regression that wired pr_diff to a
        # no-auth path would silently break authenticated diff fetches.
        assert opener.calls[0]["headers"]["Authorization"].startswith("Basic ")

    def test_invalid_pr_id(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="pr_id"):
            bb_ops.pr_diff(_client(opener), "acme", "widget-service", 0)
        assert opener.calls == []

    def test_follows_cross_host_redirect_and_strips_auth(self) -> None:
        """If Bitbucket ever introduces a redirect on the diff endpoint
        (the current behaviour is direct 200), the cross-host-auth-strip
        protection from fetch_redirected_text must apply. A regression
        that wired pr_diff to plain client.get would 5xx on any redirect
        (default opener refuses 3xx) — different from the bash-side
        failure mode but equally surprising. Pin the safe behaviour here
        rather than only in test_bb_api.

        Models the same shape as the pipeline_logs S3-redirect test."""
        remote_url = "https://diff-cache.example.com/acme/widget-service/42.diff?sig=abc"
        redirect = urllib.error.HTTPError(
            url=DEFAULT_API_BASE + "/repositories/acme/widget-service/pullrequests/42/diff",
            code=307,
            msg="Temporary Redirect",
            hdrs={"Location": remote_url},  # type: ignore[arg-type]
            fp=__import__("io").BytesIO(b""),
        )
        opener = _CaptureOpener(
            [
                redirect,
                b"diff body from remote\n",
            ]
        )
        result = bb_ops.pr_diff(_client(opener), "acme", "widget-service", 42)
        assert result == "diff body from remote\n"
        assert len(opener.calls) == 2
        # First hop MUST carry Authorization (we own the request to
        # api.bitbucket.org). A regression that dropped auth on the
        # initial request would otherwise pass silently.
        assert opener.calls[0]["headers"]["Authorization"].startswith("Basic ")
        # And the Location header was actually followed (not refetched
        # original URL).
        assert opener.calls[1]["url"] == remote_url
        # Second hop must NOT carry the Bitbucket credential.
        assert "Authorization" not in opener.calls[1]["headers"], (
            "Bitbucket Basic auth leaked to the diff-cache host"
        )


# ===========================================================================
# pr_comments_list + pr_comment_add
# ===========================================================================


class TestPrCommentsList:
    def test_walks_comments_endpoint(self) -> None:
        opener = _CaptureOpener(
            [
                {
                    "values": [
                        {"id": 1, "content": {"raw": "LGTM"}, "user": {"display_name": "Alice"}},
                        {"id": 2, "content": {"raw": "nit on line 3"}, "user": {"display_name": "Bob"}},
                    ]
                }
            ]
        )
        result = bb_ops.pr_comments_list(
            _client(opener), "acme", "widget-service", 42
        )
        assert [c["id"] for c in result] == [1, 2]
        url = opener.calls[0]["url"]
        assert url.startswith(_prs_url() + "/42/comments?")
        assert "pagelen=100" in url

    def test_rejects_non_positive_count(self) -> None:
        opener = _CaptureOpener([])
        for bad in (0, -1, True, False, "ten"):
            with pytest.raises(ValueError, match="count"):
                bb_ops.pr_comments_list(
                    _client(opener),
                    "acme",
                    "widget-service",
                    42,
                    count=bad,  # type: ignore[arg-type]
                )
        assert opener.calls == []


class TestPrCommentAdd:
    def test_posts_comment_body_in_content_raw(self) -> None:
        opener = _CaptureOpener([{"id": 99, "content": {"raw": "Looks good."}}])
        result = bb_ops.pr_comment_add(
            _client(opener), "acme", "widget-service", 42, "Looks good."
        )
        assert result["id"] == 99
        call = opener.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == _prs_url() + "/42/comments"
        # Bitbucket's contract: {"content": {"raw": "<text>"}}.
        assert call["body"] == {"content": {"raw": "Looks good."}}

    @pytest.mark.parametrize("bad_body", ["", "   ", "\n\t"])
    def test_rejects_empty_or_whitespace_body(self, bad_body: str) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="body"):
            bb_ops.pr_comment_add(
                _client(opener), "acme", "widget-service", 42, bad_body
            )
        assert opener.calls == []

    def test_rejects_non_string_body(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="body"):
            bb_ops.pr_comment_add(
                _client(opener),
                "acme",
                "widget-service",
                42,
                None,  # type: ignore[arg-type]
            )
        assert opener.calls == []


# ===========================================================================
# pr_update
# ===========================================================================


class TestPrUpdate:
    def _url(self, pr_id: int) -> str:
        return _prs_url() + f"/{pr_id}"

    def test_puts_title_and_description(self) -> None:
        opener = _CaptureOpener([_make_pr(42)])
        result = bb_ops.pr_update(
            _client(opener),
            "acme",
            "widget-service",
            42,
            title="New title",
            description="New body.",
        )
        assert result["id"] == 42
        call = opener.calls[0]
        # Method is PUT (Bitbucket Cloud has no PATCH for pullrequests),
        # to the same path pr_show GETs.
        assert call["method"] == "PUT"
        assert call["url"] == self._url(42)
        assert call["body"] == {"title": "New title", "description": "New body."}

    def test_title_only_omits_description(self) -> None:
        opener = _CaptureOpener([_make_pr(42)])
        bb_ops.pr_update(
            _client(opener), "acme", "widget-service", 42, title="Just the title"
        )
        # description omitted (None) → not in body, so the existing PR body
        # is preserved by Bitbucket's field-merge PUT.
        assert opener.calls[0]["body"] == {"title": "Just the title"}

    def test_description_only_omits_title(self) -> None:
        opener = _CaptureOpener([_make_pr(42)])
        bb_ops.pr_update(
            _client(opener),
            "acme",
            "widget-service",
            42,
            description="Just the body",
        )
        assert opener.calls[0]["body"] == {"description": "Just the body"}

    def test_empty_description_clears_body(self) -> None:
        """description="" is a DELIBERATE clear (three-way like repo_update),
        distinct from None (leave unchanged). It must appear in the body."""
        opener = _CaptureOpener([_make_pr(42)])
        bb_ops.pr_update(
            _client(opener), "acme", "widget-service", 42, description=""
        )
        assert opener.calls[0]["body"] == {"description": ""}

    def test_multiline_description_roundtrips_verbatim(self) -> None:
        opener = _CaptureOpener([_make_pr(42)])
        body_text = "# Heading\n\n- bullet one\n- bullet two\n\nTrailing para.\n"
        bb_ops.pr_update(
            _client(opener),
            "acme",
            "widget-service",
            42,
            title="t",
            description=body_text,
        )
        assert opener.calls[0]["body"]["description"] == body_text

    def test_rejects_empty_body_no_fields(self) -> None:
        """Neither title nor description → a PUT with an empty body would be
        a no-op round-trip. Reject at the boundary with NO network IO."""
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="at least one field"):
            bb_ops.pr_update(_client(opener), "acme", "widget-service", 42)
        assert opener.calls == []

    @pytest.mark.parametrize("bad_title", ["", "   ", "\n\t"])
    def test_rejects_empty_or_whitespace_title(self, bad_title: str) -> None:
        """A supplied title must be non-empty/non-whitespace (a PR needs a
        title; Bitbucket rejects a blank one). Symmetric with pr_create."""
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="title"):
            bb_ops.pr_update(
                _client(opener), "acme", "widget-service", 42, title=bad_title
            )
        assert opener.calls == []

    def test_rejects_non_string_title(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="title"):
            bb_ops.pr_update(
                _client(opener),
                "acme",
                "widget-service",
                42,
                title=123,  # type: ignore[arg-type]
            )
        assert opener.calls == []

    def test_rejects_non_string_description(self) -> None:
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="description"):
            bb_ops.pr_update(
                _client(opener),
                "acme",
                "widget-service",
                42,
                description=123,  # type: ignore[arg-type]
            )
        assert opener.calls == []

    def test_pr_id_validated_before_empty_payload_check(self) -> None:
        """A bad pr_id raises the pr_id error even when no fields are given
        — identity is validated before content, so the caller gets the most
        specific error and no network IO happens either way."""
        opener = _CaptureOpener([])
        with pytest.raises(ValueError, match="pr_id"):
            bb_ops.pr_update(_client(opener), "acme", "widget-service", 0)
        assert opener.calls == []
