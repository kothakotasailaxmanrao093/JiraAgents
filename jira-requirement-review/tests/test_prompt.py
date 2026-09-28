"""Prompt assembly."""

from __future__ import annotations

from review.prompt import SYSTEM_PROMPT, build_user_message


def _docs():
    return [
        {"source_label": "Target ABC-1 (description)", "kind": "requirement", "text": "Build X"},
        {"source_label": "Parent ABC-0 (comments)", "kind": "comments", "text": "discussed scope"},
        {"source_label": "Meeting Transcript", "kind": "transcript", "text": "we agreed Y"},
    ]


def test_system_prompt_has_all_categories():
    for category in (
        "Assumption",
        "Open Question",
        "Requirement Gap",
        "Ambiguity",
        "Edge Case",
        "Missing Functional Detail",
        "Missing Technical Detail",
        "Missing Acceptance Criterion",
        "Dependency Question",
    ):
        assert category in SYSTEM_PROMPT


def test_system_prompt_has_guardrails():
    assert "evidence_source" in SYSTEM_PROMPT
    assert "MULTIPLE tickets" in SYSTEM_PROMPT  # transcript scope rule
    assert "DO NOT emit" in SYSTEM_PROMPT or "do not emit" in SYSTEM_PROMPT.lower()


def test_system_prompt_has_priority_and_readiness_schema():
    assert '"priority"' in SYSTEM_PROMPT
    assert '"overall_readiness"' in SYSTEM_PROMPT
    assert '"readiness_score"' in SYSTEM_PROMPT
    assert '"executive_summary"' in SYSTEM_PROMPT
    assert "needs_major_clarification" in SYSTEM_PROMPT


def test_user_message_labels_every_source():
    msg = build_user_message("ABC-1", "Summary text", _docs())
    assert "[SOURCE: Target ABC-1 (description)]" in msg
    assert "[SOURCE: Parent ABC-0 (comments)]" in msg
    assert "[SOURCE: Meeting Transcript]" in msg
    assert "Build X" in msg
    assert "ABC-1" in msg
    # the label-list section enumerates each label for citation
    assert msg.count("Target ABC-1 (description)") >= 2
