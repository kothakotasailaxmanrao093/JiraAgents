"""Accounting for what the review read, and what it could not.

The standard these tests hold the code to: **nothing is ever silently skipped**,
and every gap reads as an instruction rather than an apology. A thin review must
never be mistakable for a badly written ticket.
"""

from __future__ import annotations

from review.completeness import sources_missing, sources_read, summarise


def _doc(label: str, kind: str, text: str = "content") -> dict:
    return {"source_label": label, "kind": kind, "text": text}


# --- what was read -----------------------------------------------------------


def test_only_sources_that_contributed_text_are_claimed() -> None:
    """Built from the documents, not from what was requested, so it can never
    claim to have read something that failed."""
    docs = [
        _doc("Target ABC-1 (description)", "requirement"),
        _doc("Attachment empty.pdf (on ABC-1)", "attachment", text="   "),
    ]
    assert sources_read(docs) == ["Target ABC-1 (description)"]


def test_each_source_is_named_once() -> None:
    docs = [_doc("Target ABC-1 (description)", "requirement")] * 2
    assert sources_read(docs) == ["Target ABC-1 (description)"]


def test_nothing_read_is_an_empty_list_not_an_error() -> None:
    assert sources_read([]) == []


# --- what was missing --------------------------------------------------------


def test_a_ticket_with_no_description_is_asked_for_one() -> None:
    missing = sources_missing({"key": "FL-130"}, [], [])
    sentences = [m.as_sentence() for m in missing]
    assert any("FL-130 has no description" in s and "please describe" in s for s in sentences)


def test_a_ticket_with_no_acceptance_criteria_is_asked_for_them() -> None:
    missing = sources_missing(
        {"key": "FL-130", "description_text": "Let drivers log a break."}, [], []
    )
    sentences = [m.as_sentence() for m in missing]
    assert any("FL-130 has no acceptance criteria" in s for s in sentences)
    assert any("please add them" in s for s in sentences)


def test_acceptance_criteria_in_any_recognisable_form_count() -> None:
    """Deciding to tell someone off for not writing criteria — a false
    accusation is worse than a missed nudge, so the match is generous."""
    for description in (
        "Acceptance Criteria:\n- one",
        "AC: the reading is rejected when out of range",
        "Given a driver, when they log a break, then it is recorded",
        "Definition of done: the reading is stored",
    ):
        missing = sources_missing({"key": "FL-1", "description_text": description}, [], [])
        assert not any(
            "acceptance criteria" in m.what for m in missing
        ), f"falsely flagged: {description!r}"


def test_acceptance_criteria_inside_an_attached_spec_count() -> None:
    missing = sources_missing(
        {"key": "FL-1", "description_text": "See the attached spec."},
        [_doc("Attachment spec.pdf (on FL-1)", "attachment", "Acceptance criteria: ...")],
        [],
    )
    assert not any("acceptance criteria" in m.what for m in missing)


def test_a_requirement_that_lives_only_in_a_file_is_still_flagged_as_unreadable_on_the_card() -> (
    None
):
    """Not an error — but a human opening the ticket sees nothing, so say so."""
    missing = sources_missing(
        {"key": "FL-1", "description_text": ""},
        [_doc("Attachment spec.pdf (on FL-1)", "attachment", "The driver logs a break.")],
        [],
    )
    sentences = [m.as_sentence() for m in missing]
    assert any("no description of its own" in s for s in sentences)
    assert any("please summarise it on the ticket" in s for s in sentences)


def test_an_unreadable_attachment_gets_actionable_advice() -> None:
    missing = sources_missing(
        {"key": "FL-1", "description_text": "x", "": ""},
        [],
        ["spec.xlsx (attached to FL-1) could not be read: password-protected"],
    )
    sentence = next(m.as_sentence() for m in missing if "spec.xlsx" in m.what)
    assert "re-attach" in sentence


def test_an_inaccessible_confluence_page_gets_actionable_advice() -> None:
    missing = sources_missing(
        {"key": "FL-1", "description_text": "x"},
        [],
        ['The Confluence page "Depot Ops Runbook": not permitted to read that page'],
    )
    sentence = next(m.as_sentence() for m in missing if "Depot Ops Runbook" in m.what)
    assert "grant this account access" in sentence


def test_a_link_that_is_not_a_confluence_page_gets_actionable_advice() -> None:
    """Bug 6: a link in the ticket that is not a Confluence page on this site
    (a Google Doc, a different Atlassian site) used to vanish entirely — see
    shared.confluence_text.find_unread_links."""
    missing = sources_missing(
        {"key": "FL-1", "description_text": "x"},
        [],
        [
            "Link in the ticket could not be read (not a Confluence page on "
            "this site): https://docs.google.com/document/d/abc123"
        ],
    )
    sentence = next(m.as_sentence() for m in missing if "docs.google.com" in m.what)
    assert "paste the relevant section" in sentence
    assert "re-attach" not in sentence  # not the generic file-attachment advice


def test_an_unrecognised_warning_is_still_reported() -> None:
    """A new warning must never vanish just because the advice table has not
    caught up with it."""
    missing = sources_missing(
        {"key": "FL-1", "description_text": "x"}, [], ["something nobody has seen before"]
    )
    assert any("something nobody has seen before" in m.what for m in missing)


def test_a_capped_list_is_reported_with_what_to_do() -> None:
    missing = sources_missing(
        {"key": "FL-1", "description_text": "x"},
        [],
        ["Only the first 10 of 12 attachments on FL-1 were read (payload cap)."],
    )
    sentence = next(m.as_sentence() for m in missing if "payload cap" in m.what)
    assert "review the remainder separately" in sentence


def test_a_blank_warning_is_dropped_rather_than_shown_as_an_empty_bullet() -> None:
    missing = sources_missing({"key": "FL-1", "description_text": "x"}, [], ["", "   "])
    assert not any(m.what.strip() == "" for m in missing)


# --- the one-line summary ----------------------------------------------------


def test_the_summary_says_how_much_was_read() -> None:
    assert "2 source(s)" in summarise(["a", "b"], [])


def test_the_summary_says_when_nothing_could_be_read() -> None:
    assert summarise([], []) == "Nothing could be read for this ticket."
