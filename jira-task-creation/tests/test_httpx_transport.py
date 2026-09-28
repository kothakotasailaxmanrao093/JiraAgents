"""The httpx adapter behind the shared transport port.

Shared code catches exactly one error type, so the adapter's real job is
translation: every way httpx can fail must arrive as a ``TransportError``
carrying the status when the server answered. A 403 and a 404 produce different
advice in the reply a user reads, so losing the status loses the advice.
"""

from __future__ import annotations

import httpx
import pytest

from src.jira.transport import HttpxTransport
from src.shared.transport import Transport, TransportError

SITE = "https://example.atlassian.net"


def transport_returning(handler) -> tuple[HttpxTransport, httpx.AsyncClient]:
    client = httpx.AsyncClient(base_url=SITE, transport=httpx.MockTransport(handler))
    return HttpxTransport(client), client


def test_the_adapter_satisfies_the_shared_protocol() -> None:
    """Structural check — shared code depends on the protocol, not this class."""
    adapter, _ = transport_returning(lambda r: httpx.Response(200, json={}))
    assert isinstance(adapter, Transport)


async def test_json_is_decoded_and_query_parameters_are_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"id": "131181"})

    adapter, client = transport_returning(handler)
    async with client:
        assert await adapter.get_json(f"{SITE}/x", {"expand": "body.storage"}) == {"id": "131181"}
    assert seen == {"expand": "body.storage"}


async def test_bytes_come_back_untouched() -> None:
    adapter, client = transport_returning(
        lambda r: httpx.Response(200, content=b"rule,limit\nbreak,30\n")
    )
    async with client:
        assert await adapter.get_bytes(f"{SITE}/f.csv") == b"rule,limit\nbreak,30\n"


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_an_error_status_is_translated_and_kept(status: int) -> None:
    """The status must survive: it decides what the user is told to do."""
    adapter, client = transport_returning(lambda r: httpx.Response(status, json={}))
    async with client:
        with pytest.raises(TransportError) as caught:
            await adapter.get_json(f"{SITE}/x")
    assert caught.value.status == status


async def test_permission_and_absence_are_distinguishable() -> None:
    for status, forbidden, missing in ((403, True, False), (404, False, True)):
        adapter, client = transport_returning(lambda r, s=status: httpx.Response(s, json={}))
        async with client:
            with pytest.raises(TransportError) as caught:
                await adapter.get_json(f"{SITE}/x")
        assert caught.value.is_forbidden is forbidden
        assert caught.value.is_not_found is missing


async def test_a_connection_failure_has_no_status() -> None:
    """Nothing answered, so there is no status to report — and it must not crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    adapter, client = transport_returning(handler)
    async with client:
        with pytest.raises(TransportError) as caught:
            await adapter.get_json(f"{SITE}/x")
    assert caught.value.status is None


async def test_posting_returns_the_decoded_body() -> None:
    adapter, client = transport_returning(lambda r: httpx.Response(201, json={"id": "10501"}))
    async with client:
        assert await adapter.post_json(f"{SITE}/c", {"body": {}}) == {"id": "10501"}


async def test_an_empty_success_body_is_an_empty_dict_not_a_crash() -> None:
    """Jira answers 204 with no body for some writes."""
    adapter, client = transport_returning(lambda r: httpx.Response(204))
    async with client:
        assert await adapter.post_json(f"{SITE}/c", {}) == {}


async def test_a_shortlink_resolves_to_where_it_redirected() -> None:
    """This is the only reason ``resolve_url`` exists."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wiki/x/AbCdEf":
            return httpx.Response(
                302, headers={"Location": f"{SITE}/wiki/spaces/FL/pages/131181/Driver+Breaks"}
            )
        return httpx.Response(200, text="<html></html>")

    adapter, client = transport_returning(handler)
    async with client:
        final = await adapter.resolve_url(f"{SITE}/wiki/x/AbCdEf")
    assert final == f"{SITE}/wiki/spaces/FL/pages/131181/Driver+Breaks"
