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


# --------------------------------------------------------------------------
# Bug investigation (2026-10-22)
# --------------------------------------------------------------------------

_CONFIDENCE_WORDS = {"high", "medium", "low"}
_MAX_LIST = 8


def _text(value: Any, limit: int = 1200) -> str:
    return str(value or "").strip()[:limit]


def _code_paths(documents: list[dict[str, Any]]) -> dict[str, str]:
    """``<repo>/<path>`` for every source file actually read, keyed lowercase."""
    out: dict[str, str] = {}
    for doc in documents or []:
        if doc.get("kind") != "source_code":
            continue
        meta = doc.get("metadata") or {}
        full = f"{meta.get('repo', '')}/{meta.get('path', '')}".strip("/")
        if full:
            out[full.lower()] = full
    return out


def _grounded_path(claimed: str, read: dict[str, str]) -> str | None:
    """The read file a claimed path refers to, or None when none was read.

    Accepts the full ``repo/path``, the ``Code repo/path`` label, or the bare
    path inside a repository — a model often drops the repository part.
    """
    claimed = claimed.strip().removeprefix("Code ").strip().lower()
    if not claimed:
        return None
    if claimed in read:
        return read[claimed]
    for key, full in read.items():
        if key.endswith("/" + claimed) or key.endswith(claimed):
            return full
    return None


def parse_investigation(
    raw: dict[str, Any] | None, documents: list[dict[str, Any]]
) -> dict[str, Any]:
    """The investigation block, cleaned, with every affected file grounded.

    A file the model names but that was never read is dropped, not shown: an
    invented path in a bug report sends a developer to the wrong place.
    Returns {} when there is no investigation at all.
    """
    block = (raw or {}).get("investigation")
    if not isinstance(block, dict):
        return {}

    read = _code_paths(documents)
    files: list[dict[str, str]] = []
    dropped = 0
    for entry in block.get("affected_files") or []:
        path = entry.get("path") if isinstance(entry, dict) else entry
        why = entry.get("why") if isinstance(entry, dict) else ""
        grounded = _grounded_path(str(path or ""), read)
        if grounded is None:
            dropped += 1
            continue
        if all(f["path"] != grounded for f in files):
            files.append({"path": grounded, "why": _text(why, 300)})

    confidence = _text(block.get("root_cause_confidence"), 20).lower()
    if confidence not in _CONFIDENCE_WORDS:
        confidence = ""
    if confidence == "high" and not read:
        # The prompt forbids it; the code enforces it. No code read, no proof.
        confidence = "medium"

    out = {
        "actual": _text(block.get("actual"), 500),
        "expected": _text(block.get("expected"), 500),
        "root_cause": _text(block.get("root_cause")),
        "root_cause_confidence": confidence,
        "evidence": [_text(e, 400) for e in (block.get("evidence") or []) if _text(e)][:_MAX_LIST],
        "affected_files": files[:_MAX_LIST],
        "proposed_fix": _text(block.get("proposed_fix")),
        "missing_information": [
            _text(m, 400) for m in (block.get("missing_information") or []) if _text(m)
        ][:_MAX_LIST],
    }
    if dropped:
        out["files_dropped"] = dropped
    return out if any(v for k, v in out.items() if k != "files_dropped") else {}
