"""A stand-in for ``httpx.AsyncClient`` that replays canned Jira responses."""

from __future__ import annotations

from typing import Any

import httpx


class FakeResponse:
    def __init__(self, status_code: int = 200, json_body: Any = None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.content = content
        self.text = str(self._json)

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "https://example.atlassian.net"),
                response=httpx.Response(self.status_code, text=self.text),
            )


class FakeClient:
    """Routes calls by URL substring. Records every request for assertions."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def _resolve(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        for fragment, response in self.routes.items():
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(url, kwargs)
                return response
        return FakeResponse(404, {"errorMessages": [f"no route for {url}"]})

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._resolve("GET", url, kwargs)

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._resolve("POST", url, kwargs)

    async def put(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._resolve("PUT", url, kwargs)
