"""Tolerant parsing of raw LLM JSON into a validated ``Review``.

The LLM is instructed to return a strict schema, but we never trust it blindly:
we coerce common casing/synonym variations, drop rows we cannot make sense of
(rather than failing the whole batch), and log how many were dropped.
"""

from __future__ import annotations

import logging
from typing import Any

from .models import Confidence, Finding, FindingType, Priority, Readiness, ReadinessLevel, Review

logger = logging.getLogger(__name__)

_TYPE_ALIASES: dict[str, FindingType] = {
    "assumption": FindingType.ASSUMPTION,
    "assumptions": FindingType.ASSUMPTION,
    "open question": FindingType.OPEN_QUESTION,
    "open questions": FindingType.OPEN_QUESTION,
    "question": FindingType.OPEN_QUESTION,
    "questions": FindingType.OPEN_QUESTION,
    "requirement gap": FindingType.REQUIREMENT_GAP,
    "requirement gaps": FindingType.REQUIREMENT_GAP,
    "gap": FindingType.REQUIREMENT_GAP,
    "ambiguity": FindingType.AMBIGUITY,
    "ambiguities": FindingType.AMBIGUITY,
    "edge case": FindingType.EDGE_CASE,
    "edge cases": FindingType.EDGE_CASE,
    "missing functional detail": FindingType.MISSING_FUNCTIONAL_DETAIL,
    "missing functional details": FindingType.MISSING_FUNCTIONAL_DETAIL,
    "functional gap": FindingType.MISSING_FUNCTIONAL_DETAIL,
    "missing technical detail": FindingType.MISSING_TECHNICAL_DETAIL,
    "missing technical details": FindingType.MISSING_TECHNICAL_DETAIL,
    "technical gap": FindingType.MISSING_TECHNICAL_DETAIL,
    "missing acceptance criterion": FindingType.MISSING_ACCEPTANCE_CRITERION,
    "missing acceptance criteria": FindingType.MISSING_ACCEPTANCE_CRITERION,
    "acceptance criteria gap": FindingType.MISSING_ACCEPTANCE_CRITERION,
    "dependency question": FindingType.DEPENDENCY_QUESTION,
    "dependency questions": FindingType.DEPENDENCY_QUESTION,
    "dependency": FindingType.DEPENDENCY_QUESTION,
}

_CONFIDENCE_ALIASES: dict[str, Confidence] = {
    "high": Confidence.HIGH,
    "h": Confidence.HIGH,
    "medium": Confidence.MEDIUM,
    "med": Confidence.MEDIUM,
    "m": Confidence.MEDIUM,
    "moderate": Confidence.MEDIUM,
    "low": Confidence.LOW,
    "l": Confidence.LOW,
}

_PRIORITY_ALIASES: dict[str, Priority] = {
    "high": Priority.HIGH,
    "h": Priority.HIGH,
    "medium": Priority.MEDIUM,
    "med": Priority.MEDIUM,
    "m": Priority.MEDIUM,
    "moderate": Priority.MEDIUM,
    "low": Priority.LOW,
    "l": Priority.LOW,
}

_READINESS_ALIASES: dict[str, ReadinessLevel] = {
    "ready": ReadinessLevel.READY,
    "needs_minor_clarification": ReadinessLevel.NEEDS_MINOR_CLARIFICATION,
    "needs minor clarification": ReadinessLevel.NEEDS_MINOR_CLARIFICATION,
    "minor": ReadinessLevel.NEEDS_MINOR_CLARIFICATION,
    "needs_major_clarification": ReadinessLevel.NEEDS_MAJOR_CLARIFICATION,
    "needs major clarification": ReadinessLevel.NEEDS_MAJOR_CLARIFICATION,
    "major": ReadinessLevel.NEEDS_MAJOR_CLARIFICATION,
    "not_ready": ReadinessLevel.NOT_READY,
    "not ready": ReadinessLevel.NOT_READY,
}


def _coerce_type(value: Any) -> FindingType | None:
    if isinstance(value, FindingType):
        return value
    if not isinstance(value, str):
        return None
    return _TYPE_ALIASES.get(value.strip().lower())


def _coerce_confidence(value: Any) -> Confidence:
    if isinstance(value, Confidence):
        return value
    if isinstance(value, str):
        return _CONFIDENCE_ALIASES.get(value.strip().lower(), Confidence.MEDIUM)
    return Confidence.MEDIUM


def _coerce_priority(value: Any) -> Priority | None:
    """Unlike confidence, an unrecognized/absent priority stays ``None`` rather
    than defaulting — most finding types legitimately have no priority."""
    if isinstance(value, Priority):
        return value
    if isinstance(value, str):
        return _PRIORITY_ALIASES.get(value.strip().lower())
    return None


def _coerce_readiness_level(value: Any) -> ReadinessLevel | None:
    if isinstance(value, ReadinessLevel):
        return value
    if isinstance(value, str):
        return _READINESS_ALIASES.get(value.strip().lower().replace("-", "_"))
    return None


def parse_findings(raw: Any) -> Review:
    """Coerce a raw LLM JSON object (``{"findings": [...]}``) into a ``Review``.

    Invalid rows (unknown type, empty description, non-dict) are dropped, not
    fatal. Always returns a ``Review`` (possibly empty).
    """
    items = raw.get("findings") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        logger.warning(
            "LLM response had no 'findings' list (got %s)",
            list(raw) if isinstance(raw, dict) else type(raw).__name__,
        )
        return Review(findings=[])

    findings: list[Finding] = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        ftype = _coerce_type(item.get("finding_type") or item.get("type"))
        description = item.get("description") or item.get("text") or ""
        if ftype is None or not isinstance(description, str) or not description.strip():
            dropped += 1
            continue
        findings.append(
            Finding(
                finding_type=ftype,
                description=description,
                evidence_source=item.get("evidence_source") or item.get("evidence") or "",
                confidence=_coerce_confidence(item.get("confidence")),
                priority=_coerce_priority(item.get("priority")),
            )
        )

    if dropped:
        logger.info("parse_findings dropped %d invalid finding(s)", dropped)
    return Review(findings=findings)


def parse_readiness(raw: Any) -> Readiness | None:
    """Coerce the top-level readiness fields of a raw LLM JSON object into a
    ``Readiness``. Returns ``None`` when no valid level is present — a missing or
    unparsable readiness must not be fabricated as one of the real levels."""
    if not isinstance(raw, dict):
        return None

    level = _coerce_readiness_level(
        raw.get("overall_readiness") or raw.get("readiness") or raw.get("level")
    )
    if level is None:
        return None

    score_raw = raw.get("readiness_score", raw.get("score"))
    try:
        score = int(score_raw)
    except (TypeError, ValueError):
        score = 3
    score = max(1, min(5, score))

    summary = raw.get("executive_summary") or raw.get("summary") or ""
    if not isinstance(summary, str):
        summary = ""

    return Readiness(level=level, score=score, executive_summary=summary)
