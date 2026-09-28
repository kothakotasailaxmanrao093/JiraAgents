# GENERATED FILE — DO NOT EDIT.
# Mirrored from the canonical shared/ package by scripts/sync_shared.py.
# Edit shared/rubric.py at the repository root and re-run that script.
"""One definition of "a good ticket", shared by the agent that writes tickets
and the agent that reviews them.

Before this, the review's nine finding categories lived only in
``jira-requirement-review/src/review/models.py`` and the work-breakdown agent
had no rubric at all — the prompt asked for testable criteria and nothing
checked the answer. Two definitions drift; one cannot.

The split that makes the rule testable (D1, 2026-09-25):

* **AVOIDABLE** — the writing agent's fault. The input said it, or a rule for
  writing tickets forbids it. Target: zero.
* **UNAVOIDABLE** — genuinely absent from every source read. Must be *asked*,
  never invented.

Asking about a detail the input already gave is AVOIDABLE — exactly as bad as
inventing one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class FindingType(str, Enum):
    """The review's nine categories. Values are what reviews and reports show."""

    ASSUMPTION = "Assumption"
    OPEN_QUESTION = "Open Question"
    REQUIREMENT_GAP = "Requirement Gap"
    AMBIGUITY = "Ambiguity"
    EDGE_CASE = "Edge Case"
    MISSING_FUNCTIONAL_DETAIL = "Missing Functional Detail"
    MISSING_TECHNICAL_DETAIL = "Missing Technical Detail"
    MISSING_ACCEPTANCE_CRITERION = "Missing Acceptance Criterion"
    DEPENDENCY_QUESTION = "Dependency Question"


class Avoidability(str, Enum):
    AVOIDABLE = "avoidable"
    UNAVOIDABLE = "unavoidable"


@dataclass(frozen=True)
class Check:
    code: str
    avoidability: Avoidability
    meaning: str


# The ticket-writing checks. Each AVOIDABLE one is something the self-review
# gate regenerates to fix, filling it only from context already gathered.
CHECKS: tuple[Check, ...] = (
    Check(
        "UNTESTABLE_CRITERION",
        Avoidability.AVOIDABLE,
        "an acceptance or completion criterion nobody can check",
    ),
    Check(
        "STATED_DETAIL_MISSING",
        Avoidability.AVOIDABLE,
        "a detail the input states is absent from every ticket",
    ),
    Check("NO_ACTOR", Avoidability.AVOIDABLE, "a Story that names no user or role"),
    Check(
        "STATED_EDGE_CASE_MISSING",
        Avoidability.AVOIDABLE,
        "an error or edge case the input describes has no criterion",
    ),
    Check(
        "UNTRACED_SUBTASK",
        Avoidability.AVOIDABLE,
        "a Sub-task that traces to no acceptance criterion",
    ),
    Check(
        "BOILERPLATE",
        Avoidability.AVOIDABLE,
        "generic text that says nothing about this requirement",
    ),
    Check(
        "ASKED_WHAT_WAS_STATED",
        Avoidability.AVOIDABLE,
        "an open question the input already answers",
    ),
    Check(
        "ABSENT_EVERYWHERE",
        Avoidability.UNAVOIDABLE,
        "a decision no source makes — asked in the reply, never invented",
    ),
)

BY_CODE = {c.code: c for c in CHECKS}


# Phrases that fit any ticket and therefore describe none. The first three are
# what the heuristic fallback wrote (decompose.py) before creation from it was
# blocked; the rest are what the model wrote on live runs (BGV-88, BGV-92..102).
BOILERPLATE_PHRASES: tuple[str, ...] = (
    "is available to the intended users",
    "matches the rules agreed with the product owner",
    "behave as agreed rather than failing silently",
    "as per the completion criteria",
    "implemented and tested",
    "approved and finalized",
    "approved and finalised",
    "works as expected",
    "functions correctly",
)

_BOILERPLATE = re.compile("|".join(re.escape(p) for p in BOILERPLATE_PHRASES), re.IGNORECASE)


def boilerplate_in(text: str) -> str:
    """The boilerplate phrase in ``text``, or "" when there is none."""
    match = _BOILERPLATE.search(text or "")
    return match.group(0) if match else ""
