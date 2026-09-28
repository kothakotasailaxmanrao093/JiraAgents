"""Flattening Jira's ADF into text without losing the structure.

Found in a live run: a bullet list typed in Jira's editor carries no "-"
characters of its own — the markers are structure, not text. The old flattener
joined every text node in the document with a single space, so

    Please add:
      • driver shift roster
      • fuel spend by depot
      • tyre replacement log

reached the agent as one sentence, was read as one capability, and produced one
Story instead of three.
"""

from __future__ import annotations

from src.jira.api import _plain_text


def para(*texts: str) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": t} for t in texts]}


def item(text: str) -> dict:
    return {"type": "listItem", "content": [para(text)]}


def doc(*content: dict) -> dict:
    return {"type": "doc", "version": 1, "content": list(content)}


def test_a_bullet_list_keeps_one_item_per_line() -> None:
    """The exact comment from the live run that produced one Story, not three."""
    adf = doc(
        para("@Aetherion Please add:"),
        {
            "type": "bulletList",
            "content": [
                item("driver shift roster"),
                item("fuel spend by depot"),
                item("tyre replacement log"),
            ],
        },
    )
    assert _plain_text(adf) == (
        "@Aetherion Please add:\n"
        "- driver shift roster\n"
        "- fuel spend by depot\n"
        "- tyre replacement log"
    )


def test_a_numbered_list_keeps_its_numbers() -> None:
    adf = doc(
        para("Please add:"),
        {
            "type": "orderedList",
            "content": [item("schedule a service"), item("report a fault")],
        },
    )
    assert _plain_text(adf) == "Please add:\n1. schedule a service\n2. report a fault"


def test_formatting_does_not_insert_extra_spaces() -> None:
    """ADF splits a sentence at every formatting change.

    "Allow **drivers** to log" arrives as three text nodes whose spaces are
    already inside them; joining on a space gave "Allow  drivers  to log".
    """
    adf = doc(
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Allow "},
                {"type": "text", "text": "drivers", "marks": [{"type": "strong"}]},
                {"type": "text", "text": " to log a break."},
            ],
        }
    )
    assert _plain_text(adf) == "Allow drivers to log a break."


def test_separate_paragraphs_stay_separate() -> None:
    assert _plain_text(doc(para("First capability."), para("Second capability."))) == (
        "First capability.\nSecond capability."
    )


def test_a_hard_break_is_a_line_break() -> None:
    adf = doc(
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "line one"},
                {"type": "hardBreak"},
                {"type": "text", "text": "line two"},
            ],
        }
    )
    assert _plain_text(adf) == "line one\nline two"


def test_a_link_jira_turned_into_a_card_is_still_visible() -> None:
    """Otherwise a Confluence page pasted as a smart link becomes invisible."""
    url = "https://example.atlassian.net/wiki/spaces/FL/pages/1/A"
    adf = doc({"type": "paragraph", "content": [{"type": "inlineCard", "attrs": {"url": url}}]})
    assert url in _plain_text(adf)


def test_a_mention_keeps_its_display_text() -> None:
    adf = doc(
        {
            "type": "paragraph",
            "content": [
                {"type": "mention", "attrs": {"id": "1", "text": "@Aetherion"}},
                {"type": "text", "text": " Allow drivers to log a break."},
            ],
        }
    )
    assert _plain_text(adf) == "@Aetherion Allow drivers to log a break."


def test_a_table_row_reads_across() -> None:
    def cell(text: str) -> dict:
        return {"type": "tableCell", "content": [para(text)]}

    adf = doc(
        {
            "type": "table",
            "content": [
                {"type": "tableRow", "content": [cell("Feature"), cell("Owner")]},
                {"type": "tableRow", "content": [cell("Breaks"), cell("Ops")]},
            ],
        }
    )
    assert _plain_text(adf) == "Feature | Owner\nBreaks | Ops"


def test_plain_strings_and_empty_documents_are_unchanged() -> None:
    assert _plain_text("just a string") == "just a string"
    assert _plain_text(None) == ""
    assert _plain_text({}) == ""
    assert _plain_text(doc()) == ""


def test_a_bullet_list_becomes_one_story_per_bullet() -> None:
    """The end of the chain: the split the live run got wrong."""
    from src.classification.decompose import split_capabilities

    adf = doc(
        para("Please add:"),
        {
            "type": "bulletList",
            "content": [
                item("driver shift roster"),
                item("fuel spend by depot"),
                item("tyre replacement log"),
            ],
        },
    )
    assert len(split_capabilities(_plain_text(adf))) == 3
