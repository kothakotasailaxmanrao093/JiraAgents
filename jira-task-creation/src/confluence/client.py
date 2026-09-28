"""Reading Confluence pages that a Jira ticket points at — this agent's settings.

The reading itself now lives in :mod:`src.shared.confluence_fetch`, shared with
the review agent, which needs exactly the same capability and would otherwise
have grown a second copy of it. That module does its I/O through the narrow
transport port, so it serves an agent on ``httpx`` and one on ``aiohttp`` alike.

What stays here is what is genuinely this agent's:

* the ``LTW_``-prefixed environment variables and their defaults, and
* the mapping from the shared dataclasses onto this agent's pydantic models.

The public functions keep the signatures they always had — they still take the
``httpx.AsyncClient`` the pipeline builds — so no caller changed.
"""

from __future__ import annotations

import httpx
from common_lib.utils.logger import setup_logger

from src.config.settings import env_bool, env_int
from src.context import attachments as attachments_mod
from src.jira.transport import HttpxTransport
from src.models.schemas import AttachmentText, ConfluencePage
from src.shared.confluence_fetch import (
    DEFAULT_MAX_CHARS_PER_PAGE,
    DEFAULT_MAX_PAGES,
    FetchConfig,
    PageData,
)
from src.shared.confluence_fetch import fetch_linked_pages as _shared_fetch_linked_pages
from src.shared.confluence_fetch import fetch_page as _shared_fetch_page
from src.shared.confluence_fetch import fetch_page_attachments as _shared_fetch_page_attachments
from src.shared.confluence_fetch import fetch_remote_link_urls as _shared_fetch_remote_link_urls
from src.shared.confluence_text import (
    PageRef,
    _page_url,
    find_references,
    parse_reference,
    storage_to_text,
)

logger = setup_logger(__name__)

__all__ = [
    "DEFAULT_MAX_CHARS_PER_PAGE",
    "DEFAULT_MAX_PAGES",
    "PageRef",
    "_page_url",
    "enabled",
    "fetch_linked_pages",
    "fetch_page",
    "fetch_page_attachments",
    "fetch_remote_link_urls",
    "find_references",
    "max_chars",
    "max_pages",
    "parse_reference",
    "read_attachments",
    "storage_to_text",
]


def enabled() -> bool:
    """On by default: a linked spec is usually the requirement itself."""
    return env_bool("LTW_CONFLUENCE_ENABLED", default=True)


def max_pages() -> int:
    return max(1, env_int("LTW_CONFLUENCE_MAX_PAGES", DEFAULT_MAX_PAGES))


def max_chars() -> int:
    return max(500, env_int("LTW_CONFLUENCE_MAX_CHARS", DEFAULT_MAX_CHARS_PER_PAGE))


def read_attachments() -> bool:
    """On by default: the spec is often the spreadsheet hanging off the page.

    A linked page whose body says "see the attached sheet" contributes nothing
    without this, and the breakdown that follows looks like the model ignored
    the requirement rather than never having seen it.
    """
    return env_bool("LTW_CONFLUENCE_READ_ATTACHMENTS", default=True)


def _config() -> FetchConfig:
    """This deployment's caps, read fresh so a test can change them per case."""
    return FetchConfig(
        max_pages=max_pages(),
        max_chars=max_chars(),
        read_attachments=read_attachments(),
        max_files=attachments_mod.max_files(),
        attachment_limits=attachments_mod.limits(),
        images_off_hint=attachments_mod.IMAGES_OFF_HINT,
    )


def _base_url(client: httpx.AsyncClient) -> str:
    """The site this client is pointed at.

    The shared code builds absolute URLs, because the review agent's aiohttp
    session has no base URL of its own to fall back on.
    """
    return str(client.base_url).rstrip("/")


def _as_attachment(item: object) -> AttachmentText:
    return AttachmentText(
        filename=item.filename,
        mime_type=item.mime_type,
        size_bytes=item.size_bytes,
        text=item.text,
        note=item.note,
    )


def _as_page(page: PageData) -> ConfluencePage:
    return ConfluencePage(
        page_id=page.page_id,
        title=page.title,
        space_key=page.space_key,
        url=page.url,
        text=page.text,
        note=page.note,
        attachments=[_as_attachment(a) for a in page.attachments],
    )


async def fetch_page_attachments(
    client: httpx.AsyncClient,
    page_id: str,
) -> list[AttachmentText]:
    """Read the files attached to one Confluence page. Never raises."""
    results = await _shared_fetch_page_attachments(
        HttpxTransport(client), _base_url(client), page_id, _config()
    )
    return [_as_attachment(a) for a in results]


async def fetch_page(client: httpx.AsyncClient, ref: PageRef, base_url: str) -> ConfluencePage:
    """Read one page. Never raises: an unreadable page is reported, not fatal."""
    page = await _shared_fetch_page(HttpxTransport(client), ref, base_url, _config())
    return _as_page(page)


async def fetch_linked_pages(
    client: httpx.AsyncClient,
    texts: list[str],
    base_url: str,
    remote_urls: list[str] | None = None,
) -> tuple[list[ConfluencePage], list[str]]:
    """Read every Confluence page this ticket points at.

    The ``enabled()`` switch is checked here rather than in the shared code: it
    is this agent's environment variable, and the review agent has its own.
    """
    if not enabled():
        return [], []

    pages, notes = await _shared_fetch_linked_pages(
        HttpxTransport(client), texts, base_url, remote_urls, _config()
    )
    return [_as_page(p) for p in pages], notes


async def fetch_remote_link_urls(client: httpx.AsyncClient, issue_key: str) -> list[str]:
    """URLs from the issue's remote links, where Jira stores linked pages."""
    return await _shared_fetch_remote_link_urls(
        HttpxTransport(client), _base_url(client), issue_key
    )
