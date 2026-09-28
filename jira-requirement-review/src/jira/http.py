"""Low-level Jira HTTP helpers — generalized from ``my_first_agent``.

Pure async helpers over an ``aiohttp.ClientSession``. No SDK imports; the caller
owns the session (and its BasicAuth). ``adf_to_text`` renders an Atlassian
Document Format node (issue/comment bodies) to text, keeping its structure.
"""

from __future__ import annotations

import logging
from typing import Any

# Reading ADF back to text is shared with the other Jira agents. This module
# used to flatten every text node onto one line with spaces, which turned a
# ticket's acceptance-criteria bullet list into an unreadable run-on sentence —
# fatal for an agent whose job is to notice that acceptance criteria are
# missing. The shared reader keeps the line structure.
from shared.adf import to_text as adf_to_text

logger = logging.getLogger(__name__)

JIRA_API = "/rest/api/3"


async def get_json(session: Any, url: str, params: dict | None = None) -> Any:
    """GET ``url`` and return parsed JSON; raise ``RuntimeError`` on non-200."""
    async with session.get(url, params=params) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"GET {url} -> {resp.status}: {text[:400]}")
        return await resp.json()


async def post_json(session: Any, url: str, body: dict) -> Any:
    """POST ``body`` as JSON; raise ``RuntimeError`` on non-2xx. Returns parsed JSON
    (or ``{}`` when the response has no body)."""
    async with session.post(url, json=body) as resp:
        text = await resp.text()
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"POST {url} -> {resp.status}: {text[:400]}")
        if not text:
            return {}
        try:
            return await resp.json()
        except Exception:
            return {}


__all__ = ["JIRA_API", "adf_to_text", "get_json", "post_json"]
