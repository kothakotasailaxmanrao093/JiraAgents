"""Image reading on a laptop run: OpenAI vision in place of the AI Gateway (2026-10-06).

Every image an agent reads — an attached screenshot, a file uploaded on the
form, an image on a Confluence page, a picture inside a Word, PDF, Excel or
PowerPoint file, a scanned PDF page — goes through ONE platform component:
``common_lib.embeddings.vision_processor.VisionProcessor.describe_image``. On
Aetherion it asks a vision model through the AI Gateway. A laptop has no
gateway, so every image came back empty ("No text content found").

``install()`` replaces that one method, in this worker process only, with the
same request sent straight to OpenAI: the platform's own instructions, the same
four-part answer (caption, text word for word, data, objects). It is called by
``scripts/local_worker.py`` — never by an agent — so the published agents do
not contain it and are unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import threading

import httpx

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o"
MODEL_ENV = "LOCAL_VISION_MODEL"
# Formats OpenAI accepts as they are; anything else (BMP, TIFF) becomes a PNG.
ACCEPTED = {"image/png", "image/jpeg", "image/gif", "image/webp"}

# Used only if the platform module ever stops carrying its own prompt.
FALLBACK_PROMPT = (
    "You are a meticulous document-vision analyst. Describe this image so it can "
    "be retrieved by semantic search. Produce these sections, omitting any that "
    "do not apply:\n"
    "- CAPTION: one sentence describing the image.\n"
    "- TEXT: verbatim OCR of all readable text, preserving order.\n"
    "- DATA: if it is a chart/table/diagram, describe axes, series, trends and "
    "transcribe key values as text.\n"
    "- OBJECTS: notable objects, people, scene or UI elements.\n"
    "Return plain text only. Do not invent content that is not present."
)

_SECTIONS = {
    "CAPTION": "caption",
    "TEXT": "ocr_text",
    "DATA": "structured_data",
    "OBJECTS": "objects",
}
_cache: dict[str, str] = {}
_lock = threading.Lock()


def as_accepted(image_bytes: bytes, mime_type: str = "") -> tuple[bytes, str]:
    """The image in a format OpenAI takes, and its real type.

    The type is read from the bytes, not trusted from the caller: the platform
    passes "image/png" by default whatever the file is.
    """
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        mime = Image.MIME.get(img.format or "", mime_type or "")
        if mime in ACCEPTED:
            return image_bytes, mime
        out = io.BytesIO()
        frame = img.convert("RGBA") if img.mode in ("P", "LA", "RGBA") else img.convert("RGB")
        frame.save(out, format="PNG")
        return out.getvalue(), "image/png"


def split_sections(text: str) -> dict[str, str]:
    """The model's "CAPTION: … TEXT: …" answer as the platform's fields."""
    fields: dict[str, str] = {}
    current = ""
    for line in text.splitlines():
        match = re.match(r"^\s*[-*]?\s*\**(CAPTION|TEXT|DATA|OBJECTS)\**\s*:\s*(.*)$", line)
        if match:
            current = _SECTIONS[match.group(1)]
            fields[current] = match.group(2).strip()
        elif current:
            fields[current] = f"{fields[current]}\n{line}".strip()
    return fields


def describe(
    image_bytes: bytes,
    mime_type: str = "",
    context: str | None = None,
    *,
    prompt: str = FALLBACK_PROMPT,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """The model's description of one image. Read once per image per worker."""
    key = hashlib.sha256(image_bytes).hexdigest() + (context or "")
    with _lock:
        if key in _cache:
            return _cache[key]
    data, mime = as_accepted(image_bytes, mime_type)
    instructions = prompt + (f"\n\nWhere the image appears: {context}" if context else "")
    body = {
        "model": os.environ.get(MODEL_ENV) or DEFAULT_MODEL,
        "max_completion_tokens": 2_000,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instructions},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{base64.b64encode(data).decode()}"
                        },
                    },
                ],
            }
        ],
    }
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}"}
    with httpx.Client(timeout=120, transport=transport) as client:
        resp = client.post(OPENAI_CHAT_URL, json=body, headers=headers)
        resp.raise_for_status()
    choices = resp.json().get("choices") or [{}]
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    with _lock:
        _cache[key] = text
    return text


def install(transport: httpx.BaseTransport | None = None) -> bool:
    """Route the platform's image reading to OpenAI in this process.

    True when installed; False (nothing changed) without an OpenAI key or when
    the platform library is not there.
    """
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return False
    try:
        from common_lib.embeddings import vision_processor
        from common_lib.models.embeddings import ImageDescription
    except ImportError:
        return False
    prompt = getattr(vision_processor, "_PROMPT", "") or FALLBACK_PROMPT

    def describe_image(self, image_bytes, mime_type="image/png", context=None, call_context=None):
        try:
            text = describe(image_bytes, mime_type, context, prompt=prompt, transport=transport)
        except Exception as exc:  # noqa: BLE001 — one unreadable image must not fail a run
            print(f"local vision: could not read an image: {exc}", flush=True)
            return ImageDescription()
        return ImageDescription(raw=text, **split_sections(text))

    vision_processor.VisionProcessor.describe_image = describe_image
    return True
