# GENERATED FILE — DO NOT EDIT.
# Mirrored from the canonical shared/ package by scripts/sync_shared.py.
# Edit shared/transport.py at the repository root and re-run that script.
"""The narrow HTTP port the shared package depends on.

The work-breakdown agent speaks ``httpx``; the review agent speaks ``aiohttp``. Both are working,
tested production code, and neither has a behavioural reason to change. Forcing
one onto the other's library to enable sharing would rewrite a live HTTP layer
for no user-visible gain.

So shared code depends on this Protocol instead of on either library, and each
agent supplies a ~20-line adapter over the client it already builds. That is
Dependency Inversion: the shared policy (how to read a Confluence page, how to
fetch an attachment) owns the interface, and the agents' transports conform.

Four methods, because four is all the shared code needs:

* ``get_json``      — read an API resource
* ``get_bytes``     — download an attachment
* ``post_json``     — write a comment
* ``resolve_url``   — follow redirects and report where they landed

``resolve_url`` earns its place rather than being general-purpose HTTP leaking
in: a Confluence "shortlink" (``/wiki/x/AbCdEf``) names a page only by where it
redirects to, so resolving one is impossible without seeing the final URL.

Deliberately NOT a general HTTP client. A wider port would tempt shared code
into needing streaming, redirects, retries and connection pooling, at which
point the agents' clients stop being substitutable. Interface Segregation: no
adapter should have to implement a method the shared code never calls.

Errors: an adapter raises :class:`TransportError` for anything it could not
complete, with ``status`` set when the server answered. Shared code catches that
one type, so it never has to know which library produced it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class TransportError(Exception):
    """A request that could not be completed.

    ``status`` is the HTTP status when the server replied, and ``None`` for a
    connection-level failure. Callers distinguish "you may not read this" (403)
    from "this did not exist" (404) from "the network broke", because the three
    produce different advice in the "What was missing" section of a reply.
    """

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url

    @property
    def is_not_found(self) -> bool:
        return self.status == 404

    @property
    def is_forbidden(self) -> bool:
        return self.status in (401, 403)


@runtime_checkable
class Transport(Protocol):
    """What shared code needs from an HTTP client. Implemented per agent."""

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``url`` and return decoded JSON. Raise ``TransportError`` otherwise."""
        ...

    async def get_bytes(self, url: str) -> bytes:
        """GET ``url`` and return the raw body — an attachment download."""
        ...

    async def post_json(self, url: str, body: dict[str, Any]) -> Any:
        """POST ``body`` as JSON and return the decoded response (``{}`` if empty)."""
        ...

    async def resolve_url(self, url: str) -> str:
        """GET ``url`` following redirects, and return the final URL.

        Only the destination matters, never the body — this exists to turn a
        Confluence shortlink into the page URL it points at.
        """
        ...
