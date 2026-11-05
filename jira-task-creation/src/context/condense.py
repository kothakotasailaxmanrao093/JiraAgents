"""Make a requirement of any length fit the model — by condensing, never refusing.

KS-17185 (2026-10-06) was a two-line ticket and an 18,000-character meeting
transcript: the card came to 23,747 characters and the agent asked the person
to summarise it. A transcript can be an hour long and a spec forty pages, and
both are exactly where the requirement lives — so a long card is made to fit:

1. Within the budget (``TASK_CONTEXT_MAX_CHARS``): sent as it is.
2. Over it: the context-only sections that say least (work logs, field
   history) are left out first.
3. Still over: the largest supporting sections — attachments, transcripts,
   Confluence pages, the discussion — are condensed by the model into the
   points a team builds from (decisions, rules, numbers, names, what was ruled
   out, open questions), part by part, keeping who said it and when.
4. The stated request itself is condensed only if it alone is too big.
5. Every step is written to ``notes``, so the reply and the PDF say what was
   read in full and what was condensed — nothing is dropped silently.

Pure apart from ``chat``, which is passed in: the tests give a fake one.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from common_lib.utils.logger import setup_logger

logger = setup_logger(__name__)

Chat = Callable[[str, str], Awaitable[str]]  # (prompt, system_prompt) -> reply

# What the model is given at most. gpt-5.1 takes far more, but a breakdown
# written from a focused 60,000 characters is better than one from 400,000.
DEFAULT_BUDGET_CHARS = 60_000
BUDGET_ENV = "TASK_CONTEXT_MAX_CHARS"
MIN_BUDGET_CHARS = 1_000
# One model call reads at most this much; long material is read in parts.
CHUNK_CHARS = 12_000
# Parts read at the same time.
CONCURRENCY = 4
# A condensed section is never squeezed below this — it would lose the detail
# it was condensed to keep.
MIN_SECTION_CHARS = 1_500
# Condensing the condensed notes again, when one pass is not enough.
MAX_ROUNDS = 3

# Left out before anything is condensed: context only, rarely a requirement.
_DROP_FIRST = ("Work logged so far", "Recent field history")
# Condensed, largest first. The stated request is never in this list.
_CONDENSABLE = (
    "Attached documents",
    "Uploaded on the Aetherion form",
    "Linked Confluence pages",
    "Discussion on the ticket",
    "The ticket this was asked on",
    "Linked work items",
)
_STATED = "Stated requirement"

SYSTEM_PROMPT = """\
You condense source material for a software team that will turn it into Jira
work items. You never invent anything: every point you write is stated in the
material. You keep exact values — numbers, limits, dates, names of screens,
fields, roles, products, clients and statuses — word for word."""

_PROMPT = """\
Below is part {part} of {parts} of "{label}", supporting material for this work
request:
{request}

Write the points a team needs to build what is asked, from THIS part only:
- decisions made, and who made them
- rules, behaviours and edge cases
- numbers, limits, formats, names of screens, fields, roles and statuses
- what was ruled out or deferred
- open questions left unanswered
For a meeting transcript, start each point with the speaker and time, e.g.
"[00:12:40 Tyler] Bulk print uses the latest full report PDF only."
Leave out greetings, small talk, screen-sharing chatter and repetition.
Write plain bullet points, at most about {words} words. If this part holds
nothing relevant, write "- (nothing relevant in this part)".

PART {part} OF {parts}:
{text}
"""


def budget_chars() -> int:
    """``TASK_CONTEXT_MAX_CHARS``, or 60,000; never below 1,000."""
    raw = os.environ.get(BUDGET_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_BUDGET_CHARS
    except ValueError:
        logger.warning(f"{BUDGET_ENV} is not an integer ({raw!r}); using {DEFAULT_BUDGET_CHARS}")
        value = DEFAULT_BUDGET_CHARS
    return max(MIN_BUDGET_CHARS, value)


@dataclass
class Section:
    """One "## Heading" block of the assembled requirement (or the lead text)."""

    heading: str  # "" for text before the first heading (a typed requirement)
    body: str

    def render(self) -> str:
        return f"## {self.heading}\n{self.body}".strip() if self.heading else self.body.strip()

    def kind(self, prefixes: tuple[str, ...]) -> bool:
        return self.heading.startswith(prefixes)


def split_sections(text: str) -> list[Section]:
    """The requirement as sections, in order; unsectioned text is one section."""
    lead, *blocks = re.split(r"(?m)^## ", text or "")
    sections = [Section("", lead)] if lead.strip() else []
    for block in blocks:
        heading, _, body = block.partition("\n")
        sections.append(Section(heading.strip(), body))
    return sections


def join_sections(sections: list[Section]) -> str:
    return "\n\n".join(s.render() for s in sections if s.render())


def chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split at line breaks into parts of at most ``size`` characters.

    A transcript line ("[00:12:40] Tyler: …") is never cut in half; a single
    line longer than ``size`` is split where it must be.
    """
    parts: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.splitlines(keepends=True):
        while len(line) > size:  # one enormous line: split it
            if current:
                parts.append("".join(current))
                current, length = [], 0
            parts.append(line[:size])
            line = line[size:]
        if length + len(line) > size and current:
            parts.append("".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line)
    if current:
        parts.append("".join(current))
    return [p for p in parts if p.strip()]


async def condense_text(
    text: str, *, label: str, request: str, target: int, chat: Chat, round_: int = 1
) -> str:
    """``text`` reduced to about ``target`` characters of points, by the model."""
    parts = chunk(text)
    # ~6 characters a word; each part gets its share of the target.
    words = max(60, target // len(parts) // 6)
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(index: int, part: str) -> str:
        prompt = _PROMPT.format(
            part=index,
            parts=len(parts),
            label=label,
            request=request[:1_500] or "(see the material)",
            words=words,
            text=part,
        )
        async with gate:
            return (await chat(prompt, SYSTEM_PROMPT)).strip()

    notes = await asyncio.gather(*(one(i, p) for i, p in enumerate(parts, 1)))
    joined = "\n".join(n for n in notes if n and "(nothing relevant in this part)" not in n)
    if len(joined) > target and round_ < MAX_ROUNDS and len(joined) < len(text):
        # The notes themselves are still too long: condense them once more.
        return await condense_text(
            joined, label=label, request=request, target=target, chat=chat, round_=round_ + 1
        )
    return joined


async def fit_requirement(
    text: str, *, chat: Chat, budget: int | None = None
) -> tuple[str, list[str]]:
    """Return ``(text that fits, notes on what was done)``.

    Raises nothing for a model failure: a section that could not be condensed
    is cut to its share instead, and the note says so.
    """
    limit = budget or budget_chars()
    if len(text) <= limit:
        return text, []

    original = len(text)
    sections = split_sections(text)
    notes: list[str] = []

    def total() -> int:
        return len(join_sections(sections))

    # 1. Context that rarely states a requirement goes first.
    for prefix in _DROP_FIRST:
        if total() <= limit:
            break
        before = len(sections)
        sections = [s for s in sections if not s.kind((prefix,))]
        if len(sections) < before:
            notes.append(f'"{prefix}" was left out to fit the model.')

    request = "\n".join(s.body for s in sections if s.kind((_STATED,)) or not s.heading).strip()

    # 2. The largest supporting section first, then the next, until it fits;
    #    the stated request last of all, only when it alone is too big.
    candidates = sorted(
        (s for s in sections if s.kind(_CONDENSABLE)), key=lambda s: len(s.body), reverse=True
    ) + [s for s in sections if s.kind((_STATED,)) or not s.heading]
    for section in candidates:
        excess = total() - limit
        if excess <= 0:
            break
        target = max(MIN_SECTION_CHARS, len(section.body) - excess)
        if target >= len(section.body):
            continue
        label = section.heading or "the request"
        before = len(section.body)
        try:
            section.body = await condense_text(
                section.body, label=label, request=request, target=target, chat=chat
            )
            how = "condensed by the model to its requirement points"
        except Exception as exc:  # noqa: BLE001 — a model outage must not lose the run
            logger.warning(f"Condensing {label!r} failed ({exc}); cutting it to fit instead")
            section.body = section.body[:target]
            how = "cut to fit (the model was unavailable to condense it)"
        if section.heading:
            section.heading = f"{section.heading} — condensed from {before:,} characters"
        notes.append(f'"{label}" ({before:,} characters) was {how}: {len(section.body):,} now.')

    result = join_sections(sections)
    if len(result) > limit:  # the last resort, said out loud
        result = result[:limit]
        notes.append(f"The requirement was cut to {limit:,} characters to fit the model.")
    logger.info(
        f"Requirement fitted: {original:,} -> {len(result):,} characters ({len(notes)} step(s))"
    )
    return result, notes
