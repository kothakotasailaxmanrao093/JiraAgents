"""Questions are about what the ticket asks, not how engineering builds it
(2026-11-25: on KS-6 the senior reviewer kept Q1, Q4, Q5 — empty or long name,
which screens, the same on both — and dropped Q2, Q3, Q6 — which name field, a
deleted or inactive client, a failed lookup)."""

from __future__ import annotations

from review.prompt import SYSTEM_PROMPT


def test_the_rule_is_in_the_prompt() -> None:
    assert "ONLY WHAT THE TICKET ITSELF ASKS" in SYSTEM_PROMPT


def test_engineering_questions_are_named_as_not_to_ask() -> None:
    for words in (
        "which field, table, API or data source",
        "a lookup, request or service failing",
        "deleted",
        "inactive",
    ):
        assert words in SYSTEM_PROMPT, words


def test_the_ks6_example_keeps_the_right_questions() -> None:
    assert "the internal screen, the client-facing one" in SYSTEM_PROMPT
    assert "client lookup fails" in SYSTEM_PROMPT
