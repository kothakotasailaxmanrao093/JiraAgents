"""Context sources for the files a requirement actually lives in.

Two sources, both reading through the shared extraction and Confluence code the
sibling agent already had in production:

* :class:`AttachmentsSource`  — files attached to the Jira issue.
* :class:`ConfluenceSource`   — pages the issue links to, and their attachments.

Why these matter more than their position in the registry suggests: a ticket
whose description says "see the attached spec" or "as per the Confluence page"
contributes almost nothing on its own. Reviewing it without opening the file
produces a long list of "missing detail" findings that are really findings about
the reviewer, and the person reading them cannot tell the difference.

Neither source is allowed to fail a run. A file that will not open, or a page
this account may not read, is recorded as a warning naming the file or page, so
the review can say so out loud rather than quietly reviewing less.
"""

from __future__ import annotations

import logging

from config import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_CHARS,
    MAX_ATTACHMENTS,
    MAX_CONFLUENCE_CHARS,
    MAX_CONFLUENCE_PAGES,
    READ_ATTACHMENT_IMAGES,
)
from shared.attachments import Limits
from shared.attachments import extract as extract_attachment
from shared.confluence_fetch import FetchConfig, fetch_linked_pages, fetch_remote_link_urls
from shared.transport import TransportError

from .base import ContextDocument, ContextSource, FetchContext

logger = logging.getLogger(__name__)

IMAGES_OFF_HINT = " (set READ_ATTACHMENT_IMAGES to read it)"


def attachment_limits() -> Limits:
    return Limits(
        max_file_bytes=MAX_ATTACHMENT_BYTES,
        max_chars=MAX_ATTACHMENT_CHARS,
        read_images=READ_ATTACHMENT_IMAGES,
    )


def confluence_config() -> FetchConfig:
    return FetchConfig(
        max_pages=MAX_CONFLUENCE_PAGES,
        max_chars=MAX_CONFLUENCE_CHARS,
        read_attachments=True,
        max_files=MAX_ATTACHMENTS,
        attachment_limits=attachment_limits(),
        images_off_hint=IMAGES_OFF_HINT,
    )


class AttachmentsSource(ContextSource):
    """Text extracted from the files attached to the target issue."""

    source_id = "attachments"

    def is_enabled(self, ctx: FetchContext) -> bool:
        return ctx.include_attachments and bool((ctx.target_bundle or {}).get("attachments"))

    async def load(self, ctx: FetchContext) -> list[ContextDocument]:
        bundle = ctx.target_bundle or {}
        key = bundle.get("key", ctx.issue_key)
        attachments = bundle.get("attachments") or []

        total = bundle.get("attachments_total", len(attachments))
        if total > len(attachments):
            ctx.warnings.append(
                f"Only the first {len(attachments)} of {total} attachments on {key} "
                "were read (payload cap)."
            )

        transport = ctx.jira.transport
        limits = attachment_limits()
        docs: list[ContextDocument] = []

        for item in attachments:
            filename = item.get("filename") or "(unnamed file)"
            url = item.get("content_url") or ""
            if not url:
                ctx.warnings.append(
                    f"{filename} (attached to {key}) has no download link, so it was not read."
                )
                continue

            try:
                data = await transport.get_bytes(url)
            except TransportError as exc:
                ctx.warnings.append(
                    f"{filename} (attached to {key}) could not be downloaded: {exc}"
                )
                logger.warning("Attachment %s download failed: %s", filename, exc)
                continue

            extracted = await extract_attachment(
                data,
                filename,
                item.get("mime_type") or "",
                limits=limits,
                images_off_hint=IMAGES_OFF_HINT,
            )
            if not extracted.usable:
                # Named, never silent: "the spec could not be opened" and "the
                # spec says nothing" must not look the same to the reader.
                ctx.warnings.append(
                    f"{filename} (attached to {key}) could not be read: "
                    f"{extracted.note or 'no text content'}"
                )
                continue

            docs.append(
                ContextDocument(
                    source_id=f"{key}:attachment:{filename}",
                    source_label=f"Attachment {filename} (on {key})",
                    kind="attachment",
                    text=extracted.text,
                    metadata={
                        "issue_key": key,
                        "filename": filename,
                        "mime_type": extracted.mime_type,
                        "size_bytes": extracted.size_bytes,
                        "note": extracted.note,
                    },
                )
            )

        logger.info("Attachments on %s: read %d of %d", key, len(docs), len(attachments))
        return docs


class ConfluenceSource(ContextSource):
    """Text from the Confluence pages this issue points at, and their files.

    References are found in the description and comments, and in Jira's own
    "linked pages" panel — which lives on the ``remotelink`` endpoint and so is
    invisible to everything that reads the issue fields.
    """

    source_id = "confluence"

    def is_enabled(self, ctx: FetchContext) -> bool:
        return ctx.include_confluence and bool(ctx.target_bundle)

    async def load(self, ctx: FetchContext) -> list[ContextDocument]:
        bundle = ctx.target_bundle or {}
        key = bundle.get("key", ctx.issue_key)
        transport = ctx.jira.transport
        base_url = ctx.jira.base

        # Everything a person could have pasted a link into.
        texts = [bundle.get("description_text") or ""]
        texts += [c.get("body") or "" for c in bundle.get("comments") or []]

        remote_urls = await fetch_remote_link_urls(transport, base_url, key)

        pages, notes = await fetch_linked_pages(
            transport, texts, base_url, remote_urls, confluence_config()
        )
        # Every note names a page or a file; they are what stops a thin review
        # being mistaken for a thin ticket.
        ctx.warnings.extend(notes)

        docs: list[ContextDocument] = []
        for page in pages:
            title = page.title or page.url or "Untitled page"
            if page.usable:
                docs.append(
                    ContextDocument(
                        source_id=f"confluence:{page.page_id or page.url}",
                        source_label=f'Confluence: "{title}"',
                        kind="confluence",
                        text=page.text,
                        metadata={
                            "issue_key": key,
                            "page_id": page.page_id,
                            "title": title,
                            "url": page.url,
                            "note": page.note,
                        },
                    )
                )
            for att in page.attachments:
                if not att.usable:
                    continue
                docs.append(
                    ContextDocument(
                        source_id=f"confluence:{page.page_id}:{att.filename}",
                        source_label=f'Attachment {att.filename} (on Confluence "{title}")',
                        kind="attachment",
                        text=att.text,
                        metadata={
                            "issue_key": key,
                            "filename": att.filename,
                            "page_title": title,
                            "note": att.note,
                        },
                    )
                )

        logger.info("Confluence for %s: %d document(s) from %d page(s)", key, len(docs), len(pages))
        return docs
