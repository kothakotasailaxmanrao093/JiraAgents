"""What the orchestrator does next on ONE ticket, given what it is already doing.

The router used to be stateless: every comment started its own child, blind to
the others. On BGV-32 a review was still running when "@Aetherion build" came
in, and neither said anything until it finished (2026-09-29). Now each ticket
carries its state — the job running on it, and the last review and build
results — and every request is sequenced against it:

=========================  ================================================
Asked, while …             The orchestrator
=========================  ================================================
the same job is running    says so, and starts nothing ("already reviewing")
a different job runs       queues it: waits for that job, then runs it
a review, ticket unchanged answers with the previous review — no model call
a build after a poor       pauses the build and lists the review's
review, ticket unchanged   questions (below ``min_readiness``)
a build after a good       builds, carrying the review's questions into the
review, ticket unchanged   "please confirm" list
=========================  ================================================

"Unchanged" is a fingerprint of what a review reads — summary, description and
attachment names — never the ticket's ``updated`` time, which the agent's own
replies and labels move. Pure: no I/O, no clock, no environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# The jobs the orchestrator sequences. A question is answered on the spot and
# changes nothing, so it is never queued behind anything.
TRACKED = ("BUILD", "REVIEW")

WORKING = {"BUILD": "Building", "REVIEW": "Reviewing"}
NOUN = {"BUILD": "build", "REVIEW": "review"}


class Next(str, Enum):
    RUN = "run"
    WAIT = "wait"  # another job is running on this ticket: queue behind it
    ALREADY_RUNNING = "already_running"
    REUSE_REVIEW = "reuse_review"
    BUILD_PAUSED = "build_paused"


@dataclass(frozen=True)
class Active:
    intent: str
    run_id: str
    started: str  # ISO time, stamped by the activity that claimed the ticket
    reply_id: str = ""


@dataclass(frozen=True)
class TicketState:
    active: Active | None = None
    # intent -> {"result": <AgentResult dict>, "fingerprint": ..., "finished": ...}
    last: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TicketState:
        data = data or {}
        raw = data.get("active")
        active = (
            Active(
                intent=str(raw.get("intent") or ""),
                run_id=str(raw.get("run_id") or ""),
                started=str(raw.get("started") or ""),
                reply_id=str(raw.get("reply_id") or ""),
            )
            if isinstance(raw, dict) and raw.get("intent")
            else None
        )
        last = data.get("last") if isinstance(data.get("last"), dict) else {}
        return cls(active=active, last=last)


@dataclass(frozen=True)
class Plan:
    next: Next
    # What the previous review on this unchanged ticket left open, handed to a
    # build so they reach "please confirm" instead of being asked again.
    review_questions: list[str] = field(default_factory=list)
    review_score: int = 0
    previous: dict[str, Any] | None = None  # the stored result being reused


def _last_review(state: TicketState, fingerprint: str) -> dict[str, Any] | None:
    """The previous review, only if the ticket has not changed since."""
    review = state.last.get("REVIEW")
    if not review or not fingerprint or review.get("fingerprint") != fingerprint:
        return None
    # Only a review that completed: a failed one is asked again, never reused.
    if (review.get("result") or {}).get("outcome") != "REVIEWED":
        return None
    return review


def _open_points(result: dict[str, Any], limit: int = 8) -> list[str]:
    """What the review left open: its questions, else its findings' wording."""
    questions = [str(q) for q in result.get("questions") or [] if str(q).strip()]
    if questions:
        return questions[:limit]
    findings = [
        str(f.get("description") or f.get("title") or "")
        for f in result.get("findings") or []
        if isinstance(f, dict)
    ]
    return [f for f in findings if f.strip()][:limit]


def plan_next(intent: str, state: TicketState, fingerprint: str, min_readiness: int) -> Plan:
    """The one decision: run now, wait, refuse as duplicate, or reuse."""
    if intent not in TRACKED:
        return Plan(Next.RUN)

    if state.active is not None:
        return Plan(Next.ALREADY_RUNNING if state.active.intent == intent else Next.WAIT)

    review = _last_review(state, fingerprint)
    if intent == "REVIEW":
        if review is not None:
            return Plan(Next.REUSE_REVIEW, previous=review.get("result"))
        return Plan(Next.RUN)

    if review is None:
        return Plan(Next.RUN)
    result = review.get("result") or {}
    score = int(result.get("readiness_score") or 0)
    questions = _open_points(result)
    if 0 < score < min_readiness:
        return Plan(Next.BUILD_PAUSED, review_questions=questions, review_score=score)
    return Plan(Next.RUN, review_questions=questions, review_score=score)


def clock(iso: str) -> str:
    """ "2026-09-29T10:00:12+00:00" -> "10:00 UTC" — for a person to read."""
    return f"{iso[11:16]} UTC" if len(iso) >= 16 else "earlier"


# What the card shows while a job runs, and after it — one status label at a
# time, so the board says what the agent is doing with each ticket.
STATUS_LABELS = (
    "aetherion-reviewing",
    "aetherion-reviewed",
    "aetherion-building",
    "aetherion-built",
    "aetherion-pdf-ready",
    "aetherion-needs-input",
)
RUNNING_LABEL = {"REVIEW": "aetherion-reviewing", "BUILD": "aetherion-building"}
FINISHED_LABEL = {
    "REVIEWED": "aetherion-reviewed",
    "CREATED": "aetherion-built",
    "ALREADY_EXISTS": "aetherion-built",
    "PLANNED": "aetherion-pdf-ready",
    "NEEDS_INFO": "aetherion-needs-input",
}
