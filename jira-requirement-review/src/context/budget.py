"""Aggregate payload budgeting for gathered context documents.

The per-field caps in ``config`` bound each field, but only a total budget bounds
the combined ``documents`` payload that crosses the workflow<->activity boundary
(and is held in workflow history). This keeps the highest-priority documents
within the budget, preserving their original order, and reports what was dropped.

Pure (no SDK / no I/O) so it is unit-testable.

**Priority is a table, not a chain of ``if``.** Adding a source — attachments,
Confluence, whatever comes next — is one row in ``PRIORITY``. That matters
because the ordering decides what survives budget pressure, and a new source
appended to the bottom of an ``if/elif`` silently inherited the fallback rank.
"""

from __future__ import annotations

from typing import Any

from config import MAX_TOTAL_CONTEXT_CHARS

# Lower number = higher priority = kept first under budget pressure.
#
# Keyed on the document's ``kind``, never on its label: "Target ABC-1 (linked
# issues)" starts with "Target" exactly like the requirement does, and ranking on
# the label prefix once put linked issues below subtask comments.
PRIORITY: dict[str, int] = {
    # The requirement itself. Never dropped — see NEVER_DROP below.
    "requirement": 0,
    # Small, high-signal, and the only grounding for Dependency Question findings.
    "linked_issues": 1,
    # The discussion on the target card, where scope is usually renegotiated.
    "target_comments": 2,
    # An attachment is frequently where the real specification lives: a ticket
    # that says "see the attached spec" contributes nothing without it.
    "attachment": 3,
    # Same reasoning for a linked Confluence page.
    "confluence": 4,
    # The comment that triggered the run states what was actually asked for.
    "trigger_comment": 5,
    "transcript": 6,
    "parent_context": 7,
    "parent_comments": 8,
    "subtask_context": 9,
    "subtask_comments": 10,
}

# Rank for a kind not named above. Deliberately worse than everything known, so
# an unregistered source degrades quietly rather than outranking the requirement.
UNKNOWN_PRIORITY = 50

# Kinds that must survive any amount of budget pressure. Dropping the
# requirement leaves a review of nothing, which reads as a broken agent.
NEVER_DROP = frozenset({"requirement"})


def _priority(doc: dict[str, Any]) -> int:
    """Lower number = higher priority (kept first under budget pressure)."""
    return PRIORITY.get(doc.get("kind", ""), UNKNOWN_PRIORITY)


def trim_documents_to_budget(
    documents: list[dict[str, Any]], budget: int | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """Keep highest-priority documents within ``budget`` total text chars.

    Returns ``(kept_documents_in_original_order, warning_or_None)``.

    A ``NEVER_DROP`` document is admitted even when it alone exceeds the budget:
    a truncated requirement is still reviewable, while no requirement at all is
    not, and the per-field caps have already bounded its size.
    """
    budget = MAX_TOTAL_CONTEXT_CHARS if budget is None else budget
    total = sum(len(d.get("text", "")) for d in documents)
    if total <= budget:
        return documents, None

    # Greedily admit by priority (stable on original index as tie-break).
    order = sorted(range(len(documents)), key=lambda i: (_priority(documents[i]), i))
    kept_idx: set[int] = set()
    used = 0
    for i in order:
        size = len(documents[i].get("text", ""))
        if documents[i].get("kind") in NEVER_DROP:
            kept_idx.add(i)
            used += size
            continue
        if used + size <= budget:
            kept_idx.add(i)
            used += size

    kept = [d for i, d in enumerate(documents) if i in kept_idx]
    dropped = len(documents) - len(kept)
    warning = (
        f"context payload trimmed to fit ~{budget} chars: kept {len(kept)} of "
        f"{len(documents)} documents ({used}/{total} chars); dropped {dropped} "
        f"lower-priority document(s)"
    )
    return kept, warning


def dropped_documents(
    documents: list[dict[str, Any]], kept: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Which documents the trim removed — needed so the review can say so.

    Completeness reporting has to name anything that was left out; a document
    silently dropped for budget looks identical to one that never existed.
    """
    kept_ids = {id(d) for d in kept}
    return [d for d in documents if id(d) not in kept_ids]
