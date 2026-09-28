"""The httpx side of the shared :class:`Transport` port.

Shared code (Confluence fetching, attachment downloads) depends on the narrow
protocol in :mod:`src.shared.transport` rather than on an HTTP library, so this
agent and the review agent can share it while each keeps the client it already
builds. This adapter is that agent's half: it wraps the ``httpx.AsyncClient``
the pipeline already creates and translates httpx's exceptions into the one
error type shared code knows about.

It deliberately adds no behaviour — no retries, no caching, no logging. Anything
clever here would be invisible to the shared code depending on it, and would
drift from the aiohttp adapter on the other side.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.shared.transport import TransportError


def _as_transport_error(exc: httpx.HTTPError, url: str) -> TransportError:
    """One error type for shared code, with the status when the server answered."""
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    return TransportError(str(exc), status=status, url=url)


class HttpxTransport:
    """Adapts an ``httpx.AsyncClient`` to the shared transport protocol."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise _as_transport_error(exc, url) from exc

    async def get_bytes(self, url: str) -> bytes:
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as exc:
            raise _as_transport_error(exc, url) from exc

    async def post_json(self, url: str, body: dict[str, Any]) -> Any:
        try:
            resp = await self._client.post(url, json=body)
            resp.raise_for_status()
            if not resp.content:
                return {}
            return resp.json()
        except httpx.HTTPError as exc:
            raise _as_transport_error(exc, url) from exc

    async def resolve_url(self, url: str) -> str:
        """Follow redirects and report where they landed.

        ``Accept: text/html`` because this only ever follows a Confluence
        shortlink, which redirects to the page's HTML view; the body is thrown
        away and only the final URL is used.
        """
        try:
            resp = await self._client.get(
                url, follow_redirects=True, headers={"Accept": "text/html"}
            )
            return str(resp.url)
        except httpx.HTTPError as exc:
            raise _as_transport_error(exc, url) from exc
