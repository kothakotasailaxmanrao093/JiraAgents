"""markdown_to_adf — the human-edited comment path."""

from __future__ import annotations

from jira.http import adf_to_text
from review.adf import markdown_to_adf


def test_headings():
    d = markdown_to_adf("## Title\n### Sub")
    headings = {
        (n["attrs"]["level"], n["content"][0]["text"])
        for n in d["content"]
        if n["type"] == "heading"
    }
    assert (2, "Title") in headings
    assert (3, "Sub") in headings


def test_bullets_with_nesting():
    md = "- top item\n  - child a\n  - child b\n- second"
    d = markdown_to_adf(md)
    lists = [n for n in d["content"] if n["type"] == "bulletList"]
    assert len(lists) == 1
    items = lists[0]["content"]
    assert len(items) == 2  # two top-level items
    assert any(c["type"] == "bulletList" for c in items[0]["content"])  # first has children
    text = adf_to_text(d)
    for fragment in ("top item", "child a", "child b", "second"):
        assert fragment in text


def test_inline_bold():
    d = markdown_to_adf("This is **bold** text")
    runs = d["content"][0]["content"]
    assert any(r.get("marks") == [{"type": "strong"}] and r["text"] == "bold" for r in runs)


def test_horizontal_rule():
    d = markdown_to_adf("para one\n\n---\n\npara two")
    assert any(n["type"] == "rule" for n in d["content"])


def test_footer_appended():
    d = markdown_to_adf("hello", footer="footer text")
    assert any(n["type"] == "rule" for n in d["content"])
    assert "footer text" in adf_to_text(d)


def test_empty_input_yields_doc():
    d = markdown_to_adf("")
    assert d["type"] == "doc"
    assert d["content"]  # at least one (empty) paragraph
