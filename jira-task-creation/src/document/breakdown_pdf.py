"""The work breakdown as a PDF — GENERATE_LOCAL_PDF=true.

The breakdown is generated inside the agent (no outside service), attached to
the ticket, and summarised in the reply. Nothing in Jira is created, converted,
labelled or edited: the proposed tickets do not exist, so they are numbered
E, S1, S1.1 … and the real keys shown are the tickets that already exist.

PDF through PyMuPDF (``fitz``), which the platform already installs to read PDF
attachments: HTML in, paginated A4 out. ``breakdown_html`` is pure, so what the
document says is tested without rendering it.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field
from html import escape

from src.classification.details import hard_details
from src.classification.details import phrase as detail_phrase
from src.models.schemas import WorkBreakdown

MODE_LINE = "PDF only — no Jira tickets were created or changed"
CREATED_LINE = "Created in Jira — the tickets below now exist"

SECTIONS = (
    "At a glance",
    "Tickets (card numbers)",
    "What the ticket would become",
    "Requirement",
    "Sources read and skipped",
    "Confirmed facts",
    "Missing information",
    "Duplicate check",
    "Proposed tickets",
    "Risks and dependencies",
    "Placement",
    "Which ticket delivers each requirement line",
)

_CSS = """
body { font-family: sans-serif; font-size: 9.5pt; line-height: 1.35; }
h1 { font-size: 15pt; margin: 0 0 4pt 0; }
h2 { font-size: 12pt; margin: 12pt 0 3pt 0; border-bottom: 1px solid #999; }
h3 { font-size: 10.5pt; margin: 8pt 0 2pt 0; }
p, li { margin: 1pt 0; }
.mode { font-weight: bold; }
.box { border: 1px solid #999; padding: 6pt; background-color: #f4f6f8; }
td { padding: 2pt 6pt; vertical-align: top; }
.small { font-size: 8pt; color: #444; }
"""


@dataclass(frozen=True)
class RootPreview:
    """What building would do to the ticket asked on, when it is the root."""

    key: str
    type_before: str
    type_after: str
    summary_before: str
    summary_after: str
    priority: str = ""
    reason: str = ""
    # What would go under it: "3 Sub-tasks", "2 Stories with their Sub-tasks",
    # or "" when nothing would (one step).
    children: str = ""


@dataclass
class BreakdownDocument:
    """Everything the PDF shows. Built by the tool; rendered here."""

    issue_key: str
    issue_summary: str
    project_key: str
    generated_at: str  # "2026-09-29 10:30 UTC"
    agent_version: str
    breakdown: WorkBreakdown
    root: RootPreview | None = None
    related_keys: list[str] = field(default_factory=list)
    sources_read: list[str] = field(default_factory=list)
    sources_missing: list[str] = field(default_factory=list)
    requirement_lines: list[str] = field(default_factory=list)
    coverage: dict[str, str] = field(default_factory=dict)
    quality_notes: list[str] = field(default_factory=list)
    review_questions: list[str] = field(default_factory=list)
    closest_match: dict | None = None
    placement: str = "backlog"
    # "BGV-69 — Candidate Address Verification": the Epic the form named; the
    # Stories go under it and no new Epic is made, as creation does.
    existing_epic: str = ""
    # Proposed items that already exist in Jira ({"proposed_title",
    # "existing_key", "existing_summary", "existing_status"}). In PDF mode the
    # plan is still emailed — they are named here instead of stopping the run.
    already_exist: list[dict] = field(default_factory=list)
    # "BGV-110 · Story · Bulk print …": set when the tickets WERE created (a
    # laptop run keeps this copy in docs-export); empty for a PDF-only run.
    created: list[str] = field(default_factory=list)


def numbered(
    breakdown: WorkBreakdown,
    *,
    root_is_epic: bool,
    existing_epic: str = "",
    card: RootPreview | None = None,
) -> list[str]:
    """The proposed items as a reader refers to them: E, S1, S1.1 …

    Built on a Story, Task or Bug card, the one item IS that card: it is named
    by its key and its Sub-tasks are listed under it (2026-10-07).
    """
    lines: list[str] = []
    if card is not None and card.type_after.lower() != "epic":
        subs = [
            f"S1.{t} {sub.title}"
            for t, sub in enumerate((x for st in breakdown.stories for x in st.subtasks), 1)
        ]
        label = f"{card.key} (this {card.type_after}, unchanged)"
        return [label + (f" — Sub-tasks: {', '.join(subs)}" if subs and card.children else "")]
    if existing_epic:
        lines.append(f"{existing_epic} (existing Epic — no new Epic; the Stories go under it)")
    elif breakdown.epic is not None and not root_is_epic:
        lines.append(f"E  Epic — {breakdown.epic.jira_summary}")
    for s, story in enumerate(breakdown.stories, start=1):
        subs = ", ".join(f"S{s}.{t} {sub.title}" for t, sub in enumerate(story.subtasks, start=1))
        lines.append(f"S{s}  {story.title}" + (f" — Sub-tasks: {subs}" if subs else ""))
    return lines


def _list(items: list[str], empty: str) -> str:
    if not items:
        return f"<p>{escape(empty)}</p>"
    return "<ul>" + "".join(f"<li>{escape(str(i))}</li>" for i in items) + "</ul>"


def _root(doc: BreakdownDocument) -> str:
    if doc.root is None:
        return (
            "<h2>What the ticket would become</h2>"
            f"<p>New tickets in {escape(doc.project_key)}"
            + (
                f", under the existing Epic {escape(doc.existing_epic)}"
                if doc.existing_epic
                else ""
            )
            + f". {escape(doc.issue_key)} itself would not change.</p>"
        )
    r = doc.root
    article = "an" if r.type_after[:1].lower() in "aeiou" else "a"
    return "<h2>What the ticket would become</h2>" + _list(
        [
            f"{r.key} stays {article} {r.type_after} — its type, summary and description "
            "are not changed.",
            f"Under it: {r.children}." if r.children else "Nothing goes under it: it is one step.",
        ],
        "",
    )


def _hierarchy(
    breakdown: WorkBreakdown,
    root_is_epic: bool,
    existing_epic: str,
    created: bool = False,
    card: RootPreview | None = None,
) -> str:
    parts = [
        (
            "<h2>Tickets (created in Jira)</h2>"
            if created
            else "<h2>Proposed tickets (not created)</h2>"
        )
    ]
    if existing_epic:
        parts.append(f"<h3>Existing Epic: {escape(existing_epic)}</h3>")
        parts.append("<p>No new Epic would be made. Every Story below would go under it.</p>")
    elif breakdown.epic is not None:
        epic = breakdown.epic
        label = "The root, as Epic" if root_is_epic else "E — Epic"
        parts.append(f"<h3>{label}: {escape(epic.jira_summary)}</h3>")
        parts.append(f"<p>{escape(epic.business_objective)}</p>")
        parts.append("<p><b>Scope</b></p>" + _list(epic.scope, "Not specified"))
        parts.append("<p><b>Out of scope</b></p>" + _list(epic.out_of_scope, "Not specified"))
        parts.append("<p><b>Acceptance criteria</b></p>" + _list(epic.acceptance_criteria, "—"))
    on_card = card is not None and card.type_after.lower() != "epic"
    for s, story in enumerate(breakdown.stories, start=1):
        if on_card:
            # The one item IS the card: named as it is, never re-titled.
            parts.append(
                f"<h3>{escape(card.key)} — {escape(card.summary_before)} "
                f"(this {escape(card.type_after)}, unchanged)</h3>"
            )
        else:
            parts.append(f"<h3>S{s} — {escape(story.title)}</h3>")
        parts.append(f"<p>{escape(story.user_story_statement)}</p>")
        parts.append(f"<p>{escape(story.description)}</p>")
        parts.append(
            f"<p class='small'>Priority {escape(story.priority.value)} · complexity "
            f"{escape(story.estimated_complexity.value)} · dependencies "
            f"{escape(story.dependencies)}</p>"
        )
        parts.append("<p><b>Acceptance criteria</b></p>" + _list(story.acceptance_criteria, "—"))
        for t, sub in enumerate(story.subtasks, start=1):
            parts.append(
                f"<p><b>S{s}.{t} {escape(sub.title)}</b> — {escape(sub.description)}"
                f"<br/><span class='small'>Done when: {escape(sub.completion_criteria)}</span></p>"
            )
        if story.open_questions:
            parts.append("<p><b>Open questions</b></p>" + _list(story.open_questions, ""))
    return "".join(parts)


def _facts(lines: list[str]) -> list[str]:
    return [
        f'Line {n}: "{detail_phrase(d)}"'
        for n, line in enumerate(lines, start=1)
        for d in hard_details(line)
    ]


def breakdown_html(doc: BreakdownDocument) -> str:
    """The whole document, as HTML. Pure."""
    b = doc.breakdown
    root_is_epic = bool(doc.root and doc.root.type_after.lower() == "epic")
    questions = [f"{s.title}: {q}" for s in b.stories for q in s.open_questions]
    risks = doc.quality_notes + [
        f"{s.title} depends on: {s.dependencies}"
        for s in b.stories
        if s.dependencies.strip().lower() not in {"none", ""}
    ]
    match = doc.closest_match or {}
    duplicate = (
        f"No existing ticket covers this work. Closest: {match.get('existing_key')} "
        f"“{match.get('existing_summary')}” (similarity {float(match.get('score') or 0):.2f})."
        if match
        else "No existing ticket covers this work."
    )
    existing = [
        f"“{d.get('proposed_title')}” already exists as {d.get('existing_key')} "
        f"“{d.get('existing_summary')}” ({d.get('existing_status') or 'status unknown'}) "
        "— not to be created again."
        for d in doc.already_exist
    ]
    cards = [f"{doc.issue_key} — {doc.issue_summary} (asked on)"] + list(doc.related_keys)
    coverage = [f"Line {n} → {title}" for n, title in sorted(doc.coverage.items(), key=_num)]
    counts = b.issue_count()
    if doc.root:
        becomes = f"{doc.root.key}: {doc.root.type_before} → {doc.root.type_after}"
    elif doc.existing_epic:
        becomes = f"new Stories under the existing Epic {doc.existing_epic}"
    else:
        becomes = f"new tickets in {doc.project_key}"
    glance = [
        ("Ticket", f"{doc.issue_key} — {doc.issue_summary}"),
        ("Size", b.classification.value),
        ("Would become", becomes),
        ("Proposed", f"{counts['stories']} Stories, {counts['subtasks']} Sub-tasks"),
        ("Open questions", str(len(questions) + len(doc.review_questions))),
        *([("Already in Jira", f"{len(existing)} of the proposed items")] if existing else []),
        ("Generated", f"{doc.generated_at} · agent {doc.agent_version}"),
    ]
    table = "".join(f"<tr><td><b>{escape(k)}</b></td><td>{escape(v)}</td></tr>" for k, v in glance)
    return "".join(
        [
            f"<h1>Work breakdown — {escape(doc.issue_key)}</h1>",
            f"<p class='mode'>{escape(CREATED_LINE if doc.created else MODE_LINE)}</p>",
            f"<h2>{SECTIONS[0]}</h2><div class='box'><table>{table}</table></div>",
            "<p><b>Contents</b></p>" + _list(list(SECTIONS[1:]), ""),
            f"<h2>{SECTIONS[1]}</h2>" + _list(cards, ""),
            _root(doc),
            f"<h2>{SECTIONS[3]}</h2>" + f"<p>{escape(b.analysis)}</p>",
            "<h2>Sources read</h2>" + _list(doc.sources_read, "Only the request itself."),
            "<h2>Sources skipped</h2>" + _list(doc.sources_missing, "None."),
            f"<h2>{SECTIONS[5]}</h2>" + _list(_facts(doc.requirement_lines), "None listed."),
            f"<h2>{SECTIONS[6]}</h2>"
            + _list(
                questions + [f"From the review: {q}" for q in doc.review_questions], "Nothing."
            ),
            f"<h2>{SECTIONS[7]}</h2>"
            + (_list(existing, "") if existing else f"<p>{escape(duplicate)}</p>"),
            "<h2>Summary of proposed tickets</h2>"
            + _list(
                numbered(
                    b, root_is_epic=root_is_epic, existing_epic=doc.existing_epic, card=doc.root
                ),
                "",
            ),
            ("<h2>Created in Jira</h2>" + _list(doc.created, "")) if doc.created else "",
            _hierarchy(
                b, root_is_epic, doc.existing_epic, created=bool(doc.created), card=doc.root
            ),
            f"<h2>{SECTIONS[9]}</h2>" + _list(risks, "None recorded."),
            f"<h2>{SECTIONS[10]}</h2>" + f"<p>{escape(doc.placement.replace('_', ' '))}</p>",
            f"<h2>{SECTIONS[11]}</h2>" + _list(coverage, "No listed requirement lines."),
        ]
    )


def _num(item: tuple[str, str]) -> int:
    return int(item[0]) if item[0].isdigit() else 0


def render_pdf(doc: BreakdownDocument) -> bytes:
    """The document as a paginated A4 PDF."""
    import fitz  # PyMuPDF — installed by the platform; imported where it is used

    story = fitz.Story(html=breakdown_html(doc), user_css=_CSS)
    buffer = io.BytesIO()
    writer = fitz.DocumentWriter(buffer)
    page = fitz.paper_rect("a4")
    area = page + (40, 40, -40, -40)
    more = True
    while more:
        device = writer.begin_page(page)
        more, _ = story.place(area)
        story.draw(device)
        writer.end_page()
    writer.close()
    return _number_pages(buffer.getvalue(), doc.issue_key)


def _number_pages(data: bytes, issue_key: str) -> bytes:
    """ "Page 2 of 7 · BGV-25 work breakdown" at the foot of every page."""
    import fitz

    pdf_doc = fitz.open(stream=data, filetype="pdf")
    total = pdf_doc.page_count
    for number, page in enumerate(pdf_doc, start=1):
        foot = fitz.Rect(40, page.rect.height - 30, page.rect.width - 40, page.rect.height - 15)
        page.insert_textbox(
            foot, f"Page {number} of {total} · {issue_key} work breakdown", fontsize=7, align=1
        )
    return pdf_doc.tobytes()


def file_name(issue_key: str, breakdown: WorkBreakdown) -> str:
    """Named by its content, so the same breakdown is never attached twice."""
    digest = hashlib.sha256(
        json.dumps(breakdown.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()[:8]
    return f"aetherion-breakdown-{issue_key}-{digest}.pdf"
