"""Attachment text extraction and its caps."""

from __future__ import annotations

import pytest

from src.context import attachments
from src.shared import attachments as shared_attachments


async def test_csv_is_extracted() -> None:
    result = await attachments.extract(b"name,role\nAnn,PM\n", "people.csv")
    assert result.usable
    assert "Ann" in result.text
    assert result.note == ""


async def test_plain_text_is_extracted() -> None:
    result = await attachments.extract(b"just some notes", "notes.txt")
    assert result.text == "just some notes"


async def test_unsupported_type_is_reported_not_raised() -> None:
    result = await attachments.extract(b"\x00binary", "installer.exe")
    assert not result.usable
    assert "Unsupported attachment type" in result.note


async def test_images_are_read_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A screenshot is often the only place the requirement exists."""
    monkeypatch.delenv("LTW_ATTACHMENT_READ_IMAGES", raising=False)
    assert attachments.supported("screen.png")


async def test_images_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each image costs a vision call, so a slow project can opt out."""
    monkeypatch.setenv("LTW_ATTACHMENT_READ_IMAGES", "false")
    assert not attachments.supported("screen.png")

    result = await attachments.extract(b"\x89PNG", "screen.png")
    assert not result.usable
    assert "LTW_ATTACHMENT_READ_IMAGES" in result.note


async def test_oversized_files_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTW_ATTACHMENT_MAX_BYTES", "10")
    result = await attachments.extract(b"a,b\n" * 100, "big.csv")
    assert not result.usable
    assert "over the" in result.note


async def test_long_text_is_truncated_with_a_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTW_ATTACHMENT_MAX_CHARS", "20")
    result = await attachments.extract(b"x" * 500, "long.txt")
    assert len(result.text) == 20
    assert "Truncated" in result.note


async def test_extraction_failure_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_data: bytes, _name: str) -> str:
        raise RuntimeError("corrupt file")

    # The extractor itself now lives in the shared engine; this agent only
    # supplies the caps. Patch it where it is defined.
    monkeypatch.setattr(shared_attachments, "_extract_sync", boom)
    result = await attachments.extract(b"%PDF-1.4", "broken.pdf")
    assert not result.usable
    assert "corrupt file" in result.note


async def test_empty_extraction_is_reported() -> None:
    result = await attachments.extract(b"   ", "blank.txt")
    assert not result.usable
    assert "No text content" in result.note


def test_size_and_count_caps_fall_back_on_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTW_ATTACHMENT_MAX_FILES", "not-a-number")
    assert attachments.max_files() == attachments.DEFAULT_MAX_FILES
    monkeypatch.setenv("LTW_ATTACHMENT_MAX_FILES", "3")
    assert attachments.max_files() == 3


# --- JSON attachments -------------------------------------------------------
# A requirement exported from another tool arrives as .json more often than as
# a document, and the agent used to reject it as an unsupported type.


async def test_a_json_requirement_is_read() -> None:
    raw = b'{"requirement": "Let drivers log a break", "max_minutes": 30}'
    result = await attachments.extract(raw, "requirement.json")
    assert "Let drivers log a break" in result.text
    assert not result.note


async def test_a_one_line_json_export_is_made_readable() -> None:
    """Re-dumped with indentation, so the structure survives into the prompt."""
    result = await attachments.extract(b'{"a":{"b":1}}', "export.json")
    assert "\n" in result.text


async def test_json_lines_are_read_one_object_at_a_time() -> None:
    result = await attachments.extract(b'{"a": 1}\n{"b": 2}', "rows.jsonl")
    assert '"a": 1' in result.text
    assert '"b": 2' in result.text


async def test_a_corrupt_json_file_is_reported_not_silently_truncated() -> None:
    """A fragment that looks like content is worse than saying it is unreadable."""
    result = await attachments.extract(b'{"broken": ', "bad.json")
    assert not result.text
    assert "not valid JSON" in result.note


async def test_a_partly_readable_json_export_keeps_what_parsed() -> None:
    result = await attachments.extract(b'{"a": 1}\nnot json at all', "half.jsonl")
    assert '"a": 1' in result.text


# --- long documents keep both ends ------------------------------------------
# Truncation kept the opening and dropped the scope, the limits and the
# acceptance criteria, which is the half that decides what gets built.


def test_a_short_document_is_left_alone() -> None:
    text, note = attachments.fit_to_budget("short enough", 1000)
    assert text == "short enough"
    assert not note


def test_a_long_document_keeps_its_opening_and_its_ending() -> None:
    paragraphs = [f"Paragraph {i}: " + "x" * 200 for i in range(60)]
    doc = "\n\n".join(paragraphs)

    text, note = attachments.fit_to_budget(doc, 3000)

    assert len(text) <= 3000
    assert text.startswith("Paragraph 0:")
    assert paragraphs[-1] in text
    assert "omitted from the middle" in text
    assert "left out" in note


def test_the_gap_is_visible_in_the_text_itself() -> None:
    """The model must see that something is missing, not read a fragment as whole."""
    doc = "\n\n".join("y" * 300 for _ in range(50))
    text, _ = attachments.fit_to_budget(doc, 2000)
    assert "characters omitted from the middle" in text


def test_a_budget_too_small_to_split_falls_back_to_truncation() -> None:
    doc = "z" * 5000
    text, note = attachments.fit_to_budget(doc, 100)
    assert len(text) == 100
    assert "Truncated to the first" in note


def test_a_document_with_no_paragraph_breaks_still_fits() -> None:
    """One unbroken block cannot be split on blank lines; it must still obey the cap."""
    text, note = attachments.fit_to_budget("q" * 9000, 1000)
    assert len(text) <= 1000
    assert note
