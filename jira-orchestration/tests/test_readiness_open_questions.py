"""5/5 only when nothing is open (2026-11-24). The review is told not to ask
again what the card's memory holds as open, so it can find nothing NEW and
score 5/5 while earlier questions are unanswered (KS-6: 5/5 with 6 open)."""

from __future__ import annotations

import pytest

from routing import pipeline


@pytest.mark.parametrize(
    "score,still_open,expected",
    [(5, 0, 5), (5, 1, 4), (5, 2, 4), (5, 3, 3), (5, 6, 3), (2, 6, 2), (4, 1, 4), (0, 3, 0)],
)
def test_open_questions_lower_the_score(score, still_open, expected) -> None:
    assert pipeline.readiness_with_open(score, still_open) == expected


def test_ks6_is_not_ready_with_six_open() -> None:
    """The default 4/5 is needed to design: six open questions hold it back."""
    assert pipeline.readiness_with_open(5, 6) < 4


def test_open_count_counts_unanswered_only() -> None:
    questions = [
        {"id": "Q1", "answers": [{"text": "yes"}]},
        {"id": "Q2", "answers": []},
        {"id": "Q3"},
    ]
    assert pipeline.open_count(questions) == 2
    assert pipeline.open_count(None) == 0
