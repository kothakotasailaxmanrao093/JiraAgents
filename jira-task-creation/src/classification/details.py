"""Hard details: the exact facts a requirement line states, which the ticket
that line maps to must carry.

Coverage (``uncovered_lines``) proves each line maps to SOME Story or Sub-task.
It cannot prove the ticket kept the line's facts. BGV-41 (2026-09-25) mapped
every line and still dropped five stated rules: "Price missing", "every billing
contact", "30 days after the invoice date", the resend record, "only one
reminder". The self-review model missed them too — a 22-line requirement is too
much to cross-check in one pass. This is deterministic, and costs no call.

A hard detail is something a person typed on purpose and a ticket can carry
word for word:

* a quoted name           "Price missing", "Not sent"
* a number with its unit  30 days, 5 working days, 24 months, 8 MB, 25 per page
* a clock time            06:00 IST
* an identifier pattern   INV-YYYYMM-<client code>
* a limit                 only one reminder, at most 3 attempts
* "every" / "all" + noun  every billing contact

Soft wording is not checked — paraphrase is fine, dropping a fact is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.models.schemas import Story, WorkBreakdown

# Double quotes only: an apostrophe ("the client's", "officer's") is not a quote.
_QUOTED = re.compile(r"[\"“”]([^\"“”<>]{2,60})[\"“”]")
_NUMBER_UNIT = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*"
    r"(working days?|business days?|calendar days?|days?|hours?|hrs?|minutes?|mins?|"
    r"seconds?|weeks?|months?|years?|mb|kb|gb|%|characters?|attempts?|times|per page)\b",
    re.IGNORECASE,
)
_CLOCK = re.compile(r"\b(\d{1,2}:\d{2})\b")
_IDENTIFIER = re.compile(r"\b([A-Z]{2,}-[A-Z0-9]{2,}(?:-[A-Z0-9]+)*)")
_LIMIT = re.compile(
    r"\b(only one|at most \w+|no more than \w+|exactly \w+|at least \w+|up to \w+)\s+(\w+)",
    re.IGNORECASE,
)
# "…are not created twice" — an idempotency rule (BGV-41 line 10, 2026-09-25).
_NOT_TWICE = re.compile(r"\b(?:not|never)\b[^.;]{0,40}\btwice\b", re.IGNORECASE)
# "…each resend is recorded with date, time and who resent it" — an audit rule
# (BGV-41 line 16): the named fields must be kept.
_RECORDED_WITH = re.compile(
    r"\b(?:recorded|logged|stored|kept|saved)\s+with\s+([^.;]{3,80})", re.IGNORECASE
)
_EVERY = re.compile(r"\b(?:every|all)\s+(\w+(?:\s+\w+)?)", re.IGNORECASE)
# Linking words end the noun: "all invoices from the last 24 months" is "invoices".
_LINKING = {"from", "of", "in", "on", "for", "with", "to", "that", "which", "who", "and", "or"}
# Words that follow "every"/"all" without naming anything checkable.
_EVERY_NOISE = {"the", "of", "time", "times", "day", "days", "month", "months", "cases"}


@dataclass(frozen=True)
class Detail:
    kind: str
    text: str


def hard_details(line: str) -> list[Detail]:
    """The hard details one requirement line states."""
    found: list[Detail] = []
    for m in _QUOTED.finditer(line):
        found.append(Detail("quoted", m.group(1).strip()))
    for m in _NUMBER_UNIT.finditer(line):
        found.append(Detail("number", f"{m.group(1)} {m.group(2).lower()}"))
    for m in _CLOCK.finditer(line):
        found.append(Detail("clock", m.group(1)))
    for m in _IDENTIFIER.finditer(line):
        # "…, e.g. INV-202609-ACME" is an example of the format, not a rule.
        if re.search(r"e\.g\.?\s*$|for example\s*$", line[max(0, m.start() - 14) : m.start()]):
            continue
        found.append(Detail("identifier", m.group(1)))
    for m in _LIMIT.finditer(line):
        found.append(Detail("limit", f"{m.group(1).lower()} {m.group(2).lower()}"))
    if _NOT_TWICE.search(line):
        found.append(Detail("not_twice", "not twice"))
    for m in _RECORDED_WITH.finditer(line):
        fields = [
            f.strip().split()[0].lower()
            for f in re.split(r",|\band\b", m.group(1))
            if f.strip() and f.strip().split()[0].lower() not in {"the", "a", "an", "its"}
        ]
        if fields:
            found.append(Detail("recorded", ", ".join(fields)))
    for m in _EVERY.finditer(line):
        words = m.group(1).lower().split()
        if len(words) == 2 and words[1] in _LINKING:
            words = words[:1]
        if words and words[0] not in _EVERY_NOISE and words[0] not in _LINKING:
            found.append(Detail("every", " ".join(words)))
    # One detail once, in order.
    seen: set[tuple[str, str]] = set()
    return [
        d
        for d in found
        if not ((d.kind, d.text.lower()) in seen or seen.add((d.kind, d.text.lower())))
    ]


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def _story_text(story: Story) -> str:
    parts = [story.title, story.user_story_statement, story.description, *story.acceptance_criteria]
    for sub in story.subtasks:
        parts += [sub.title, sub.description, sub.expected_outcome, sub.completion_criteria]
    return _flat(" ".join(parts))


def carried(detail: Detail, text: str) -> bool:
    """Whether ``text`` (already flattened) carries ``detail``."""
    if detail.kind == "quoted":
        return _flat(detail.text) in text
    if detail.kind == "number":
        number, unit = detail.text.split(" ", 1)
        stem = re.sub(r"s$", "", unit)  # day(s), month(s)…
        return re.search(rf"\b{re.escape(number)}\s*{re.escape(stem)}", text) is not None
    if detail.kind == "clock":
        return detail.text in text
    if detail.kind == "identifier":
        return detail.text.lower() in text
    if detail.kind == "limit":
        limiter, noun = detail.text.rsplit(" ", 1)
        stem = re.sub(r"s$", "", noun)
        if limiter == "only one":
            # "only one reminder", "a single reminder", "one reminder only".
            return (
                re.search(rf"\b(?:only one|a single|single|one)\s+{re.escape(stem)}", text)
                is not None
            )
        return limiter in text and stem in text
    if detail.kind == "not_twice":
        return re.search(r"\btwice\b|\bduplicat|\bonce\b|already exist", text) is not None
    if detail.kind == "recorded":
        fields = detail.text.split(", ")
        audit = re.search(r"\b(record|log|audit|history|stored|saved)", text) is not None
        return audit and all(re.search(rf"\b{re.escape(f)}", text) for f in fields)
    if detail.kind == "every":
        noun = re.sub(r"s$", "", detail.text)
        return (
            re.search(rf"\b(?:every|all|each)\s+(?:\w+\s+){{0,2}}{re.escape(noun)}", text)
            is not None
        )
    return True


def missing_details(
    lines: list[str], coverage: Any, breakdown: WorkBreakdown
) -> list[tuple[int, Detail, str]]:
    """``(line number, detail, where it should be)`` for every hard detail the
    mapped ticket does not carry.

    Checked against the Story the line maps to — including its Sub-tasks — so
    "30 days" in a reminder Story does not vouch for "due 30 days after the
    invoice date" in the sending Story. Without a mapping, any Story will do.
    """
    if not lines:
        return []
    by_title: dict[str, Story] = {}
    for story in breakdown.stories:
        by_title[_norm_title(story.title)] = story
        for sub in story.subtasks:
            by_title[_norm_title(sub.title)] = story
    everything = _flat(" ".join(_story_text(s) for s in breakdown.stories))
    mapping = coverage if isinstance(coverage, dict) else {}

    gaps: list[tuple[int, Detail, str]] = []
    for number, line in enumerate(lines, start=1):
        named = mapping.get(str(number), mapping.get(number))
        names = named if isinstance(named, list) else [named]
        stories = [by_title[_norm_title(n)] for n in names if n and _norm_title(n) in by_title]
        scope = _flat(" ".join(_story_text(s) for s in stories)) if stories else everything
        where = ", ".join(dict.fromkeys(s.title for s in stories)) or "any Story"
        for detail in hard_details(line):
            if not carried(detail, scope):
                gaps.append((number, detail, where))
    return gaps


def _norm_title(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(text).lower()).strip()


def describe(gaps: list[tuple[int, Detail, str]], lines: list[str]) -> str:
    """The instruction for the one regeneration."""
    items = [f'line {n} states "{phrase(d)}" — not in {where}' for n, d, where in gaps[:12]]
    return (
        "These stated details are missing from the tickets their lines map to: "
        + "; ".join(items)
        + ". Carry each one exactly as the requirement states it."
    )


def target_story(line_number: int, line: str, coverage: Any, breakdown: WorkBreakdown) -> Story:
    """The Story a line's missing fact belongs in: the first one the line maps
    to, else the Story sharing most words with the line."""
    by_title: dict[str, Story] = {}
    for story in breakdown.stories:
        by_title[_norm_title(story.title)] = story
        for sub in story.subtasks:
            by_title[_norm_title(sub.title)] = story
    mapping = coverage if isinstance(coverage, dict) else {}
    named = mapping.get(str(line_number), mapping.get(line_number))
    for name in named if isinstance(named, list) else [named]:
        if name and _norm_title(name) in by_title:
            return by_title[_norm_title(name)]
    words = set(re.findall(r"[a-z]{4,}", line.lower()))
    return max(
        breakdown.stories,
        key=lambda s: len(words & set(re.findall(r"[a-z]{4,}", _story_text(s)))),
    )


def phrase(detail: Detail) -> str:
    """The detail as a person reads it: "every billing contact", not "billing contact"."""
    if detail.kind == "every":
        return f"every {detail.text}"
    if detail.kind == "recorded":
        return f"recorded with {detail.text}"
    if detail.kind == "not_twice":
        return "never created twice"
    return detail.text
