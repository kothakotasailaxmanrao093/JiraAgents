"""Text extraction for Jira attachments — this agent's caps over the shared engine.

The extraction itself (PDF, Word, Excel, PowerPoint, CSV, JSON, text, images via
OCR, the size caps and the middle-out truncation) now lives in
:mod:`src.shared.attachments`, so the review agent reads attachments through the
same tested code rather than growing a second copy of it.

What stays here is what is genuinely this agent's: the ``LTW_``-prefixed
environment variables, and the mapping onto this agent's ``AttachmentText``
model. The shared engine takes its caps as an argument precisely so it does not
have to know that those variable names exist.
"""

from __future__ import annotations

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
