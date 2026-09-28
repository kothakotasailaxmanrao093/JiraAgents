"""Aggregate context payload budgeting."""

from __future__ import annotations

from context.budget import dropped_documents, trim_documents_to_budget


def _doc(label, kind, n):
    return {"source_label": label, "kind": kind, "text": "x" * n, "source_id": label}


def test_no_trim_when_under_budget():
    docs = [_doc("Target ABC-1 (description)", "requirement", 100)]
    kept, warning = trim_documents_to_budget(docs, budget=1000)
    assert kept == docs
    assert warning is None


def test_trims_lowest_priority_first_and_preserves_order():
    docs = [
        _doc("Target ABC-1 (description)", "requirement", 100),  # never dropped
        _doc("Subtask ABC-2 (comments)", "subtask_comments", 100),  # priority 4
        _doc("Meeting Transcript", "transcript", 100),  # outranks subtask comments
    ]
    kept, warning = trim_documents_to_budget(docs, budget=200)  # room for 2 of 3
    labels = [d["source_label"] for d in kept]
    assert "Target ABC-1 (description)" in labels
    assert "Meeting Transcript" in labels
    assert "Subtask ABC-2 (comments)" not in labels  # lowest priority dropped
    # original order preserved among kept docs
    assert labels.index("Target ABC-1 (description)") < labels.index("Meeting Transcript")
    assert warning is not None and "dropped 1" in warning


def test_always_keeps_target_description_under_pressure():
    docs = [
        _doc("Subtask ABC-2 (comments)", "subtask_comments", 100),
        _doc("Target ABC-1 (description)", "requirement", 100),
    ]
    kept, warning = trim_documents_to_budget(docs, budget=100)  # only room for one
    assert [d["source_label"] for d in kept] == ["Target ABC-1 (description)"]
    assert warning is not None


def test_linked_issues_outrank_target_comments_and_subtasks():
    # Regression check: "Target ... (linked issues)" starts with "Target" like the
    # requirement/comments docs. Ranking is on ``kind`` alone for exactly this
    # reason — the label prefix once put linked issues below subtask comments.
    docs = [
        _doc("Subtask ABC-2 (comments)", "subtask_comments", 100),
        _doc("Target ABC-1 (linked issues)", "linked_issues", 100),
        _doc("Target ABC-1 (comments)", "target_comments", 100),
    ]
    kept, _ = trim_documents_to_budget(docs, budget=200)  # room for 2 of 3
    labels = {d["source_label"] for d in kept}
    assert "Target ABC-1 (linked issues)" in labels
    assert "Target ABC-1 (comments)" in labels
    assert "Subtask ABC-2 (comments)" not in labels


# --- guarantees the new sources depend on -----------------------------------


def test_the_requirement_survives_even_when_it_alone_exceeds_the_budget():
    """A truncated requirement is reviewable; no requirement at all is not.

    Per-field caps have already bounded its size by this point, so admitting it
    over budget cannot be unbounded.
    """
    docs = [
        _doc("Target ABC-1 (description)", "requirement", 500),
        _doc("Subtask ABC-2 (comments)", "subtask_comments", 10),
    ]
    kept, warning = trim_documents_to_budget(docs, budget=100)
    assert [d["source_label"] for d in kept] == ["Target ABC-1 (description)"]
    assert warning is not None


def test_a_new_source_cannot_push_the_requirement_out():
    """The whole point of the rework: adding attachments and Confluence must not
    cost us the thing being reviewed."""
    docs = [
        _doc("Attachment spec.pdf", "attachment", 5000),
        _doc("Confluence: Spec v2", "confluence", 5000),
        _doc("Target ABC-1 (description)", "requirement", 100),
    ]
    kept, _ = trim_documents_to_budget(docs, budget=150)
    assert "Target ABC-1 (description)" in [d["source_label"] for d in kept]


def test_attachments_and_confluence_outrank_parent_and_subtask_context():
    """A ticket that says "see the attached spec" is worthless without the spec,
    whereas the parent card is only ever supporting colour."""
    docs = [
        _doc("Parent ABC-0 (description)", "parent_context", 100),
        _doc("Subtask ABC-2 (description)", "subtask_context", 100),
        _doc("Attachment spec.pdf", "attachment", 100),
        _doc("Confluence: Spec v2", "confluence", 100),
    ]
    kept, _ = trim_documents_to_budget(docs, budget=200)
    labels = {d["source_label"] for d in kept}
    assert labels == {"Attachment spec.pdf", "Confluence: Spec v2"}


def test_an_unregistered_kind_ranks_below_everything_known():
    """A source added without a PRIORITY row degrades quietly rather than
    outranking the requirement."""
    docs = [
        _doc("Something New", "not_in_the_table", 100),
        _doc("Subtask ABC-2 (comments)", "subtask_comments", 100),
    ]
    kept, _ = trim_documents_to_budget(docs, budget=100)
    assert [d["source_label"] for d in kept] == ["Subtask ABC-2 (comments)"]


def test_dropped_documents_names_what_was_left_out():
    """Completeness reporting has to say so — a document dropped for budget
    looks identical to one that never existed."""
    docs = [
        _doc("Target ABC-1 (description)", "requirement", 100),
        _doc("Subtask ABC-2 (comments)", "subtask_comments", 100),
    ]
    kept, _ = trim_documents_to_budget(docs, budget=100)
    dropped = dropped_documents(docs, kept)
    assert [d["source_label"] for d in dropped] == ["Subtask ABC-2 (comments)"]
