"""Text extraction for Jira and Confluence attachments. Pure — no network.

Reuses the platform's own extractors (``agent_lib.utils.file_utils``) rather
than adding parsing dependencies. Extraction is best-effort by design: a
requirement is still readable without its screenshot, so a file that cannot be
parsed is reported as a note on the attachment and never fails the run.

Caps exist because an attachment feeds straight into a model prompt: a 40 MB
design PDF would blow the context window and the bill alongside it.

**This module does no I/O of its own.** It takes bytes that the caller has
already downloaded, which is what lets two agents on different HTTP stacks
share it unchanged. The caps arrive as an explicit :class:`Limits` rather than
being read from the environment here, because each agent names its own
variables (``LTW_ATTACHMENT_MAX_BYTES``, ``REVIEW_ATTACHMENT_MAX_BYTES``) and a
shared module must not pick a winner.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from ._logging import get_logger
from .transcripts import TRANSCRIPT_SUFFIXES, clean_transcript

logger = get_logger(__name__)

DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB per attachment
DEFAULT_MAX_FILES = 10
DEFAULT_MAX_CHARS_PER_FILE = 20_000

# Extension -> the file_utils extractor that handles it. Images go through the
# vision path, which needs the AI Gateway; everything else is local parsing.
_TEXT_LIKE = {".txt", ".md", ".log", ".rst", ".text"}
# JSON is read as text, then pretty-printed so a one-line export does not
# reach the model as a single unreadable 20,000-character string.
_JSON_LIKE = {".json", ".jsonl", ".ndjson"}
# Meeting transcripts: text, with the cue numbers and timings reduced to
# "[00:01:12] Priya: …" so the model sees who said what, and when.
_TRANSCRIPT_LIKE = set(TRANSCRIPT_SUFFIXES)
_IMAGE_LIKE = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}

_DOCUMENT_LIKE = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".csv"}


@dataclass(frozen=True)
class Limits:
    """The caps one agent applies to one attachment.

    Passed in explicitly so this module stays free of any agent's environment
    variable names. :func:`default_limits` gives the values both agents use
    today, so a caller only overrides what it actually wants to change.
    """

    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_chars: int = DEFAULT_MAX_CHARS_PER_FILE
    read_images: bool = True


def default_limits() -> Limits:
    return Limits()


@dataclass
class Extracted:
    """One attachment and whatever text could be extracted from it.

    ``text`` is empty when extraction was skipped or failed; ``note`` then says
    why. A failed attachment never fails the run — a requirement is still
    readable without its screenshot.

    A plain dataclass rather than a pydantic model, so the shared package does
    not force a validation library on either agent. Each agent maps this onto
    whatever model its own pipeline already speaks.
    """

    filename: str
    mime_type: str = ""
    size_bytes: int = 0
    text: str = ""
    note: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.text.strip())


def suffix(filename: str) -> str:
    """Lowercased extension including the dot, or "" when there is none."""
    name = (filename or "").strip().lower()
    dot = name.rfind(".")
    return name[dot:] if dot > 0 else ""


def _extract_json(data: bytes) -> str:
    """Readable text from a JSON or JSON-lines export.

    A requirement exported from another tool is usually one long line. Re-dumped
    with indentation it reads like a document; left alone it reaches the model
    as a single unbroken string and the structure is lost.

    A file where nothing parses is a corrupt attachment, and raising here is what
    puts "could not read this" in front of the person instead of feeding them a
    truncated fragment that looks like content.
    """
    raw = data.decode("utf-8", errors="replace").strip()
    if not raw:
        return ""
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except json.JSONDecodeError:
        pass

    # JSON lines: one object per line, so parse them individually. A partly
    # readable export is still worth having; a wholly unreadable one is not.
    rendered: list[str] = []
    parsed_any = False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rendered.append(json.dumps(json.loads(line), indent=2, ensure_ascii=False))
            parsed_any = True
        except json.JSONDecodeError:
            rendered.append(line)
    if not parsed_any:
        raise ValueError("the file is not valid JSON")
    return "\n\n".join(rendered)


def fit_to_budget(text: str, limit: int) -> tuple[str, str]:
    """Reduce ``text`` to ``limit`` characters, keeping both ends.

    Returns ``(text, note)``; ``note`` is empty when nothing was dropped.

    Plain truncation keeps the opening and throws away everything after it,
    which is the worst possible half of a requirements document: the scope,
    the limits and the acceptance criteria are almost always at the end. A
    40-page PDF became eight pages of preamble and nothing else.

    So the start and the end are both kept, split on blank lines so no
    paragraph is cut mid-sentence, and the gap is stated in the text itself —
    the model can see that something is missing rather than reading a truncated
    document as a complete one.
    """
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text, ""

    marker_template = "\n\n[... {n} characters omitted from the middle of this document ...]\n\n"
    reserve = len(marker_template.format(n=len(text)))
    usable = max(limit - reserve, 0)
    if usable < 200:
        # Too small to be worth splitting; keep the opening and say so.
        return text[:limit], f"Truncated to the first {limit} characters."

    # Two thirds from the front — that is where the request itself lives — and
    # one third from the back, where the constraints usually are.
    head_budget = (usable * 2) // 3
    tail_budget = usable - head_budget

    paragraphs = text.split("\n\n")

    head: list[str] = []
    used = 0
    for para in paragraphs:
        if used + len(para) > head_budget:
            break
        head.append(para)
        used += len(para) + 2

    tail: list[str] = []
    used = 0
    for para in reversed(paragraphs[len(head) :]):
        if used + len(para) > tail_budget:
            break
        tail.insert(0, para)
        used += len(para) + 2

    if not head and not tail:
        return text[:limit], f"Truncated to the first {limit} characters."

    kept = len("\n\n".join(head)) + len("\n\n".join(tail))
    omitted = len(text) - kept
    joined = "\n\n".join(head) + marker_template.format(n=omitted) + "\n\n".join(tail)
    note = (
        f"This document is {len(text)} characters. The opening and the closing "
        f"sections were kept and {omitted} characters in the middle were left out."
    )
    return joined, note


def _extract_sync(data: bytes, filename: str) -> str:
    """Dispatch to the platform extractor for this file type.

    Deliberately uses the public per-type helpers rather than the private
    dispatcher in ``file_utils``, so an internal rename there cannot silently
    break attachment reading.
    """
    from agent_lib.utils import file_utils

    ext = suffix(filename)
    if ext in _TEXT_LIKE:
        return data.decode("utf-8", errors="replace")
    if ext in _TRANSCRIPT_LIKE:
        return clean_transcript(data.decode("utf-8-sig", errors="replace"))
    if ext in _JSON_LIKE:
        return _extract_json(data)
    if ext == ".pdf":
        return file_utils.extract_text_from_pdf(data)
    if ext == ".docx":
        return file_utils.extract_text_from_docx(data)
    if ext == ".doc":
        return file_utils.extract_text_from_doc(data)
    if ext == ".xlsx":
        return file_utils.extract_text_from_xlsx(data)
    if ext == ".xls":
        return file_utils.extract_text_from_xls(data)
    if ext == ".pptx":
        return file_utils.extract_text_from_pptx(data)
    if ext == ".csv":
        return file_utils.extract_text_from_csv(data)
    if ext in _IMAGE_LIKE:
        return file_utils.extract_text_from_image(data)
    raise ValueError(f"unsupported attachment type '{ext or filename}'")


def supported(filename: str, *, read_images: bool = True) -> bool:
    """True when this file type can be read at all.

    ``read_images`` is explicit because reading an image costs a vision call;
    an agent that has switched that off must report the image as skipped rather
    than pretend the type is unknown.
    """
    ext = suffix(filename)
    if ext in _IMAGE_LIKE:
        return read_images
    return ext in _TEXT_LIKE | _JSON_LIKE | _TRANSCRIPT_LIKE | _DOCUMENT_LIKE


async def extract(
    data: bytes,
    filename: str,
    mime_type: str = "",
    *,
    limits: Limits | None = None,
    images_off_hint: str = "",
) -> Extracted:
    """Extract text from one already-downloaded attachment. Never raises.

    Extraction is CPU-bound and synchronous, so it runs in a worker thread to
    keep the caller's event loop free for the other downloads.

    ``images_off_hint`` lets an agent name its own environment variable in the
    note a user reads, without this module knowing that variable exists.
    """
    limits = limits or default_limits()
    info = Extracted(filename=filename, mime_type=mime_type, size_bytes=len(data))

    if not supported(filename, read_images=limits.read_images):
        ext = suffix(filename)
        if ext in _IMAGE_LIKE:
            info.note = f"Image attachment skipped{images_off_hint}."
        else:
            info.note = f"Unsupported attachment type '{ext or 'unknown'}'; not read."
        return info

    if len(data) > limits.max_file_bytes:
        info.note = (
            f"Attachment is {len(data) // 1024} KB, over the "
            f"{limits.max_file_bytes // 1024} KB limit; not read."
        )
        return info

    try:
        text = await asyncio.to_thread(_extract_sync, data, filename)
    except Exception as exc:  # noqa: BLE001 — one bad file must not fail the run
        logger.warning(f"Could not extract text from {filename!r}: {exc}")
        info.note = f"Could not read this attachment: {exc}"
        return info

    text = (text or "").strip()
    if not text:
        info.note = "No text content found in this attachment."
        return info

    text, note = fit_to_budget(text, limits.max_chars)
    if note:
        info.note = note
    info.text = text
    logger.info(f"Extracted {len(text)} chars from attachment {filename!r}")
    return info
