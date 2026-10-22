# GENERATED FILE — DO NOT EDIT.
# Mirrored from the canonical shared/ package by scripts/sync_shared.py.
# Edit shared/video_understanding.py at the repository root and re-run that script.
"""Understand a screen recording: what each screen shows, and what happens.

A screen recording attached to a Jira ticket is usually someone showing a flow
or a fault, often with no voice: open a page, fill a form, press a button, see
an error. Reading each frame on its own (the platform's default image reader
copies the text on screen) gives a list of screens, not what happened. So:

1. every chosen frame is read by the vision model with a prompt about *screens*:
   which page, what the user is doing, the values shown, any error;
2. one more model call reads those descriptions in order, with the ticket's
   subject as context, and explains the video: what it shows, step by step with
   timestamps, the problems visible, and what the screens cannot tell.

Both steps degrade, never fail: a frame the vision model cannot read falls back
to the platform's own image reader, and if the explanation call fails the
screen-by-screen descriptions are used as they are. The caller's run never
fails because of a video.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable

from ._logging import get_logger
from .video import VideoExtraction, VideoFrame

logger = get_logger(__name__)

FRAME_CONCURRENCY = 4
CALL_TIMEOUT_SECONDS = 60
NARRATION_MAX_TOKENS = 1500

SCREEN_PROMPT = (
    "This is one frame, at {at}, from a screen recording attached to a Jira ticket"
    "{context}. Describe it for a software team, in plain text, at most 120 words:\n"
    "- the application and the page or screen shown;\n"
    "- what the user appears to be doing: the field in focus, the button or link "
    "under the cursor or just clicked, an open menu or dialog;\n"
    "- the data on screen: form values, amounts, statuses, table rows that matter;\n"
    "- any error, warning, empty state or value that looks wrong — copy its exact text.\n"
    "Describe only what is visible. Do not guess what happens off screen."
)

NARRATION_SYSTEM_PROMPT = (
    "You explain screen recordings to software teams. You are given descriptions of "
    "the frames of one recording, in time order, each with its timestamp. Work out "
    "what the person did and what the system showed, and explain it clearly. Use only "
    "what the descriptions show; when you infer something (for example that a button "
    "was clicked because the next screen appeared), say 'appears to'. Never invent "
    "values, messages or screens."
)

NARRATION_PROMPT = """Recording: {name}, {length} long{audio}.
Ticket: {context}

Frames, in order:
{frames}

Write plain text with exactly these four headings:
WHAT THIS VIDEO SHOWS: 1-3 sentences.
STEP BY STEP: numbered steps, each starting with its timestamp, saying what the person \
did and what the system showed.
PROBLEMS SEEN: each error, wrong value or unexpected result, with its timestamp and \
exact text; or "None visible".
NOT CLEAR FROM THE SCREENS: what a viewer still cannot tell (for example what was \
said, or data not shown)."""


def provider_for(model: str) -> str:
    """The gateway provider for a model name."""
    name = (model or "").lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("gemini"):
        return "google"
    return "openai"


def clock(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


async def _gateway_describe(frame: VideoFrame, prompt: str, model: str) -> str:
    from agent_lib.gateway.ai import ai_gateway

    return await asyncio.wait_for(
        ai_gateway.describe_image(
            provider=provider_for(model),
            model_name=model,
            prompt=prompt,
            image_b64=base64.b64encode(frame.png_bytes).decode("ascii"),
            media_type="image/png",
        ),
        timeout=CALL_TIMEOUT_SECONDS,
    )


async def _platform_describe(frame: VideoFrame) -> str:
    from agent_lib.utils import file_utils

    return await asyncio.to_thread(file_utils.extract_text_from_image, frame.png_bytes)


async def describe_frames(
    frames: list[VideoFrame],
    *,
    model: str,
    context: str = "",
    describe: Callable | None = None,
) -> list[str]:
    """One description per frame, in order. Never raises.

    ``describe`` replaces the vision call in tests.
    """
    gate = asyncio.Semaphore(FRAME_CONCURRENCY)
    about = f" about: {context}" if context else ""

    async def one(frame: VideoFrame) -> str:
        prompt = SCREEN_PROMPT.format(at=clock(frame.timestamp_seconds), context=about)
        async with gate:
            try:
                if describe is not None:
                    text = await describe(frame, prompt)
                else:
                    text = await _gateway_describe(frame, prompt, model)
            except Exception as exc:  # noqa: BLE001 — fall back to the platform reader
                logger.info(f"Screen reading fell back to the platform reader: {exc}")
                try:
                    text = await _platform_describe(frame)
                except Exception as inner:  # noqa: BLE001 — one frame never stops the rest
                    text = f"(this frame could not be read: {inner})"
        return str(text or "").strip() or "(no readable content)"

    return list(await asyncio.gather(*(one(f) for f in frames)))


async def _gateway_chat(prompt: str, model: str) -> str:
    from . import llm

    if llm.direct_openai():  # a laptop run: no gateway there
        return await llm.openai_chat(
            provider=provider_for(model),
            model=model,
            prompt=prompt,
            system_prompt=NARRATION_SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=NARRATION_MAX_TOKENS,
        )
    from agent_lib.gateway.ai import ai_gateway

    reply = await asyncio.wait_for(
        ai_gateway.chat(
            provider=provider_for(model),
            model_name=model,
            prompt=prompt,
            system_prompt=NARRATION_SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=NARRATION_MAX_TOKENS,
        ),
        timeout=CALL_TIMEOUT_SECONDS,
    )
    return reply.get("content", "") if isinstance(reply, dict) else str(reply)


async def narrate(
    filename: str,
    extraction: VideoExtraction,
    descriptions: list[str],
    *,
    model: str,
    context: str = "",
    chat: Callable | None = None,
) -> str:
    """The explanation of the whole video, or "" when it could not be written."""
    timeline = "\n".join(
        f"[{clock(f.timestamp_seconds)}] {d}"
        for f, d in zip(extraction.frames, descriptions, strict=True)
    )
    prompt = NARRATION_PROMPT.format(
        name=filename,
        length=clock(extraction.duration_seconds or 0),
        audio=", with spoken audio that was not transcribed" if extraction.audio_present else "",
        context=context or "(not given)",
        frames=timeline,
    )
    try:
        text = await (chat(prompt) if chat is not None else _gateway_chat(prompt, model))
    except Exception as exc:  # noqa: BLE001 — the screens alone are still worth having
        logger.warning(f"Could not explain the video {filename!r}: {exc}")
        return ""
    return str(text or "").strip()


async def understand_video(
    filename: str,
    extraction: VideoExtraction,
    *,
    model: str,
    context: str = "",
    describe: Callable | None = None,
    chat: Callable | None = None,
) -> str:
    """The text a reader of the ticket gets for one video. "" when no frames."""
    if not extraction.frames:
        return ""

    descriptions = await describe_frames(
        extraction.frames, model=model, context=context, describe=describe
    )
    explained = await narrate(
        filename, extraction, descriptions, model=model, context=context, chat=chat
    )

    length = clock(extraction.duration_seconds or 0)
    parts = [f"[Video, {length} long, {len(extraction.frames)} screen(s) read]"]
    if explained:
        parts.append(explained)
    parts.append("SCREEN BY SCREEN:")
    parts.extend(
        f"[{clock(f.timestamp_seconds)}] {d}"
        for f, d in zip(extraction.frames, descriptions, strict=True)
    )
    if extraction.audio_present:
        parts.append(
            "[This video also has spoken audio. Speech-to-text is not available on the "
            "platform yet, so it was understood from the screens only — attach the meeting "
            "transcript (.vtt or .docx) for what was said.]"
        )
    return "\n".join(parts)
