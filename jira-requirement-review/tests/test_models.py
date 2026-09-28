"""Finding/Review models + tolerant parser."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from review.models import Confidence, Finding, FindingType, Priority, ReadinessLevel, Review
from review.parser import parse_findings, parse_readiness


def test_finding_strips_and_defaults():
    f = Finding(finding_type=FindingType.ASSUMPTION, description="  hi  ")
    assert f.description == "hi"
    assert f.confidence == Confidence.MEDIUM
    assert f.evidence_source == ""


def test_finding_rejects_empty_description():
    with pytest.raises(ValidationError):
        Finding(finding_type=FindingType.ASSUMPTION, description="   ")


def test_parse_coerces_casing_and_synonyms():
    raw = {
        "findings": [
            {"finding_type": "ASSUMPTIONS", "description": "x", "confidence": "HIGH"},
            {"type": "gap", "text": "y", "confidence": "med"},
        ]
    }
    review = parse_findings(raw)
    assert len(review.findings) == 2
    assert review.findings[0].finding_type == FindingType.ASSUMPTION
    assert review.findings[0].confidence == Confidence.HIGH
    assert review.findings[1].finding_type == FindingType.REQUIREMENT_GAP
    assert review.findings[1].confidence == Confidence.MEDIUM  # 'med' alias


def test_parse_drops_invalid_rows():
    raw = {
        "findings": [
            {"finding_type": "Nonsense", "description": "x"},
            {"finding_type": "Assumption", "description": ""},
            "not a dict",
            {"finding_type": "Ambiguity", "description": "ok"},
        ]
    }
    review = parse_findings(raw)
    assert [f.finding_type for f in review.findings] == [FindingType.AMBIGUITY]


def test_parse_missing_findings_list():
    assert parse_findings({}).findings == []
    assert parse_findings({"foo": 1}).findings == []
    assert parse_findings("garbage").findings == []


def test_grouped_sorted_and_nonempty_types():
    review = parse_findings(
        {
            "findings": [
                {"finding_type": "Open Question", "description": "q1", "confidence": "low"},
                {"finding_type": "Open Question", "description": "q2", "confidence": "high"},
            ]
        }
    )
    grouped = review.grouped()
    assert len(grouped[FindingType.OPEN_QUESTION]) == 2
    # high confidence sorts before low
    assert grouped[FindingType.OPEN_QUESTION][0].description == "q2"
    assert review.non_empty_types() == [FindingType.OPEN_QUESTION]


def test_empty_review():
    assert Review().is_empty()
    assert Review().non_empty_types() == []


def test_finding_priority_defaults_to_none():
    f = Finding(finding_type=FindingType.AMBIGUITY, description="x")
    assert f.priority is None


def test_new_finding_types_and_priority_alias():
    raw = {
        "findings": [
            {
                "finding_type": "Dependency Question",
                "description": "Does ABC-9 land first?",
                "priority": "HIGH",
            },
            {"finding_type": "edge cases", "description": "empty input"},
            {"finding_type": "missing acceptance criteria", "description": "no AC for retries"},
        ]
    }
    review = parse_findings(raw)
    types = [f.finding_type for f in review.findings]
    assert types == [
        FindingType.DEPENDENCY_QUESTION,
        FindingType.EDGE_CASE,
        FindingType.MISSING_ACCEPTANCE_CRITERION,
    ]
    assert review.findings[0].priority == Priority.HIGH
    # priority is only ever set when the LLM supplied one — no forced default
    assert review.findings[1].priority is None


def test_grouped_sorts_high_priority_first_within_confidence():
    review = parse_findings(
        {
            "findings": [
                {"finding_type": "Ambiguity", "description": "low one", "priority": "low"},
                {"finding_type": "Ambiguity", "description": "high one", "priority": "high"},
                {"finding_type": "Ambiguity", "description": "no priority"},
            ]
        }
    )
    grouped = review.grouped()[FindingType.AMBIGUITY]
    assert grouped[0].description == "high one"


def test_parse_readiness_valid():
    raw = {
        "overall_readiness": "Needs_Major_Clarification",
        "readiness_score": 2,
        "executive_summary": "  Missing error handling.  ",
    }
    readiness = parse_readiness(raw)
    assert readiness.level == ReadinessLevel.NEEDS_MAJOR_CLARIFICATION
    assert readiness.score == 2
    assert readiness.executive_summary == "Missing error handling."


def test_parse_readiness_clamps_score():
    readiness = parse_readiness({"overall_readiness": "ready", "readiness_score": 99})
    assert readiness.score == 5
    readiness = parse_readiness({"overall_readiness": "ready", "readiness_score": 0})
    assert readiness.score == 1


def test_parse_readiness_missing_or_invalid_level_is_none():
    assert parse_readiness({}) is None
    assert parse_readiness({"overall_readiness": "not_a_real_level"}) is None
    assert parse_readiness("garbage") is None
    assert parse_readiness(None) is None
