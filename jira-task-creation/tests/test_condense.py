"""A requirement of any length fits the model: condensed, never refused (2026-10-06).

KS-17185 — a two-line ticket with an 18,000-character meeting transcript — was
turned away with "longer than the supported 20000 characters". These pin the
replacement: what fits is untouched, what does not is condensed by the model,
the request itself is kept word for word, and every step is said.
"""

from __future__ import annotations

import asyncio

import pytest

from src.classification.validate import validate_input
from src.context import condense
from src.context.condense import budget_chars, chunk, fit_requirement, split_sections
from src.models.schemas import ResultStatus

REQUEST = (
    "## Stated requirement (the description of KS-1)\n"
    "Parity with Classic: client users print reports in mass from the report dashboard."
)


def transcript(lines: int) -> str:
    return "\n".join(
        f"[00:{i // 60:02d}:{i % 60:02d}] Tyler: point {i} about bulk printing the latest PDF."
        for i in range(lines)
    )


def card(transcript_text: str, *, history: str = "") -> str:
    text = (
        f"{REQUEST}\n\n## Attached documents (supporting material)\n"
        f"### Attachment: 17185 Transcript.txt\n{transcript_text}"
    )
    if history:
        text += f"\n\n## Recent field history (context only)\n{history}"
    return text


class FakeChat:
    """Answers each part with a short list of points; records every call."""

    def __init__(self, fail: bool = False) -> None:
        self.prompts: list[str] = []
        self.fail = fail

    async def __call__(self, prompt: str, system_prompt: str) -> str:
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("gateway unreachable")
        assert "never invent" in system_prompt
        return "- [00:12:40 Tyler] Bulk print uses the latest full report PDF only."


def run(text: str, chat: FakeChat, budget: int) -> tuple[str, list[str]]:
    return asyncio.run(fit_requirement(text, chat=chat, budget=budget))


def test_a_card_within_the_budget_is_untouched_and_no_model_is_called() -> None:
    chat = FakeChat()
    text = card(transcript(10))
    assert run(text, chat, budget=60_000) == (text, [])
    assert chat.prompts == []


def test_a_long_transcript_is_condensed_and_the_request_kept_word_for_word() -> None:
    chat = FakeChat()
    text = card(transcript(600))  # ~43,000 characters, KS-17185 twice over
    fitted, notes = run(text, chat, budget=20_000)

    assert len(fitted) <= 20_000
    assert fitted.startswith(REQUEST), "the request itself is never condensed while it fits"
    assert "Bulk print uses the latest full report PDF only." in fitted
    assert "condensed from" in fitted, "the heading says the section was condensed"
    assert any("Attached documents" in n and "condensed by the model" in n for n in notes)
    # Read in parts, each with the request in view, none over the part size.
    assert len(chat.prompts) >= 2
    assert all("Parity with Classic" in p for p in chat.prompts)


def test_history_is_left_out_before_anything_is_condensed() -> None:
    chat = FakeChat()
    text = card(transcript(5), history="- status changed " * 2_000)
    fitted, notes = run(text, chat, budget=4_000)
    assert "Recent field history" not in fitted
    assert any("Recent field history" in n and "left out" in n for n in notes)
    assert chat.prompts == [], "dropping the history was enough; nothing needed condensing"


def test_a_model_outage_cuts_to_fit_instead_of_failing_the_run() -> None:
    chat = FakeChat(fail=True)
    fitted, notes = run(card(transcript(600)), chat, budget=20_000)
    assert len(fitted) <= 20_000
    assert fitted.startswith(REQUEST)
    assert any("cut to fit" in n and "unavailable" in n for n in notes)


def test_a_typed_requirement_that_is_itself_huge_is_condensed_last() -> None:
    # A manual run where the person pasted a whole transcript into the form.
    chat = FakeChat()
    typed = "Bulk print reports.\n" + transcript(900)
    fitted, notes = run(typed, chat, budget=10_000)
    assert len(fitted) <= 10_000
    assert "Bulk print uses the latest full report PDF only." in fitted
    assert any("the request" in n for n in notes)


def test_transcript_lines_are_never_cut_in_half() -> None:
    text = transcript(400)
    parts = chunk(text, size=2_000)
    assert len(parts) > 1
    assert all(len(p) <= 2_000 for p in parts)
    assert "".join(parts) == text
    assert all(p.startswith("[00:") for p in parts)


def test_one_enormous_line_is_still_split() -> None:
    parts = chunk("x" * 5_000, size=2_000)
    assert [len(p) for p in parts] == [2_000, 2_000, 1_000]


def test_sections_split_and_join_back() -> None:
    sections = split_sections(card(transcript(3)))
    assert [s.heading.split(" (")[0] for s in sections] == [
        "Stated requirement",
        "Attached documents",
    ]


def test_the_budget_is_configurable_and_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    assert budget_chars() == condense.DEFAULT_BUDGET_CHARS == 60_000
    monkeypatch.setenv("TASK_CONTEXT_MAX_CHARS", "90000")
    assert budget_chars() == 90_000
    monkeypatch.setenv("TASK_CONTEXT_MAX_CHARS", "5")
    assert budget_chars() == 1_000
    monkeypatch.setenv("TASK_CONTEXT_MAX_CHARS", "lots")
    assert budget_chars() == 60_000


def test_validation_no_longer_refuses_a_long_request() -> None:
    long_request = "Build a bulk print of the latest consumer report PDFs. " * 800  # ~44,000
    verdict = validate_input(long_request)
    assert "characters" not in (verdict.reason or "")
    assert verdict.status is not ResultStatus.VALIDATION_ERROR


def test_the_flow_skips_fitting_below_the_smallest_budget() -> None:
    from src.agent import agent

    assert agent.MIN_BUDGET_CHARS == condense.MIN_BUDGET_CHARS
