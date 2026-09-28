"""``_unreadable_notes`` — what the direct/manual-mode Jira reply says was
linked or attached but could not be read.

Bug 6's gap: a link *referenced* partway through a longer requirement (as
opposed to gate 0c's narrower "the request is only a link" case) used to
vanish silently once the rest of the requirement text was substantial enough
to pass validation. See jira-task-creation/tests/test_delegated_mode.py for
the same case on the routed contract's ``sources_missing``.
"""

from __future__ import annotations

from src.agent.agent import _unreadable_notes


def test_an_unreadable_attachment_is_named() -> None:
    notes = _unreadable_notes(
        {"attachments": [{"filename": "spec.xlsx", "text": "", "note": "password-protected"}]}
    )
    assert any("spec.xlsx" in n and "password-protected" in n for n in notes)


def test_an_unreadable_confluence_page_is_named() -> None:
    notes = _unreadable_notes(
        {"confluence_pages": [{"title": "Depot Ops Runbook", "text": "", "note": "no access"}]}
    )
    assert any("Depot Ops Runbook" in n and "no access" in n for n in notes)


def test_a_link_referenced_mid_requirement_is_named() -> None:
    notes = _unreadable_notes({"unread_links": ["https://example.com/policy"]})
    assert any("https://example.com/policy" in n for n in notes)


def test_a_readable_source_is_never_listed_as_unreadable() -> None:
    notes = _unreadable_notes(
        {
            "attachments": [{"filename": "spec.pdf", "text": "the actual spec text"}],
            "confluence_pages": [{"title": "Runbook", "text": "readable content"}],
        }
    )
    assert notes == []


def test_no_source_at_all_is_not_an_error() -> None:
    assert _unreadable_notes(None) == []
    assert _unreadable_notes({}) == []
