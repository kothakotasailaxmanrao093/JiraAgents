# GENERATED FILE — DO NOT EDIT.
# Mirrored from the canonical shared/ package by scripts/sync_shared.py.
# Edit shared/adf.py at the repository root and re-run that script.
"""Atlassian Document Format, both directions. Pure — no I/O.

Jira Cloud's v3 API takes and returns comment and description bodies as ADF, so
every agent needs to read it and write it. Before this module there were two
implementations:

* The work-breakdown agent built ADF from ``(heading, body)`` pairs and read it back
  with a **line-aware** walker.
* The review agent had richer node builders (bold/italic marks, rules, markdown → ADF)
  but read ADF back by joining every text node with a space.

Both halves are kept here, and the reader is the work-breakdown agent's. That is not a
stylistic preference — the review agent's reader collapsed a whole ticket
onto one line:

    Acceptance Criteria The driver must be able to: record a reading see past
    readings Latency must be under 2s.

A reviewer asked to find *missing acceptance criteria* cannot see that a list
exists in that. The line-aware reader produces the structure the model needs.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Node builders — writing ADF
# ---------------------------------------------------------------------------


def text_node(text: str, *, strong: bool = False, em: bool = False) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": text}
    marks: list[dict[str, str]] = []
    if strong:
        marks.append({"type": "strong"})
    if em:
        marks.append({"type": "em"})
    if marks:
        node["marks"] = marks
    return node


def paragraph(runs: Any) -> dict[str, Any]:
    if isinstance(runs, str):
        runs = [text_node(runs)]
    return {"type": "paragraph", "content": list(runs)}


def heading(text: str, level: int = 3) -> dict[str, Any]:
    level = max(1, min(level, 6))
    return {"type": "heading", "attrs": {"level": level}, "content": [text_node(text)]}


def rule() -> dict[str, Any]:
    return {"type": "rule"}


def list_item(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "listItem", "content": content}


def bullet_list(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "bulletList", "content": items}


def doc(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": content}


def _bullet_list_of_strings(items: list[str]) -> dict[str, Any]:
    """A bullet list from plain strings, skipping blanks."""
    return bullet_list([list_item([paragraph(item)]) for item in items if item and item.strip()])


def blocks_to_doc(blocks: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an ADF document from ``(heading, body)`` pairs.

    ``body`` may be a string (rendered as a paragraph) or a list (rendered as a
    bullet list). Empty bodies are skipped so Jira never shows a bare heading.
    """
    content: list[dict[str, Any]] = []
    for head, body in blocks:
        # A list body (a footer-less bullet list, e.g. the HELP/AMBIGUOUS
        # options) must be checked BEFORE the string-shaped branches below —
        # a list is truthy, so "body and not head" used to catch it first and
        # render str(body), the raw Python list repr, instead of bullets.
        if isinstance(body, list):
            items = [str(i) for i in body if str(i).strip()]
            if not items:
                if head:
                    content.append(heading(head))
                continue
            if head:
                content.append(heading(head))
            content.append(_bullet_list_of_strings(items))
            continue
        # A block with a heading and no body is a title; one with a body and no
        # heading is a footer. Both are rendered, unlike an empty pair.
        if head and not body:
            content.append(heading(head))
            continue
        if body and not head:
            content.append(paragraph(str(body)))
            continue
        text = str(body).strip()
        if not text:
            continue
        content.append(heading(head))
        content.append(paragraph(text))
    if not content:
        content = [paragraph("No description provided.")]
    return doc(content)


# ---------------------------------------------------------------------------
# Reading ADF back to text
# ---------------------------------------------------------------------------

# Nodes whose children are inline content: their text forms one line.
_ADF_TEXT_BLOCKS = frozenset({"paragraph", "heading", "codeBlock", "taskItem", "decisionItem"})


def _adf_inline(node: Any) -> str:
    """The text of one block, with its original spacing.

    Joined with ``""``, not ``" "``: ADF splits a sentence at every formatting
    change, so "Allow **drivers** to log" arrives as three text nodes whose
    spaces are already inside them. Joining on a space produced "Allow
    drivers  to log".
    """
    parts: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                walk(child)
            return
        if not isinstance(item, dict):
            return
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "hardBreak":
            parts.append("\n")
        elif kind in ("mention", "emoji"):
            parts.append(str((item.get("attrs") or {}).get("text") or ""))
        elif kind in ("inlineCard", "blockCard"):
            # A pasted link Jira turned into a card still has to be visible:
            # this is how a Confluence page linked that way is found.
            parts.append(str((item.get("attrs") or {}).get("url") or ""))
        for child in item.get("content") or []:
            walk(child)

    walk(node)
    return "".join(parts)


def _adf_lines(node: Any) -> list[str]:
    """Render an ADF node to lines, keeping the structure a reader relies on."""
    if isinstance(node, list):
        lines: list[str] = []
        for child in node:
            lines.extend(_adf_lines(child))
        return lines
    if not isinstance(node, dict):
        return []

    kind = node.get("type")

    if kind in _ADF_TEXT_BLOCKS:
        text = _adf_inline(node).strip()
        return text.split("\n") if text else []

    if kind in ("bulletList", "orderedList"):
        ordered = kind == "orderedList"
        out: list[str] = []
        for index, item in enumerate(node.get("content") or [], start=1):
            marker = f"{index}. " if ordered else "- "
            item_lines = [line for line in _adf_lines(item) if line.strip()]
            if not item_lines:
                continue
            out.append(marker + item_lines[0])
            # A nested list or second paragraph stays under its own bullet.
            out.extend("  " + line for line in item_lines[1:])
        return out

    if kind == "tableRow":
        cells = [" ".join(_adf_lines(cell)).strip() for cell in node.get("content") or []]
        row = " | ".join(cell for cell in cells if cell)
        return [row] if row else []

    if kind == "rule":
        return []

    out = []
    for child in node.get("content") or []:
        out.extend(_adf_lines(child))
    return out


def to_text(node: Any) -> str:
    """Flatten an ADF document (or plain string) down to readable text.

    Structure is preserved deliberately. Flattening everything onto one line
    cost a live run three Stories: a bullet list typed in Jira's editor has no
    "-" characters of its own, so "Please add: / driver shift roster / fuel
    spend by depot / tyre replacement log" arrived as a single sentence and was
    decomposed as a single capability.
    """
    if not node:
        return ""
    if isinstance(node, str):
        return node.strip()
    text = "\n".join(_adf_lines(node))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def comment_text(node: Any, keep: str = "") -> str:
    """A comment's text without the people it opens by mentioning.

    Clicking Reply in Jira pre-fills "@<author>" at the start of the comment, so
    "@Aetherion review" typed as a reply arrives as "@Kothakota Sai Laxman Rao
    @Aetherion review". The verb then no longer opens the comment, and routing
    fell back to the model for an explicit "review" (BGV-1, 2026-09-24). A
    person's name can be several words, so this has to happen on the ADF, where
    a mention is one node — not on the text. Mentions later in the comment are
    kept: "assign it to @Ann" means something. A mention containing ``keep``
    (the trigger keyword) is never dropped, in case the agent is a Jira user.
    """
    if not isinstance(node, dict):
        return to_text(node)
    blocks = list(node.get("content") or [])
    if not blocks or not isinstance(blocks[0], dict):
        return to_text(node)
    inline = list(blocks[0].get("content") or [])
    while inline:
        first = inline[0]
        if not isinstance(first, dict):
            break
        mentioned = str((first.get("attrs") or {}).get("text") or "")
        if first.get("type") == "mention" and not (keep and keep.lower() in mentioned.lower()):
            inline.pop(0)
        elif first.get("type") == "text" and not str(first.get("text") or "").strip():
            inline.pop(0)
        else:
            break
    trimmed = {**node, "content": [{**blocks[0], "content": inline}, *blocks[1:]]}
    return to_text(trimmed)


# ---------------------------------------------------------------------------
# Markdown -> ADF
# ---------------------------------------------------------------------------

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*]\s+(.*)$")
_RULE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")


def _inline_runs(text: str) -> list[dict[str, Any]]:
    """Split a line into ADF text runs, honouring ``**bold**``."""
    runs: list[dict[str, Any]] = []
    pos = 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            runs.append(text_node(text[pos : m.start()]))
        runs.append(text_node(m.group(1), strong=True))
        pos = m.end()
    if pos < len(text):
        runs.append(text_node(text[pos:]))
    return runs or [text_node(text)]


def _indent(raw: str) -> int:
    return len(raw) - len(raw.lstrip(" "))


def _consume_bullets(lines: list[str], start: int) -> tuple[dict[str, Any], int]:
    """Build a one-level-nested bulletList from consecutive bullet lines."""
    items: list[list[Any]] = []  # each: [text, [child_text, ...]]
    i = start
    base_indent = _indent(lines[i])
    while i < len(lines):
        m = _BULLET.match(lines[i])
        if not m:
            break
        indent = len(m.group(1))
        text = m.group(2).strip()
        if indent <= base_indent or not items:
            items.append([text, []])
        else:
            items[-1][1].append(text)
        i += 1

    li_nodes: list[dict[str, Any]] = []
    for text, children in items:
        node_content: list[dict[str, Any]] = [paragraph(_inline_runs(text))]
        if children:
            node_content.append(
                bullet_list([list_item([paragraph(_inline_runs(c))]) for c in children])
            )
        li_nodes.append(list_item(node_content))
    return bullet_list(li_nodes), i


def markdown_to_adf(md: str, footer: str = "") -> dict[str, Any]:
    """Convert a supported markdown subset into an ADF document.

    Supported: ``#``-``######`` headings, ``-``/``*`` bullet lists (one level of
    nesting), ``**bold**`` inline, ``---`` rules, and plain paragraphs;
    consecutive plain lines join into one paragraph. Anything else degrades to a
    paragraph rather than being dropped.
    """
    content: list[dict[str, Any]] = []
    lines = (md or "").splitlines()
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue
        if _RULE.match(stripped):
            content.append(rule())
            i += 1
            continue
        m_head = _HEADING.match(stripped)
        if m_head:
            level = len(m_head.group(1))
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": max(1, min(level, 6))},
                    "content": _inline_runs(m_head.group(2).strip()),
                }
            )
            i += 1
            continue
        if _BULLET.match(raw):
            block, i = _consume_bullets(lines, i)
            content.append(block)
            continue
        # paragraph: gather consecutive plain lines
        para_lines = [stripped]
        i += 1
        while i < n:
            nxt = lines[i]
            if (
                (not nxt.strip())
                or _HEADING.match(nxt.strip())
                or _BULLET.match(nxt)
                or _RULE.match(nxt.strip())
            ):
                break
            para_lines.append(nxt.strip())
            i += 1
        content.append(paragraph(_inline_runs(" ".join(para_lines))))

    if footer:
        content.append(rule())
        content.append(paragraph([text_node(footer, em=True)]))
    if not content:
        content = [paragraph("")]
    return doc(content)
