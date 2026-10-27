"""What Planning's memory is asked, and how its answers are worded (2026-10-26).

Planning keeps each story's requirement facts, its TDD versions and the design
decisions in force (in its encrypted graph). Orchestration asks it two things:

* "facts": is this story still the same requirement as its current TDD? A
  reworded story keeps its TDD; only a changed fact makes a new version.
* "discuss" with a kind: is this change to the TDD valid? Only a valid one is
  applied; a person can insist with "update the TDD anyway".

Pure — no network, no environment, no clock.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Verdicts of Planning's "facts" mode.
SAME, REWORDED, CHANGED, UNSURE, NEW = "same", "reworded", "changed", "unsure", "new"
KEEP_TDD = (SAME, REWORDED, UNSURE)

# Verdicts of Planning's "discuss" mode.
ANSWER_ONLY, APPLY, REFUSE, ALREADY_PRESENT, PARTIAL = (
    "answer_only",
    "apply",
    "refuse",
    "already_present",
    "partial",
)

MAX_STORY_CHARS = 30_000
MAX_FILE_CHARS = 6_000  # of one file's text, in the story the memory compares
_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def story_text(
    root: dict[str, Any],
    tickets: list[dict[str, Any]],
    comments: list[str],
    files: list[dict[str, Any]] | None = None,
) -> str:
    """The story as its requirement: summary, description, its sub-tasks, the
    people's comments that add to it (``comments`` is already filtered: no agent
    replies, no requests to the agent), and its attached files (2026-10-27). No
    review text: that changes run to run. A document counts by its text; an
    image or video by its file id, since a model describes it a little
    differently each time — unless it was read for a bug report (``text``)."""
    lines = [
        f"{root.get('key', '')}: {root.get('summary', '')}".strip(),
        str(root.get("description") or "").strip() or "(no description)",
    ]
    for ticket in tickets:
        lines.append(f"Sub-task {ticket.get('key', '')}: {ticket.get('summary', '')}".strip())
        if ticket.get("description"):
            lines.append(str(ticket["description"]).strip())
    if comments:
        lines.append("Comments:")
        lines += [f"- {c.strip()}" for c in comments if c.strip()]
    if files:
        lines.append("Attached files:")
        for f in files:
            name = f"{f.get('filename')} (file {f.get('id')})"
            text = str(f.get("text") or "").strip()
            if text:
                lines += [f"- {name}:", text[:MAX_FILE_CHARS]]
            else:
                lines.append(f"- {name}")
    return "\n".join(lines)[:MAX_STORY_CHARS]


def normalised_hash(text: str) -> str:
    """The text with case, punctuation and spacing removed, hashed: a typo-level
    edit that changes none of the words keeps the same hash."""
    plain = _SPACE.sub(" ", _PUNCT.sub(" ", (text or "").lower())).strip()
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def change_lines(changes: list[dict[str, Any]]) -> list[str]:
    """Planning's changed facts as reply lines: "daily_limit: 2,00,000 INR → 2,50,000 INR"."""
    out = []
    for change in changes or []:
        slot = str(change.get("slot") or "requirement").replace("_", " ")
        old, new = str(change.get("old") or "").strip(), str(change.get("new") or "").strip()
        if old and new:
            line = f"{slot}: {old} → {new}"
        elif new:
            line = f"{slot}: added — {new}"
        elif old:
            line = f"{slot}: removed — {old}"
        else:
            line = slot
        why = str(change.get("why") or "").strip()
        out.append(f"{line} ({why})" if why else line)
    return out


def verdict_of(out: dict[str, Any]) -> str:
    """The discuss verdict, also from a Planning that predates verdicts."""
    verdict = str(out.get("verdict") or "").strip().lower()
    if verdict in (ANSWER_ONLY, APPLY, REFUSE, ALREADY_PRESENT, PARTIAL):
        return verdict
    return APPLY if out.get("change_recommended") else ANSWER_ONLY


def to_apply(out: dict[str, Any]) -> list[str]:
    """The changes Planning agreed to, as a list (falls back to proposed_change)."""
    items = [str(a).strip() for a in out.get("apply") or [] if str(a).strip()]
    if items:
        return items
    proposed = str(out.get("proposed_change") or "").strip()
    return [proposed] if proposed else []


def refused_lines(out: dict[str, Any]) -> list[str]:
    """Each refused point with its reason."""
    lines = []
    for item in out.get("refused") or []:
        if isinstance(item, dict):
            change = str(item.get("change") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if change or reason:
                lines.append(f"{change} — {reason}" if change and reason else change or reason)
        elif str(item).strip():
            lines.append(str(item).strip())
    return lines
