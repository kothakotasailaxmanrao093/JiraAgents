"""Domain models for a requirement review.

A ``Review`` is a list of ``Finding``s. Each finding has a type (one of the six
categories the business asked for), a description, the evidence source it was
derived from, and a confidence level. Pydantic gives us validation/serialisation
for free — important because the findings originate from an LLM.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator

# One definition, shared with the agent that writes tickets (shared/rubric.py).
from shared.rubric import FindingType as FindingType


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Priority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReadinessLevel(str, Enum):
    READY = "ready"
    NEEDS_MINOR_CLARIFICATION = "needs_minor_clarification"
    NEEDS_MAJOR_CLARIFICATION = "needs_major_clarification"
    NOT_READY = "not_ready"


# Display / sort order used by the renderers.
FINDING_TYPE_ORDER: list[FindingType] = [
    FindingType.ASSUMPTION,
    FindingType.OPEN_QUESTION,
    FindingType.REQUIREMENT_GAP,
    FindingType.AMBIGUITY,
    FindingType.EDGE_CASE,
    FindingType.MISSING_FUNCTIONAL_DETAIL,
    FindingType.MISSING_TECHNICAL_DETAIL,
    FindingType.MISSING_ACCEPTANCE_CRITERION,
    FindingType.DEPENDENCY_QUESTION,
]

_CONFIDENCE_RANK: dict[Confidence, int] = {
    Confidence.HIGH: 0,
    Confidence.MEDIUM: 1,
    Confidence.LOW: 2,
}

_PRIORITY_RANK: dict[Priority, int] = {
    Priority.HIGH: 0,
    Priority.MEDIUM: 1,
    Priority.LOW: 2,
}

# Worst-first rank for rolling up readiness across a batch of issues.
READINESS_RANK: dict[ReadinessLevel, int] = {
    ReadinessLevel.NOT_READY: 0,
    ReadinessLevel.NEEDS_MAJOR_CLARIFICATION: 1,
    ReadinessLevel.NEEDS_MINOR_CLARIFICATION: 2,
    ReadinessLevel.READY: 3,
}


class Finding(BaseModel):
    finding_type: FindingType
    description: str = Field(min_length=1)
    evidence_source: str = ""
    confidence: Confidence = Confidence.MEDIUM
    # Only meaningful for Ambiguity / Dependency Question; null elsewhere.
    priority: Priority | None = None

    @field_validator("description", "evidence_source", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class Readiness(BaseModel):
    """Per-issue implementation-readiness assessment (separate from the findings list)."""

    level: ReadinessLevel
    score: int = Field(ge=1, le=5, default=3)
    executive_summary: str = ""

    @field_validator("executive_summary", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class Review(BaseModel):
    findings: list[Finding] = Field(default_factory=list)

    def grouped(self) -> dict[FindingType, list[Finding]]:
        """Group findings by type, sorting each group by priority, confidence, then description."""
        groups: dict[FindingType, list[Finding]] = {}
        for f in self.findings:
            groups.setdefault(f.finding_type, []).append(f)
        for ft in groups:
            groups[ft].sort(
                key=lambda x: (
                    _PRIORITY_RANK[x.priority] if x.priority is not None else 1,
                    _CONFIDENCE_RANK[x.confidence],
                    x.description.lower(),
                )
            )
        return groups

    def non_empty_types(self) -> list[FindingType]:
        """Finding types that actually have findings, in canonical display order."""
        groups = self.grouped()
        return [ft for ft in FINDING_TYPE_ORDER if groups.get(ft)]

    def is_empty(self) -> bool:
        return not self.findings
