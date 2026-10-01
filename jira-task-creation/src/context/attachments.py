"""Text extraction for Jira attachments — this agent's caps over the shared engine.

The extraction itself (PDF, Word, Excel, PowerPoint, CSV, JSON, text, images via
the platform's vision model, the size caps and the middle-out truncation) now lives in
:mod:`src.shared.attachments`, so the review agent reads attachments through the
same tested code rather than growing a second copy of it.

What stays here is what is genuinely this agent's: the ``LTW_``-prefixed
environment variables, and the mapping onto this agent's ``AttachmentText``
model. The shared engine takes its caps as an argument precisely so it does not
have to know that those variable names exist.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from src.config.settings import env_bool, env_int
from src.models.schemas import AttachmentText
from src.shared.attachments import (
    DEFAULT_MAX_CHARS_PER_FILE,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_FILES,
    Limits,
    fit_to_budget,
)
from src.shared.attachments import extract as _shared_extract
from src.shared.attachments import supported as _shared_supported

# Named in the note a user reads when an image was skipped, so the message can
# tell them which variable to change without the shared engine knowing it.
IMAGES_OFF_HINT = " (set LTW_ATTACHMENT_READ_IMAGES=true to read it)"


def max_file_bytes() -> int:
    return env_int("LTW_ATTACHMENT_MAX_BYTES", DEFAULT_MAX_FILE_BYTES)


def max_files() -> int:
    return env_int("LTW_ATTACHMENT_MAX_FILES", DEFAULT_MAX_FILES)


def max_chars() -> int:
    return env_int("LTW_ATTACHMENT_MAX_CHARS", DEFAULT_MAX_CHARS_PER_FILE)


DEFAULT_READ_CONCURRENCY = 4


def read_concurrency() -> int:
    """How many attachments are downloaded and read at the same time.

    4, and at least 1. Jira Cloud rate-limits per account, and a burst of
    parallel downloads from one agent account is exactly what trips it; four
    covers the usual two to five files in one round while staying well inside
    the limit. Measured (BGV-69, 2026-09-29): a download takes 0.6-1.0 s, so
    serial reads of four files cost about 3 s and parallel ones about 1 s.
    """
    return max(1, env_int("LTW_READ_CONCURRENCY", DEFAULT_READ_CONCURRENCY))


def images_enabled() -> bool:
    """Read images through a vision model. On by default.

    A screenshot of a form, a photo of a whiteboard or a mockup is often the
    only place the requirement exists, and skipping it produced a thin
    breakdown that looked like the requirement's fault.

    It is not free: each image costs a vision call and adds seconds to the run,
    so a ticket with a dozen screenshots is noticeably slower. Set
    ``LTW_ATTACHMENT_READ_IMAGES=false`` to turn it off.
    """
    return env_bool("LTW_ATTACHMENT_READ_IMAGES", default=True)


def limits() -> Limits:
    """This deployment's caps, read fresh so a test can change them per case."""
    return Limits(
        max_file_bytes=max_file_bytes(),
        max_chars=max_chars(),
        read_images=images_enabled(),
    )


def supported(filename: str) -> bool:
    """True when this agent knows how to read the file type."""
    return _shared_supported(filename, read_images=images_enabled())


async def extract(data: bytes, filename: str, mime_type: str = "") -> AttachmentText:
    """Extract text from one attachment. Never raises."""
    result = await _shared_extract(
        data,
        filename,
        mime_type,
        limits=limits(),
        images_off_hint=IMAGES_OFF_HINT,
    )
    return AttachmentText(
        filename=result.filename,
        mime_type=result.mime_type,
        size_bytes=result.size_bytes,
        text=result.text,
        note=result.note,
    )


__all__ = [
    "DEFAULT_MAX_CHARS_PER_FILE",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_FILE_BYTES",
    "IMAGES_OFF_HINT",
    "extract",
    "fit_to_budget",
    "images_enabled",
    "limits",
    "max_chars",
    "max_file_bytes",
    "max_files",
    "supported",
]


async def read_all(
    items: list[dict[str, Any]],
    download: Callable[[str], Awaitable[bytes]],
) -> tuple[list[AttachmentText], list[str]]:
    """Read Jira's attachment list: capped, in parallel, never failing the run.

    Returns the extracted attachments **in Jira's order** — whatever order the
    downloads finish in — plus the notes for the reply. A file that cannot be
    downloaded or read becomes an attachment carrying a note, exactly as when
    they were read one at a time, so "what could not be read" is unchanged.
    """
    notes: list[str] = []
    limit = max_files()
    if len(items) > limit:
        notes.append(f"{len(items)} attachments found; only the first {limit} were read.")

    gate = asyncio.Semaphore(read_concurrency())

    async def read_one(item: dict[str, Any]) -> AttachmentText | None:
        filename = item.get("filename", "") or ""
        mime = item.get("mimeType", "") or ""
        url = item.get("content", "") or ""
        if not (filename and url):
            return None
        if not supported(filename):
            return await extract(b"", filename, mime)
        async with gate:
            try:
                data = await download(url)
            except httpx.HTTPError as exc:
                return AttachmentText(
                    filename=filename,
                    mime_type=mime,
                    note=f"Could not download this attachment: {exc}",
                )
            return await extract(data, filename, mime)

    wanted = items[:limit]
    read = await asyncio.gather(*(read_one(item) for item in wanted), return_exceptions=True)
    out: list[AttachmentText] = []
    for item, result in zip(wanted, read, strict=True):
        if isinstance(result, BaseException):
            # Anything unexpected is still one unreadable file, never a lost run.
            out.append(
                AttachmentText(
                    filename=item.get("filename", "") or "attachment",
                    mime_type=item.get("mimeType", "") or "",
                    note=f"Could not read this attachment: {result}",
                )
            )
        elif result is not None:
            out.append(result)
    return out, notes
