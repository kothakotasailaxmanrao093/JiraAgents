# GENERATED FILE — DO NOT EDIT.
# Mirrored from the canonical shared/ package by scripts/sync_shared.py.
# Edit shared/confluence_text.py at the repository root and re-run that script.
"""Confluence page references and storage-format text. Pure — no network.

The half of Confluence support that involves no HTTP: recognising which URLs in
a ticket point at a Confluence page, de-duplicating those references, and
turning Confluence's storage format (a bespoke XHTML dialect) into readable
text.

Teams write the real specification in Confluence and link it from the ticket.
Before this code existed the agent saw only the URL characters, built a thin
breakdown from almost nothing, and never said why — the worst kind of failure,
because it looks like success.

Only pages on the *same* Atlassian site are accepted. Anyone who can comment on
a ticket can put a URL in front of this code, so following arbitrary hosts would
turn an agent into a request proxy for whatever network it runs on. That guard
lives here, in the pure layer, so it cannot be skipped by a caller that builds
its own HTTP client.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlparse

# --------------------------------------------------------------------------
# Finding references
# --------------------------------------------------------------------------

# Trailing punctuation a person's sentence leaves stuck to a pasted URL.
_URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+")
_TRAILING = ".,;:!?)]}>\"'"

# /wiki/spaces/FL/pages/131181/Driver+Breaks  (and without the title slug)
_SPACE_PAGE_RE = re.compile(r"/wiki/spaces/([^/]+)/pages/(\d+)(?:/([^/?#]*))?", re.IGNORECASE)
# /wiki/display/FL/Driver+Breaks  (the older form, still pasted from bookmarks)
_DISPLAY_RE = re.compile(r"/wiki/display/([^/]+)/([^/?#]+)", re.IGNORECASE)
# /wiki/x/AbCdEf  (the "shortlink" from the Share dialog)
_TINY_RE = re.compile(r"/wiki/x/([A-Za-z0-9_-]+)", re.IGNORECASE)


class PageRef:
    """One way of naming a Confluence page, before it has been fetched."""

    def __init__(
        self,
        url: str,
        page_id: str = "",
        space_key: str = "",
        title: str = "",
        tiny: str = "",
    ) -> None:
        self.url = url
        self.page_id = page_id
        self.space_key = space_key
        self.title = title
        self.tiny = tiny

    @property
    def identity(self) -> str:
        """What makes two references the same page, for de-duplication."""
        if self.page_id:
            return f"id:{self.page_id}"
        if self.space_key and self.title:
            return f"title:{self.space_key.lower()}:{self.title.lower()}"
        return f"url:{self.url.lower()}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PageRef({self.identity})"


def _same_site(url: str, base_url: str) -> bool:
    """True when ``url`` is on the Atlassian site this agent is configured for."""
    try:
        host = (urlparse(url).hostname or "").lower()
        base = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and bool(base) and host == base


def _unslug(value: str) -> str:
    """``Driver+Breaks`` / ``Driver%20Breaks`` -> ``Driver Breaks``."""
    return unquote(value or "").replace("+", " ").strip()


def parse_reference(url: str, base_url: str) -> PageRef | None:
    """Turn one URL into a ``PageRef``, or ``None`` if it is not a Confluence page."""
    if not url or not _same_site(url, base_url):
        return None

    parsed = urlparse(url)
    path = parsed.path or ""

    match = _SPACE_PAGE_RE.search(path)
    if match:
        return PageRef(
            url=url,
            page_id=match.group(2),
            space_key=match.group(1),
            title=_unslug(match.group(3) or ""),
        )

    match = _DISPLAY_RE.search(path)
    if match:
        return PageRef(url=url, space_key=match.group(1), title=_unslug(match.group(2)))

    match = _TINY_RE.search(path)
    if match:
        return PageRef(url=url, tiny=match.group(1))

    # ...?pageId=131181 — the legacy viewpage.action form.
    page_id = (parse_qs(parsed.query or "").get("pageId") or [""])[0]
    if page_id.isdigit() and "/wiki/" in path.lower():
        return PageRef(url=url, page_id=page_id)

    return None


def find_references(texts: list[str], base_url: str) -> list[PageRef]:
    """Every distinct Confluence page referenced across ``texts``, in order."""
    refs: list[PageRef] = []
    seen: set[str] = set()
    for text in texts:
        for raw in _URL_RE.findall(text or ""):
            url = raw.rstrip(_TRAILING)
            ref = parse_reference(url, base_url)
            if ref and ref.identity not in seen:
                seen.add(ref.identity)
                refs.append(ref)
    return refs


def find_unread_links(texts: list[str], base_url: str) -> list[str]:
    """Every URL in ``texts`` that :func:`parse_reference` could not turn into a
    page — a different site, or a same-site URL this code does not recognise
    the shape of. ``find_references`` silently drops these, which is correct
    for *it* (it only promises Confluence pages) but wrong for a caller that
    needs to say a link was seen and not read. "Build what this spec says:
    <a Google Doc link>" must not read as a complete requirement once the URL
    is gone — the words behind it never arrived.
    """
    out: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for raw in _URL_RE.findall(text or ""):
            url = raw.rstrip(_TRAILING)
            if url in seen:
                continue
            if parse_reference(url, base_url) is None:
                seen.add(url)
                out.append(url)
    return out


# --------------------------------------------------------------------------
# Storage format -> readable text
# --------------------------------------------------------------------------

# Macro parameters are configuration, not content: <ac:parameter ac:name="id">
# carries a UUID no reader ever sees. The body of a macro is kept.
_MACRO_PARAM_RE = re.compile(r"<ac:parameter\b.*?</ac:parameter>", re.DOTALL | re.IGNORECASE)
_RESOURCE_RE = re.compile(r"<ri:[^>]*/?>", re.IGNORECASE)
_BLOCK_END_RE = re.compile(
    r"</(p|div|h[1-6]|li|tr|blockquote|ac:rich-text-body|ac:structured-macro)\s*>",
    re.IGNORECASE,
)
_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_CELL_RE = re.compile(r"</t[dh]\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def storage_to_text(body: str) -> str:
    """Flatten Confluence storage-format HTML into plain text.

    Structure the person relied on to mean something — paragraphs, list items,
    table cells — becomes whitespace that preserves the same reading order.
    """
    if not body:
        return ""
    text = _MACRO_PARAM_RE.sub(" ", body)
    text = _RESOURCE_RE.sub(" ", text)
    text = _BREAK_RE.sub("\n", text)
    text = _LIST_ITEM_RE.sub("\n- ", text)
    text = _CELL_RE.sub(" | ", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = text.replace("\u00a0", " ")  # non-breaking space
    # Collapse runs of spaces without destroying the line structure above.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _page_url(base_url: str, space_key: str, page_id: str) -> str:
    if space_key and page_id:
        return f"{base_url.rstrip('/')}/wiki/spaces/{space_key}/pages/{page_id}"
    return ""
