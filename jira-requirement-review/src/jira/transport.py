"""The aiohttp side of the shared :class:`Transport` port.

Shared code (Confluence fetching, attachment downloads) depends on the narrow
protocol in :mod:`shared.transport` rather than on an HTTP library, so this
agent and the work-breakdown agent can share it while each keeps the client it
already builds. This adapter is this agent's half: it wraps the
``aiohttp.ClientSession`` that ``jira_client_from_env`` creates and translates
aiohttp's failures into the one error type shared code knows about.

It deliberately adds no behaviour — no retries, no caching, no logging. Anything
clever here would be invisible to the shared code depending on it, and would
drift from the httpx adapter on the other side.

The session is injected rather than created here, so this stays unit-testable
against a fake transport exactly like ``JiraClient`` is.
"""

from __future__ import annotations

from typing import Any

from shared.transport import TransportError

_OK = (200, 201, 204)


class AiohttpTransport:
    """Adapts an ``aiohttp.ClientSession`` to the shared transport protocol."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        try:
            async with self._session.get(url, params=_stringify(params)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise TransportError(
                        f"GET {url} -> {resp.status}: {text[:400]}",
                        status=resp.status,
                        url=url,
                    )
                return await resp.json()
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001 — connection-level failure
            raise TransportError(f"GET {url} failed: {exc}", url=url) from exc

    async def get_bytes(self, url: str) -> bytes:
        try:
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    raise TransportError(f"GET {url} -> {resp.status}", status=resp.status, url=url)
                return await resp.read()
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001 — connection-level failure
            raise TransportError(f"GET {url} failed: {exc}", url=url) from exc

    async def post_json(self, url: str, body: dict[str, Any]) -> Any:
        try:
            async with self._session.post(url, json=body) as resp:
                text = await resp.text()
                if resp.status not in _OK:
                    raise TransportError(
                        f"POST {url} -> {resp.status}: {text[:400]}",
                        status=resp.status,
                        url=url,
                    )
                if not text:
                    return {}
                try:
                    return await resp.json()
                except Exception:  # noqa: BLE001 — a 2xx with a non-JSON body
                    return {}
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001 — connection-level failure
            raise TransportError(f"POST {url} failed: {exc}", url=url) from exc

    async def resolve_url(self, url: str) -> str:
        """Follow redirects and report where they landed.

        aiohttp follows redirects by default, so the final URL is simply
        ``resp.url``. The body is thrown away: this exists only to turn a
        Confluence shortlink into the page URL it points at.
        """
        try:
            async with self._session.get(url, headers={"Accept": "text/html"}) as resp:
                return str(resp.url)
        except Exception as exc:  # noqa: BLE001 — connection-level failure
            raise TransportError(f"GET {url} failed: {exc}", url=url) from exc


def _stringify(params: dict[str, Any] | None) -> dict[str, str] | None:
    """aiohttp rejects non-string query values; httpx accepts them.

    Shared code passes ints (``{"limit": 10}``), so they are coerced here rather
    than forcing every caller to know which client it is talking to.
    """
    if not params:
        return None
    return {k: str(v) for k, v in params.items() if v is not None}
