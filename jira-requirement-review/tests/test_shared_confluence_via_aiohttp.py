"""The shared Confluence reader, driven through this agent's aiohttp adapter.

This is the point of the transport port. The reading logic was written for the
work-breakdown agent against ``httpx`` and is exercised there by its own suite;
these tests show the *same module* working unchanged through an ``aiohttp``
session, which is what lets this agent read Confluence in Phase 1 without a
second copy of the code.

The routing fake answers by URL, so a wrong endpoint fails loudly rather than
silently returning the previous response.
"""

from __future__ import annotations

from typing import Any

from jira.transport import AiohttpTransport
from shared.confluence_fetch import FetchConfig, fetch_linked_pages, fetch_page
from shared.confluence_text import parse_reference

SITE = "https://example.atlassian.net"


class _Resp:
    def __init__(self, status: int, payload: Any = None, body: bytes = b"") -> None:
        self.status = status
        self._payload = payload
        self._body = body
        self.url = ""

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return "" if self._payload is None else "body"

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class RoutedSession:
    """Answers by URL fragment, most specific first; unmatched URLs are a 404.

    Longest-match rather than insertion order, so that "/content/131181" cannot
    shadow "/content/131181/child/attachment" and quietly hand the page payload
    back as the attachment listing.
    """

    def __init__(self, routes: dict[str, _Resp]) -> None:
        self.routes = routes
        self.seen: list[str] = []

    def get(self, url: str, params: Any = None, headers: Any = None) -> _Resp:
        self.seen.append(url)
        matches = [f for f in self.routes if f in url]
        if matches:
            return self.routes[max(matches, key=len)]
        return _Resp(404, payload={})

    def post(self, url: str, json: Any = None) -> _Resp:  # pragma: no cover
        raise AssertionError("reading Confluence must never POST")


def _page_payload(body: str = "<p>Drivers log a break.</p>") -> dict:
    return {
        "id": "131181",
        "title": "Driver Breaks",
        "space": {"key": "FL"},
        "body": {"storage": {"value": body}},
    }


def _attachment_listing(name: str) -> dict:
    return {
        "results": [
            {
                "title": name,
                "extensions": {"mediaType": "text/csv"},
                "_links": {"download": f"/download/attachments/131181/{name}"},
            }
        ]
    }


async def test_a_linked_page_is_read_through_the_aiohttp_adapter() -> None:
    session = RoutedSession(
        {
            "/content/131181": _Resp(200, payload=_page_payload()),
            "/child/attachment": _Resp(200, payload={"results": []}),
        }
    )
    ref = parse_reference(f"{SITE}/wiki/spaces/FL/pages/131181/Driver+Breaks", SITE)
    assert ref is not None

    page = await fetch_page(AiohttpTransport(session), ref, SITE)

    assert page.title == "Driver Breaks"
    assert page.space_key == "FL"
    assert page.text == "Drivers log a break."
    assert page.usable


async def test_the_structure_of_a_page_survives_the_round_trip() -> None:
    """A bullet list must not arrive as one run-on line — the whole reason the
    shared reader replaced this agent's old flattener."""
    body = "<p>Criteria:</p><ul><li>log a break</li><li>see past breaks</li></ul>"
    session = RoutedSession(
        {
            "/content/131181": _Resp(200, payload=_page_payload(body)),
            "/child/attachment": _Resp(200, payload={"results": []}),
        }
    )
    ref = parse_reference(f"{SITE}/wiki/spaces/FL/pages/131181", SITE)
    assert ref is not None

    page = await fetch_page(AiohttpTransport(session), ref, SITE)

    assert "- log a break" in page.text
    assert "- see past breaks" in page.text


async def test_a_file_attached_to_a_page_is_read() -> None:
    """The spec is often the spreadsheet hanging off the page."""
    session = RoutedSession(
        {
            "/content/131181": _Resp(200, payload=_page_payload("<p>See the attached sheet.</p>")),
            "/child/attachment": _Resp(200, payload=_attachment_listing("rules.csv")),
            "rules.csv": _Resp(200, body=b"rule,limit\nbreak,30\n"),
        }
    )
    ref = parse_reference(f"{SITE}/wiki/spaces/FL/pages/131181", SITE)
    assert ref is not None

    page = await fetch_page(AiohttpTransport(session), ref, SITE)

    assert len(page.attachments) == 1
    assert page.attachments[0].filename == "rules.csv"
    assert "break" in page.attachments[0].text


async def test_a_page_this_account_may_not_read_is_reported_not_raised() -> None:
    """ "I could not read the spec" must never look like "the spec says nothing"."""
    session = RoutedSession({"/content/131181": _Resp(403, payload={})})
    ref = parse_reference(f"{SITE}/wiki/spaces/FL/pages/131181", SITE)
    assert ref is not None

    page = await fetch_page(AiohttpTransport(session), ref, SITE)

    assert not page.usable
    assert "not permitted" in page.note


async def test_a_page_on_another_host_is_never_fetched() -> None:
    """The same-site guard sits in the pure layer, ahead of any transport."""
    session = RoutedSession({})
    pages, notes = await fetch_linked_pages(
        AiohttpTransport(session),
        ["see https://evil.example.com/wiki/spaces/FL/pages/1/x"],
        SITE,
    )
    assert pages == []
    assert notes == []
    assert session.seen == [], "no request may be made to another host"


async def test_the_page_cap_is_reported_rather_than_silently_applied() -> None:
    session = RoutedSession(
        {
            "/content/": _Resp(200, payload=_page_payload()),
            "/child/attachment": _Resp(200, payload={"results": []}),
        }
    )
    links = " ".join(f"{SITE}/wiki/spaces/FL/pages/{n}/P{n}" for n in range(1, 5))

    pages, notes = await fetch_linked_pages(
        AiohttpTransport(session), [links], SITE, cfg=FetchConfig(max_pages=2)
    )

    assert len(pages) == 2
    assert any("only the first 2 were read" in n for n in notes)
