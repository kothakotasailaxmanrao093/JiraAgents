"""``blocks_to_doc`` — turning ``(heading, body)`` pairs into ADF.

Live bug, found by testing a real published reply: a headerless list body
(``("", options)`` — the shape ``router_only``'s HELP/AMBIGUOUS options use)
rendered as the raw Python list repr, e.g.
``['"@Aetherion build" — ...', '"@Aetherion review" — ...']``, instead of
bullets. Nothing exercised this function's writing direction with a
list-and-no-heading body before — compose()/router_only() are well tested,
but they only build the tuples; the ADF conversion happens one layer further
in, inside the ``post_reply`` activity, which this test now covers directly.
"""

from __future__ import annotations

from shared.adf import blocks_to_doc


def _bullet_texts(doc: dict) -> list[str]:
    for node in doc["content"]:
        if node["type"] == "bulletList":
            out = []
            for item in node["content"]:
                for para in item["content"]:
                    out.append("".join(t["text"] for t in para["content"]))
            return out
    return []


def test_a_headerless_list_body_becomes_bullets_not_a_python_repr() -> None:
    options = ['"@Aetherion build" — create issues', '"@Aetherion review" — assess it']
    doc = blocks_to_doc([("", options)])

    assert _bullet_texts(doc) == options
    # The exact failure mode this pins: a single paragraph node whose text IS
    # the stringified Python list, which is what the pre-fix code produced.
    paragraphs = [n for n in doc["content"] if n["type"] == "paragraph"]
    assert not any(
        "@Aetherion build" in str(p) and "@Aetherion review" in str(p) for p in paragraphs
    )


def test_a_titled_list_body_keeps_its_heading() -> None:
    doc = blocks_to_doc([("What I need to know", ["Which project?", "By when?"])])
    types = [n["type"] for n in doc["content"]]
    assert types == ["heading", "bulletList"]
    assert _bullet_texts(doc) == ["Which project?", "By when?"]


def test_an_empty_list_body_with_a_heading_renders_the_heading_alone() -> None:
    doc = blocks_to_doc([("Findings (0)", [])])
    assert doc["content"] == [
        {
            "type": "heading",
            "attrs": {"level": 3},
            "content": [{"type": "text", "text": "Findings (0)"}],
        }
    ]


def test_an_empty_list_body_with_no_heading_renders_nothing_for_that_block() -> None:
    doc = blocks_to_doc([("", [])])
    # Falls back to the "no content at all" placeholder.
    assert doc["content"] == [
        {"type": "paragraph", "content": [{"type": "text", "text": "No description provided."}]}
    ]


def test_a_heading_only_block_is_still_a_title() -> None:
    doc = blocks_to_doc([("AetherionAgent · Headline", "")])
    assert doc["content"][0]["type"] == "heading"


def test_a_body_only_string_block_is_still_a_footer() -> None:
    doc = blocks_to_doc([("", "— AetherionAgent · automated reply")])
    assert doc["content"][0]["type"] == "paragraph"


def test_a_heading_and_string_body_renders_both() -> None:
    doc = blocks_to_doc([("Handled by", "Jira Orchestration → Work Breakdown")])
    types = [n["type"] for n in doc["content"]]
    assert types == ["heading", "paragraph"]
