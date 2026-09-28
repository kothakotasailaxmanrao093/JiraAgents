"""De-duplication of findings."""

from __future__ import annotations

from review.dedup import dedupe
from review.models import Confidence, Finding, FindingType


def _f(ftype, desc, conf=Confidence.MEDIUM):
    return Finding(finding_type=ftype, description=desc, confidence=conf)


def test_collapses_exact_and_normalized_duplicates():
    items = [
        _f(FindingType.ASSUMPTION, "Assumes OAuth."),
        _f(FindingType.ASSUMPTION, "assumes oauth"),  # normalises to the same
        _f(FindingType.OPEN_QUESTION, "Assumes OAuth."),  # different type -> kept
    ]
    out = dedupe(items)
    assert len(out) == 2
    assert out[0].finding_type == FindingType.ASSUMPTION
    assert out[1].finding_type == FindingType.OPEN_QUESTION


def test_preserves_first_occurrence_order():
    items = [_f(FindingType.AMBIGUITY, "B"), _f(FindingType.AMBIGUITY, "A")]
    out = dedupe(items)
    assert [f.description for f in out] == ["B", "A"]


def test_empty():
    assert dedupe([]) == []
