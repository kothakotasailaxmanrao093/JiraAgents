"""Atlassian Document Format (ADF) rendering — pure, no I/O.

Jira Cloud's v3 comment API requires the comment body as an ADF document. This
module builds ADF three ways:

* ``render_review_to_adf``      — structured path: a ``Review`` -> ADF doc.
* ``render_review_to_markdown`` — the human-readable/editable preview (Agent A).
* ``markdown_to_adf``           — the human-edited markdown -> ADF (Agent B post).

The markdown subset supported by ``markdown_to_adf`` is intentionally small and
matches what ``render_review_to_markdown`` emits: ``##``/``###`` headings, ``-``
bullet lists (one level of nesting), ``**bold**`` inline, ``---`` rules, and
plain paragraphs. Anything else degrades to a paragraph.
"""

from __future__ import annotations

from typing import Any

from shared.adf import (
    bullet_list,
    doc,
    heading,
    list_item,
    markdown_to_adf,
    paragraph,
    rule,
    text_node,
)

from .models import Finding, Readiness, Review

# ---------------------------------------------------------------------------
# ADF node builders
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Structured review -> ADF
# ---------------------------------------------------------------------------

_EMPTY_MSG = (
    "No assumptions, open questions, requirement gaps, ambiguities, edge cases, "
    "or missing functional/technical/acceptance-criteria details were identified "
    "for this requirement."
)

_READINESS_LABELS = {
    "ready": "Ready",
    "needs_minor_clarification": "Needs Minor Clarification",
    "needs_major_clarification": "Needs Major Clarification",
    "not_ready": "Not Ready",
}


def _readiness_label(readiness: Readiness) -> str:
    return _READINESS_LABELS.get(readiness.level.value, readiness.level.value)


def _bullet_text(f: Finding) -> str:
    """Description, prefixed with a ``[PRIORITY]`` tag when the finding carries one."""
    if f.priority is not None:
        return f"[{f.priority.value.upper()}] {f.description}"
    return f.description


def render_review_to_adf(
    review: Review,
    issue_key: str,
    footer: str = "",
    readiness: Readiness | None = None,
) -> dict[str, Any]:
    """Render a ``Review`` to an ADF document.

    Layout: an H2 title, an optional readiness summary, then one H3 section per
    non-empty category. Each finding is a single bullet (the description, prefixed
    with its priority when set — evidence/confidence are kept in the structured
    data but not shown in the comment). An optional italic footer is separated by
    a rule.
    """
    content: list[dict[str, Any]] = [heading(f"Requirement Review — {issue_key}", level=2)]

    if readiness is not None:
        content.append(
            paragraph(
                [
                    text_node("Overall Readiness: ", strong=True),
                    text_node(f"{_readiness_label(readiness)} (score {readiness.score}/5)"),
                ]
            )
        )
        if readiness.executive_summary:
            content.append(paragraph([text_node(readiness.executive_summary, em=True)]))

    grouped = review.grouped()
    types = review.non_empty_types()
    if not types:
        content.append(paragraph(_EMPTY_MSG))
    for ftype in types:
        content.append(heading(ftype.value, level=3))
        items = [list_item([paragraph(_bullet_text(f))]) for f in grouped[ftype]]
        content.append(bullet_list(items))

    if footer:
        content.append(rule())
        content.append(paragraph([text_node(footer, em=True)]))
    return doc(content)


def render_review_to_markdown(
    review: Review,
    issue_key: str,
    readiness: Readiness | None = None,
) -> str:
    """Human-readable, editable preview (Agent A). Priority-prefixed, description-only
    bullets per category; the footer is added at post time, so it is omitted here."""
    lines: list[str] = [f"## Requirement Review — {issue_key}", ""]

    if readiness is not None:
        lines.append(
            f"**Overall Readiness:** {_readiness_label(readiness)} (score {readiness.score}/5)"
        )
        if readiness.executive_summary:
            lines.append(f"_{readiness.executive_summary}_")
        lines.append("")

    grouped = review.grouped()
    types = review.non_empty_types()
    if not types:
        lines.append(f"_{_EMPTY_MSG}_")
        return "\n".join(lines).strip() + "\n"
    for ftype in types:
        lines.append(f"### {ftype.value}")
        for f in grouped[ftype]:
            lines.append(f"- {_bullet_text(f)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Markdown -> ADF (human-edited path)
# ---------------------------------------------------------------------------


__all__ = [
    "bullet_list",
    "doc",
    "heading",
    "list_item",
    # Re-exported: callers and tests import the markdown converter from here,
    # where it used to be defined. The implementation is shared now.
    "markdown_to_adf",
    "paragraph",
    "render_review_to_adf",
    "render_review_to_markdown",
    "rule",
    "text_node",
]
