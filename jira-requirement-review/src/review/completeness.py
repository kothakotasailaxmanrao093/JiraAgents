"""Accounting for everything the review did and did not read.

A review is only trustworthy if it says what it looked at. Without this, a thin
review and a thin ticket are indistinguishable — and in practice the ticket gets
blamed, because the reader has no way to know the specification was in a
password-protected spreadsheet the agent could not open.

So every run produces two lists, and both are populated even when empty:

* :func:`sources_read`    — every source consulted, by name.
* :func:`sources_missing` — everything that was tried and failed, or that the
  ticket simply does not have, each with what to do about it.

The rule for ``sources_missing``: **every entry reads as an instruction, not an
apology.** "FL-130 has no acceptance criteria — please add them", never "some
information was unavailable". The difference decides whether anyone acts on it.
"""

from __future__ import annotations

import re
from typing import Any

from shared.contract import MissingSource

# Warnings the gather step emits are prose. These patterns turn the ones we
# recognise into structured advice; anything unrecognised is still reported, so
# a new warning can never vanish just because this table has not caught up.
_ADVICE: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # Checked before the generic "could not be read" pattern below —
        # "re-attach the file" is the wrong advice for a link, not a file
        # (Bug 6: a link referenced in the ticket that is not a Confluence
        # page on this site — a Google Doc, a different Atlassian site — was
        # previously invisible entirely; see find_unread_links).
        re.compile(r"not a confluence page on this site", re.I),
        "paste the relevant section into the ticket, or attach the document",
    ),
    (
        re.compile(r"could not be downloaded|could not be read", re.I),
        "please check the file opens, and re-attach it if it is corrupt or protected",
    ),
    (
        re.compile(r"not permitted|no access|403", re.I),
        "please grant this account access, or paste the relevant section into the ticket",
    ),
    (
        re.compile(r"no download link", re.I),
        "please re-attach the file",
    ),
    (
        re.compile(r"unsupported attachment type", re.I),
        "please attach it in a readable format (PDF, Word, Excel, PowerPoint, CSV or text)",
    ),
    (
        re.compile(r"payload cap|only the first", re.I),
        "narrow the ticket, or review the remainder separately",
    ),
    (
        re.compile(r"trimmed to fit", re.I),
        "the lowest-priority context was dropped; the requirement itself was kept",
    ),
)


def _advice_for(warning: str) -> str:
    for pattern, advice in _ADVICE:
        if pattern.search(warning):
            return advice
    return ""


def sources_read(documents: list[dict[str, Any]]) -> list[str]:
    """Every source that actually contributed text, by name.

    Built from the documents themselves rather than from what was *requested*,
    so it can never claim to have read something that failed.
    """
    seen: list[str] = []
    for doc in documents:
        label = (doc.get("source_label") or "").strip()
        if label and label not in seen and (doc.get("text") or "").strip():
            seen.append(label)
    return seen


def _requirement_gaps(
    target: dict[str, Any], documents: list[dict[str, Any]]
) -> list[MissingSource]:
    """Gaps in the ticket itself, as opposed to things that failed to load.

    These are the two the brief names explicitly: a ticket with no description,
    and a ticket with no acceptance criteria. Both are extremely common and both
    are actionable by the person reading the review.
    """
    gaps: list[MissingSource] = []
    key = target.get("key") or "This ticket"

    description = (target.get("description_text") or "").strip()
    has_other_requirement = any(
        doc.get("kind") in ("attachment", "confluence") and (doc.get("text") or "").strip()
        for doc in documents
    )
    if not description and not has_other_requirement:
        gaps.append(
            MissingSource(
                what=f"{key} has no description",
                why="",
                what_to_do="please describe what needs to be built",
            )
        )
    elif not description:
        # The requirement was readable, just not where a reader would look.
        gaps.append(
            MissingSource(
                what=f"{key} has no description of its own",
                why="the requirement was read from an attachment or a linked page",
                what_to_do=(
                    "please summarise it on the ticket so it is readable " "without opening files"
                ),
            )
        )

    # A Sub-task carries completion criteria; its acceptance criteria are its
    # parent Story's. BGV-32 was told "has no acceptance criteria" although
    # BGV-30 had them (2026-09-25).
    if target.get("is_subtask"):
        criteria_found = re.search(r"completion criteria", description, re.I) or any(
            _AC_HINTS.search(doc.get("text") or "")
            for doc in documents
            if doc.get("kind") == "parent_context"
        )
        if not criteria_found:
            gaps.append(
                MissingSource(
                    what=(
                        f"{key} has no completion criteria, and its parent has no "
                        "acceptance criteria"
                    ),
                    why="",
                    what_to_do="please add them, so it is clear when this Sub-task is done",
                )
            )
        return gaps

    if description and not _mentions_acceptance_criteria(description, documents):
        gaps.append(
            MissingSource(
                what=f"{key} has no acceptance criteria",
                why="",
                what_to_do=(
                    "please add them, so it is testable and two developers " "build the same thing"
                ),
            )
        )

    return gaps


_AC_HINTS = re.compile(
    r"acceptance criteria|acceptance-criteria\b|\bgiven\b.*\bwhen\b.*\bthen\b|"
    r"definition of done|\bAC\b\s*[:\-]",
    re.I | re.S,
)


def _mentions_acceptance_criteria(description: str, documents: list[dict[str, Any]]) -> bool:
    """Whether acceptance criteria appear anywhere that was read.

    Deliberately generous: this decides whether to *tell someone off* for not
    writing criteria, and a false accusation is worse than a missed nudge. Any
    recognisable form counts, in the description or in an attached spec.
    """
    if _AC_HINTS.search(description):
        return True
    return any(
        _AC_HINTS.search(doc.get("text") or "")
        for doc in documents
        if doc.get("kind") in ("attachment", "confluence", "requirement")
    )


def sources_missing(
    target: dict[str, Any],
    documents: list[dict[str, Any]],
    warnings: list[str],
) -> list[MissingSource]:
    """Everything the review could not read, plus gaps in the ticket itself.

    ``warnings`` come from the gather step, where each unreadable file and
    unreachable page was recorded by name.
    """
    missing = _requirement_gaps(target, documents)

    for warning in warnings:
        text = (warning or "").strip()
        if not text:
            continue
        missing.append(MissingSource(what=text, why="", what_to_do=_advice_for(text)))

    return missing


def summarise(read: list[str], missing: list[MissingSource]) -> str:
    """One plain sentence for the top of the reply."""
    if not read:
        return "Nothing could be read for this ticket."
    sentence = f"Read {len(read)} source(s)."
    if missing:
        sentence += f" {len(missing)} item(s) could not be read or are missing from the ticket."
    return sentence
