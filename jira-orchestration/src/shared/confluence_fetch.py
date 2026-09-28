# GENERATED FILE — DO NOT EDIT.
# Mirrored from the canonical shared/ package by scripts/sync_shared.py.
# Edit shared/confluence_fetch.py at the repository root and re-run that script.
"""Reading Confluence pages that a Jira ticket points at.

Teams write the real specification in Confluence and link it from the ticket.
Without this, an agent sees only the URL characters, builds a thin breakdown
from almost nothing, and never says why — the worst kind of failure, because it
looks like success.

Two things make a page visible here:

* a Confluence URL pasted into the trigger comment or the description, and
* Jira's own "linked pages" panel, which lives on the ``remotelink`` endpoint
  rather than on the issue fields everything else is read from.

Only pages on the *same* Atlassian site are fetched — the guard lives in
:mod:`shared.confluence_text`, and every reference here has passed through it.
Anyone who can comment on a ticket can put a URL in front of this code, so
following arbitrary hosts would turn an agent into a request proxy for whatever
network it runs on.

**This module does its I/O through :class:`shared.transport.Transport`**, not
through ``httpx`` or ``aiohttp`` directly, so the two agents can share it while
each keeps the HTTP client it already has.

Nothing here raises. A page that cannot be read comes back with a ``note``
saying why, because "I could not read the spec" and "the spec says nothing" must
never look the same to the person reading the reply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from ._logging import get_logger
from .attachments import Extracted, Limits, fit_to_budget
from .attachments import extract as extract_attachment
from .attachments import supported as attachment_supported
from .confluence_text import PageRef, _page_url, find_references, parse_reference, storage_to_text
from .transport import Transport, TransportError

logger = get_logger(__name__)

DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_CHARS_PER_PAGE = 20_000

_EXPAND = "body.storage,space"


@dataclass(frozen=True)
class FetchConfig:
    """What one agent's deployment allows. Passed in, never read from env here.

    Each agent names its own variables (``LTW_CONFLUENCE_MAX_PAGES``,
    ``REVIEW_CONFLUENCE_MAX_PAGES``), so a shared module must not pick a winner.
    """

    max_pages: int = DEFAULT_MAX_PAGES
    max_chars: int = DEFAULT_MAX_CHARS_PER_PAGE
    read_attachments: bool = True
    max_files: int = 10
    attachment_limits: Limits = field(default_factory=Limits)
    images_off_hint: str = ""


@dataclass
class PageData:
    """One Confluence page and whatever could be read from it."""

    page_id: str = ""
    title: str = ""
    space_key: str = ""
    url: str = ""
    text: str = ""
    note: str = ""
    attachments: list[Extracted] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.text.strip())


def _api(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def _from_payload(payload: dict, base_url: str, fallback_url: str, cfg: FetchConfig) -> PageData:
    space_key = str((payload.get("space") or {}).get("key") or "")
    page_id = str(payload.get("id") or "")
    text = storage_to_text(((payload.get("body") or {}).get("storage") or {}).get("value") or "")

    text, note = fit_to_budget(text, cfg.max_chars)
    if not text:
        note = "This page has no readable text."

    return PageData(
        page_id=page_id,
        title=str(payload.get("title") or ""),
        space_key=space_key,
        url=_page_url(base_url, space_key, page_id) or fallback_url,
        text=text,
        note=note,
    )


def _unreadable(ref: PageRef, note: str) -> PageData:
    return PageData(
        page_id=ref.page_id,
        title=ref.title,
        space_key=ref.space_key,
        url=ref.url,
        text="",
        note=note,
    )


def _http_note(exc: TransportError) -> str:
    if exc.is_forbidden:
        return (
            "This account is not permitted to read that Confluence page, so "
            "its contents were not used."
        )
    if exc.is_not_found:
        return "That Confluence page was not found, so its contents were not used."
    if exc.status is not None:
        return f"That Confluence page could not be read (HTTP {exc.status})."
    return f"That Confluence page could not be read: {exc}"


async def fetch_page_attachments(
    transport: Transport, base_url: str, page_id: str, cfg: FetchConfig
) -> list[Extracted]:
    """Read the files attached to one Confluence page. Never raises.

    Uses the same extractors and the same size caps as Jira attachments, so a
    PDF behaves identically whether it hangs off the ticket or off the linked
    page. A file that cannot be read is reported, never dropped in silence.
    """
    if not page_id or not cfg.read_attachments:
        return []

    listing_url = _api(base_url, f"/wiki/rest/api/content/{page_id}/child/attachment")
    try:
        payload = await transport.get_json(listing_url, {"limit": cfg.max_files})
        results = (payload or {}).get("results") or []
    except TransportError as exc:
        logger.warning(f"Could not list attachments on Confluence page {page_id}: {exc}")
        return [
            Extracted(
                filename="",
                note=f"The files attached to this page could not be listed: {exc}",
            )
        ]

    out: list[Extracted] = []
    for item in results[: cfg.max_files]:
        title = str(item.get("title") or "")
        media_type = str((item.get("extensions") or {}).get("mediaType") or "")
        download = str((item.get("_links") or {}).get("download") or "")

        if not attachment_supported(title, read_images=cfg.attachment_limits.read_images):
            # Round-trips through the extractor so the "why" note is worded in
            # exactly one place.
            out.append(
                await extract_attachment(
                    b"",
                    title,
                    media_type,
                    limits=cfg.attachment_limits,
                    images_off_hint=cfg.images_off_hint,
                )
            )
            continue
        if not download:
            out.append(
                Extracted(
                    filename=title,
                    mime_type=media_type,
                    note="This attachment has no download link.",
                )
            )
            continue

        file_url = _api(base_url, f"/wiki{download}") if download.startswith("/") else download
        try:
            data = await transport.get_bytes(file_url)
        except TransportError as exc:
            out.append(
                Extracted(
                    filename=title,
                    mime_type=media_type,
                    note=f"This attachment could not be downloaded: {exc}",
                )
            )
            continue

        out.append(
            await extract_attachment(
                data,
                title,
                media_type,
                limits=cfg.attachment_limits,
                images_off_hint=cfg.images_off_hint,
            )
        )

    if out:
        read = sum(1 for a in out if a.text)
        logger.info(f"Confluence page {page_id}: read {read} of {len(out)} attachment(s)")
    return out


async def _resolve_tiny(transport: Transport, ref: PageRef, base_url: str) -> PageRef | None:
    """Follow a ``/wiki/x/...`` shortlink to the real page URL."""
    try:
        final = await transport.resolve_url(_api(base_url, urlparse(ref.url).path))
    except TransportError:
        return None
    resolved = parse_reference(final, base_url)
    if resolved and not resolved.tiny:
        resolved.url = ref.url
        return resolved
    return None


async def fetch_page(
    transport: Transport, ref: PageRef, base_url: str, cfg: FetchConfig | None = None
) -> PageData:
    """Read one page. Never raises: an unreadable page is reported, not fatal."""
    cfg = cfg or FetchConfig()
    try:
        if ref.tiny:
            resolved = await _resolve_tiny(transport, ref, base_url)
            if not resolved:
                return _unreadable(
                    ref, "That Confluence shortlink could not be resolved to a page."
                )
            ref = resolved

        if ref.page_id:
            payload = await transport.get_json(
                _api(base_url, f"/wiki/rest/api/content/{ref.page_id}"), {"expand": _EXPAND}
            )
            page = _from_payload(payload, base_url, ref.url, cfg)
            page.attachments = await fetch_page_attachments(transport, base_url, page.page_id, cfg)
            return page

        if ref.space_key and ref.title:
            payload = await transport.get_json(
                _api(base_url, "/wiki/rest/api/content"),
                {
                    "spaceKey": ref.space_key,
                    "title": ref.title,
                    "expand": _EXPAND,
                    "limit": 1,
                },
            )
            results = (payload or {}).get("results") or []
            if not results:
                return _unreadable(
                    ref,
                    f"No Confluence page titled '{ref.title}' was found in "
                    f"space {ref.space_key}.",
                )
            page = _from_payload(results[0], base_url, ref.url, cfg)
            page.attachments = await fetch_page_attachments(transport, base_url, page.page_id, cfg)
            return page

        return _unreadable(ref, "That Confluence link does not name a page.")
    except TransportError as exc:
        return _unreadable(ref, _http_note(exc))


async def fetch_linked_pages(
    transport: Transport,
    texts: list[str],
    base_url: str,
    remote_urls: list[str] | None = None,
    cfg: FetchConfig | None = None,
) -> tuple[list[PageData], list[str]]:
    """Read every Confluence page this ticket points at.

    ``texts`` are the comment and description bodies to scan; ``remote_urls``
    come from Jira's linked-pages panel. Returns ``(pages, notes)`` — notes
    describe anything skipped, so a thin breakdown is never silently blamed on
    the requirement.
    """
    cfg = cfg or FetchConfig()
    refs = find_references(list(texts) + list(remote_urls or []), base_url)
    if not refs:
        return [], []

    notes: list[str] = []
    limit = cfg.max_pages
    if len(refs) > limit:
        notes.append(f"{len(refs)} Confluence pages were linked; only the first {limit} were read.")
        refs = refs[:limit]

    pages: list[PageData] = []
    for ref in refs:
        page = await fetch_page(transport, ref, base_url, cfg)
        pages.append(page)
        if not page.usable and page.note:
            notes.append(f"{page.url or page.title or 'A linked Confluence page'}: {page.note}")
        # A spec that lives in an attachment and could not be read is exactly
        # the case where a thin breakdown gets blamed on the requirement.
        where = page.title or page.url or "a linked Confluence page"
        for att in page.attachments:
            if not att.text and att.note:
                name = att.filename or "An attached file"
                notes.append(f"{name} (attached to {where}): {att.note}")

    readable = sum(1 for p in pages if p.usable)
    logger.info(f"Confluence: {readable}/{len(pages)} linked page(s) read")
    return pages, notes


async def fetch_remote_link_urls(transport: Transport, base_url: str, issue_key: str) -> list[str]:
    """URLs from the issue's remote links, where Jira stores linked pages.

    A page attached through Jira's own "Link > Confluence page" control never
    appears in the issue fields, so nothing else can see it.

    Remote links are supporting context, so nothing here is allowed to fail the
    run: an unreadable or oddly shaped response yields no URLs rather than an
    exception. This endpoint is not part of the issue payload everything else
    comes from, so its shape is not guaranteed by anything already validated.
    """
    try:
        payload = await transport.get_json(
            _api(base_url, f"/rest/api/3/issue/{issue_key}/remotelink")
        )
    except Exception as exc:  # noqa: BLE001 — supporting context must never fail a run
        logger.warning(f"Could not read remote links for {issue_key}: {exc}")
        return []

    urls: list[str] = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        obj = item.get("object")
        if not isinstance(obj, dict):
            continue
        url = str(obj.get("url") or "").strip()
        if url:
            urls.append(url)
    return urls
