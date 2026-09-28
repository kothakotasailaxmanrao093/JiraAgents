"""Reading the files and pages a requirement actually lives in.

These tests care as much about what is *reported* when something cannot be read
as about the happy path. A ticket whose description says "see the attached spec"
produces, if the spec is never opened, a long list of "missing detail" findings
that are really findings about the reviewer — and the person reading them cannot
tell the difference. So every unreadable file and unreachable page must come
back named, in ``ctx.warnings``.
"""

from __future__ import annotations

from typing import Any

import pytest

from context.attachment_source import AttachmentsSource, ConfluenceSource
from context.base import FetchContext
from shared.transport import TransportError
from tools.gather_context_tool import requirement_is_present

SITE = "https://example.atlassian.net"


class FakeTransport:
    """Answers by longest matching URL fragment; anything else 404s."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.seen: list[str] = []

    def _match(self, url: str) -> Any:
        self.seen.append(url)
        hits = [f for f in self.routes if f in url]
        if not hits:
            raise TransportError(f"GET {url} -> 404", status=404, url=url)
        value = self.routes[max(hits, key=len)]
        if isinstance(value, Exception):
            raise value
        return value

    async def get_json(self, url: str, params: Any = None) -> Any:
        return self._match(url)

    async def get_bytes(self, url: str) -> bytes:
        return self._match(url)

    async def post_json(self, url: str, body: Any) -> Any:  # pragma: no cover
        raise AssertionError("reading context must never POST")

    async def resolve_url(self, url: str) -> str:  # pragma: no cover
        return url


class FakeJira:
    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport
        self.base = SITE


def _ctx(bundle: dict, transport: FakeTransport, **over: Any) -> FetchContext:
    ctx = FetchContext(issue_key=bundle.get("key", "ABC-1"), target_bundle=bundle, **over)
    ctx.jira = FakeJira(transport)
    return ctx


def _attachment(filename: str, url: str | None = None, mime: str = "text/csv") -> dict:
    """``url=""`` means Jira gave us no download link; ``None`` means "the usual"."""
    return {
        "filename": filename,
        "mime_type": mime,
        "size": 100,
        "content_url": f"{SITE}/secure/attachment/1/{filename}" if url is None else url,
    }


# --- attachments -------------------------------------------------------------


async def test_an_attached_file_becomes_evidence() -> None:
    bundle = {"key": "ABC-1", "attachments": [_attachment("rules.csv")], "attachments_total": 1}
    transport = FakeTransport({"rules.csv": b"rule,limit\nbreak,30\n"})
    ctx = _ctx(bundle, transport)

    docs = await AttachmentsSource().load(ctx)

    assert len(docs) == 1
    assert docs[0].kind == "attachment"
    assert docs[0].source_label == "Attachment rules.csv (on ABC-1)"
    assert "break" in docs[0].text
    assert ctx.warnings == []


async def test_a_file_that_will_not_download_is_named_not_swallowed() -> None:
    bundle = {"key": "ABC-1", "attachments": [_attachment("spec.pdf")], "attachments_total": 1}
    transport = FakeTransport({"spec.pdf": TransportError("403", status=403)})
    ctx = _ctx(bundle, transport)

    docs = await AttachmentsSource().load(ctx)

    assert docs == []
    assert any("spec.pdf" in w for w in ctx.warnings), ctx.warnings


async def test_an_unsupported_file_type_is_named() -> None:
    """A .zip is not readable, and the review must say which file it skipped."""
    bundle = {"key": "ABC-1", "attachments": [_attachment("bundle.zip")], "attachments_total": 1}
    transport = FakeTransport({"bundle.zip": b"PK\x03\x04"})
    ctx = _ctx(bundle, transport)

    docs = await AttachmentsSource().load(ctx)

    assert docs == []
    assert any("bundle.zip" in w for w in ctx.warnings), ctx.warnings


async def test_an_attachment_with_no_download_link_is_named() -> None:
    bundle = {
        "key": "ABC-1",
        "attachments": [_attachment("ghost.csv", url="")],
        "attachments_total": 1,
    }
    ctx = _ctx(bundle, FakeTransport())

    docs = await AttachmentsSource().load(ctx)

    assert docs == []
    assert any("ghost.csv" in w and "no download link" in w for w in ctx.warnings)


async def test_one_bad_file_does_not_stop_the_others() -> None:
    bundle = {
        "key": "ABC-1",
        "attachments": [_attachment("broken.pdf"), _attachment("good.csv")],
        "attachments_total": 2,
    }
    transport = FakeTransport(
        {"broken.pdf": TransportError("500", status=500), "good.csv": b"a,b\n1,2\n"}
    )
    ctx = _ctx(bundle, transport)

    docs = await AttachmentsSource().load(ctx)

    assert [d.metadata["filename"] for d in docs] == ["good.csv"]
    assert any("broken.pdf" in w for w in ctx.warnings)


async def test_a_capped_attachment_list_says_so() -> None:
    """ "ALL attachments" coverage has to be honest about the cap."""
    bundle = {"key": "ABC-1", "attachments": [_attachment("a.csv")], "attachments_total": 12}
    ctx = _ctx(bundle, FakeTransport({"a.csv": b"x,y\n1,2\n"}))

    await AttachmentsSource().load(ctx)

    assert any("12 attachments" in w for w in ctx.warnings), ctx.warnings


def test_the_source_is_off_when_the_flag_is_off() -> None:
    bundle = {"key": "ABC-1", "attachments": [_attachment("a.csv")]}
    assert AttachmentsSource().is_enabled(_ctx(bundle, FakeTransport()))
    assert not AttachmentsSource().is_enabled(
        _ctx(bundle, FakeTransport(), include_attachments=False)
    )


def test_the_source_is_off_when_there_is_nothing_attached() -> None:
    assert not AttachmentsSource().is_enabled(_ctx({"key": "ABC-1"}, FakeTransport()))


# --- Confluence --------------------------------------------------------------


def _page(body: str = "<p>Drivers log a break.</p>") -> dict:
    return {
        "id": "131181",
        "title": "Driver Breaks",
        "space": {"key": "FL"},
        "body": {"storage": {"value": body}},
    }


async def test_a_linked_page_becomes_evidence() -> None:
    bundle = {
        "key": "ABC-1",
        "description_text": f"spec: {SITE}/wiki/spaces/FL/pages/131181/Driver+Breaks",
        "comments": [],
    }
    transport = FakeTransport(
        {
            "/content/131181": _page(),
            "/child/attachment": {"results": []},
            "/remotelink": [],
        }
    )
    ctx = _ctx(bundle, transport)

    docs = await ConfluenceSource().load(ctx)

    assert len(docs) == 1
    assert docs[0].kind == "confluence"
    assert docs[0].source_label == 'Confluence: "Driver Breaks"'
    assert "Drivers log a break." in docs[0].text


async def test_a_page_linked_only_through_jiras_panel_is_still_found() -> None:
    """Jira's "Link > Confluence page" never appears in the issue fields, so
    nothing that reads the description can see it."""
    bundle = {"key": "ABC-1", "description_text": "no links here", "comments": []}
    transport = FakeTransport(
        {
            "/remotelink": [
                {"object": {"url": f"{SITE}/wiki/spaces/FL/pages/131181/Driver+Breaks"}}
            ],
            "/content/131181": _page(),
            "/child/attachment": {"results": []},
        }
    )
    ctx = _ctx(bundle, transport)

    docs = await ConfluenceSource().load(ctx)

    assert [d.kind for d in docs] == ["confluence"]


async def test_a_page_this_account_cannot_read_is_named() -> None:
    bundle = {
        "key": "ABC-1",
        "description_text": f"{SITE}/wiki/spaces/FL/pages/131181/Ops+Runbook",
        "comments": [],
    }
    transport = FakeTransport(
        {"/remotelink": [], "/content/131181": TransportError("403", status=403)}
    )
    ctx = _ctx(bundle, transport)

    docs = await ConfluenceSource().load(ctx)

    assert docs == []
    assert any("not permitted" in w for w in ctx.warnings), ctx.warnings


async def test_a_file_attached_to_a_page_becomes_its_own_document() -> None:
    bundle = {
        "key": "ABC-1",
        "description_text": f"{SITE}/wiki/spaces/FL/pages/131181/Driver+Breaks",
        "comments": [],
    }
    transport = FakeTransport(
        {
            "/remotelink": [],
            "/content/131181": _page("<p>See the attached sheet.</p>"),
            "/child/attachment": {
                "results": [
                    {
                        "title": "rules.csv",
                        "extensions": {"mediaType": "text/csv"},
                        "_links": {"download": "/download/attachments/131181/rules.csv"},
                    }
                ]
            },
            "rules.csv": b"rule,limit\nbreak,30\n",
        }
    )
    ctx = _ctx(bundle, transport)

    docs = await ConfluenceSource().load(ctx)

    kinds = [d.kind for d in docs]
    assert kinds == ["confluence", "attachment"]
    assert "break" in docs[1].text


async def test_a_link_to_another_host_is_never_followed() -> None:
    """Anyone who can comment can put a URL here."""
    bundle = {
        "key": "ABC-1",
        "description_text": "see https://evil.example.com/wiki/spaces/FL/pages/1/x",
        "comments": [],
    }
    transport = FakeTransport({"/remotelink": []})
    ctx = _ctx(bundle, transport)

    docs = await ConfluenceSource().load(ctx)

    assert docs == []
    assert not any("evil.example.com" in u for u in transport.seen)


@pytest.mark.parametrize("flag", [True, False])
def test_confluence_respects_its_flag(flag: bool) -> None:
    ctx = _ctx({"key": "ABC-1"}, FakeTransport(), include_confluence=flag)
    assert ConfluenceSource().is_enabled(ctx) is flag


# --- the requirement living only in a file ----------------------------------
#
# These exercise the real predicate from the gather activity, not a copy of it:
# a test that re-expresses the logic passes even when the code is wrong.


def test_a_requirement_only_in_an_attachment_counts_as_present() -> None:
    """A ticket whose description is "see attached" used to abort as "nothing to
    review", which is the opposite of the truth."""
    assert requirement_is_present(
        {"description_text": ""},
        [{"kind": "attachment", "text": "The driver must log a break every 4 hours."}],
    )


def test_a_requirement_only_on_a_confluence_page_counts_as_present() -> None:
    assert requirement_is_present(
        {"description_text": "   "},
        [{"kind": "confluence", "text": "Drivers log a break every 4 hours."}],
    )


def test_an_empty_attachment_does_not_fake_a_requirement() -> None:
    assert not requirement_is_present(
        {"description_text": ""}, [{"kind": "attachment", "text": "   "}]
    )


def test_supporting_context_alone_is_not_a_requirement() -> None:
    """A parent card and some comments are not the thing being reviewed."""
    assert not requirement_is_present(
        {"description_text": ""},
        [
            {"kind": "parent_context", "text": "background"},
            {"kind": "subtask_comments", "text": "chatter"},
        ],
    )


def test_a_description_alone_is_still_enough() -> None:
    assert requirement_is_present({"description_text": "Log a break."}, [])
