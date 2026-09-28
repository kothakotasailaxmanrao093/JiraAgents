"""The aiohttp adapter behind the shared transport port.

The work-breakdown agent has the same tests against its httpx adapter. Both must
behave identically, because shared code is written against the protocol and
cannot tell which one it is holding — that substitutability is the whole point
of the port.

A fake session is used rather than a live one for the same reason ``JiraClient``
is tested that way: the adapter's job is translation, not networking.
"""

from __future__ import annotations

from typing import Any

import pytest

from jira.transport import AiohttpTransport
from shared.transport import Transport, TransportError

SITE = "https://example.atlassian.net"


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        payload: Any = None,
        body: bytes = b"",
        text: str = "",
        url: str = "",
    ) -> None:
        self.status = status
        self._payload = payload
        self._body = body
        self._text = text
        self.url = url

    async def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSession:
    """Records the call and returns a queued response (or raises)."""

    def __init__(self, response: Any = None, raises: Exception | None = None) -> None:
        self._response = response
        self._raises = raises
        self.calls: list[tuple[str, str, Any]] = []

    def _respond(self, method: str, url: str, extra: Any) -> Any:
        self.calls.append((method, url, extra))
        if self._raises is not None:
            raise self._raises
        return self._response

    def get(self, url: str, params: Any = None, headers: Any = None) -> Any:
        return self._respond("GET", url, params or headers)

    def post(self, url: str, json: Any = None) -> Any:
        return self._respond("POST", url, json)


def test_the_adapter_satisfies_the_shared_protocol() -> None:
    """Structural check — shared code depends on the protocol, not this class."""
    assert isinstance(AiohttpTransport(FakeSession()), Transport)


async def test_json_is_decoded() -> None:
    session = FakeSession(FakeResponse(200, payload={"id": "131181"}))
    assert await AiohttpTransport(session).get_json(f"{SITE}/x") == {"id": "131181"}


async def test_integer_query_values_are_stringified() -> None:
    """aiohttp rejects non-string query values; httpx accepts them.

    Shared code passes ``{"limit": 10}``, so the adapter has to absorb the
    difference or the two transports are not substitutable.
    """
    session = FakeSession(FakeResponse(200, payload={}))
    await AiohttpTransport(session).get_json(f"{SITE}/x", {"limit": 10, "expand": "body"})
    _, _, params = session.calls[0]
    assert params == {"limit": "10", "expand": "body"}


async def test_none_query_values_are_dropped() -> None:
    session = FakeSession(FakeResponse(200, payload={}))
    await AiohttpTransport(session).get_json(f"{SITE}/x", {"a": 1, "b": None})
    assert session.calls[0][2] == {"a": "1"}


async def test_bytes_come_back_untouched() -> None:
    session = FakeSession(FakeResponse(200, body=b"rule,limit\nbreak,30\n"))
    assert await AiohttpTransport(session).get_bytes(f"{SITE}/f.csv") == b"rule,limit\nbreak,30\n"


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_an_error_status_is_translated_and_kept(status: int) -> None:
    session = FakeSession(FakeResponse(status, text="nope"))
    with pytest.raises(TransportError) as caught:
        await AiohttpTransport(session).get_json(f"{SITE}/x")
    assert caught.value.status == status


async def test_permission_and_absence_are_distinguishable() -> None:
    forbidden = FakeSession(FakeResponse(403, text=""))
    with pytest.raises(TransportError) as caught:
        await AiohttpTransport(forbidden).get_json(f"{SITE}/x")
    assert caught.value.is_forbidden and not caught.value.is_not_found

    missing = FakeSession(FakeResponse(404, text=""))
    with pytest.raises(TransportError) as caught:
        await AiohttpTransport(missing).get_json(f"{SITE}/x")
    assert caught.value.is_not_found and not caught.value.is_forbidden


async def test_a_connection_failure_has_no_status() -> None:
    session = FakeSession(raises=OSError("no route to host"))
    with pytest.raises(TransportError) as caught:
        await AiohttpTransport(session).get_json(f"{SITE}/x")
    assert caught.value.status is None


async def test_posting_returns_the_decoded_body() -> None:
    session = FakeSession(FakeResponse(201, payload={"id": "10501"}, text='{"id":"10501"}'))
    assert await AiohttpTransport(session).post_json(f"{SITE}/c", {}) == {"id": "10501"}


async def test_an_empty_success_body_is_an_empty_dict_not_a_crash() -> None:
    """Jira answers 204 with no body for some writes."""
    session = FakeSession(FakeResponse(204, text=""))
    assert await AiohttpTransport(session).post_json(f"{SITE}/c", {}) == {}


async def test_a_success_with_an_undecodable_body_is_not_an_error() -> None:
    session = FakeSession(FakeResponse(200, payload=None, text="OK"))
    assert await AiohttpTransport(session).post_json(f"{SITE}/c", {}) == {}


async def test_a_shortlink_resolves_to_where_it_redirected() -> None:
    """aiohttp follows redirects itself, so the final URL is on the response."""
    final = f"{SITE}/wiki/spaces/FL/pages/131181/Driver+Breaks"
    session = FakeSession(FakeResponse(200, url=final))
    assert await AiohttpTransport(session).resolve_url(f"{SITE}/wiki/x/AbCdEf") == final
