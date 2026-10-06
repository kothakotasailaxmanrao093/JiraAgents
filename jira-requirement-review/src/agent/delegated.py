"""Delegated mode — the agent as a pure function for the router to call.

The existing entry points are unchanged: a person triggers a review from the UI,
the CLI or the API, and gets a draft back. This adds a second way in, for when
the router has received a Jira comment and decided this agent should answer it.

Three rules, and the reasons they are rules:

1. **It writes nothing to Jira.** The router posts exactly one comment per user
   comment; a child that also posts makes "exactly once" a promise three
   services have to keep instead of a structural fact. ``post_to_jira`` is
   forced off here regardless of configuration — a webhook is an unsolicited
   trigger, and it must not turn a review tool into an auto-commenter.

2. **It returns the shared result contract**, so the router can compose a reply
   without knowing which agent produced it.

3. **It accounts for everything**, via ``sources_read`` and ``sources_missing``.
   Both are present on every run, even when empty. A thin review with no account
   of what was read is indistinguishable from a badly written ticket, and in
   practice the ticket gets blamed.

Nothing here duplicates the review itself: it calls the same ``run_review`` flow
the direct path uses and translates its result. That is what keeps the two modes
from drifting — there is one implementation and one mode flag, not two paths.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from config import AGENT_DISPLAY_NAME, AGENT_NAME, agent_version
from review.completeness import sources_missing, sources_read, summarise
from shared.contract import AgentResult, EmailKind, MissingSource, Outcome

logger = logging.getLogger(__name__)

Execute = Callable[..., Awaitable[dict]]

# Readiness levels that mean "this is not ready, and here is what is missing".
# Used only to pick the headline; the findings themselves carry the detail.
_NEEDS_WORK = {"not_ready", "needs_major_clarification"}


def _headline(readiness: str | None, finding_count: int) -> str:
    if readiness in _NEEDS_WORK:
        return "Requirement review — significant gaps found"
    if finding_count:
        return "Requirement review — nothing created"
    return "Requirement review — no gaps found"


_LEVEL_WORDS = {
    "ready": "Ready",
    "needs_minor_clarification": "Needs minor clarification",
    "needs_major_clarification": "Needs major clarification",
    "not_ready": "Not ready",
}


def _readiness_line(readiness: Any) -> str:
    """ "Needs minor clarification (4/5). <the model's executive summary>"

    The level, score and summary live INSIDE ``readiness``. This used to read
    ``result["executive_summary"]``, which never exists, so every live review
    showed "Read 3 source(s)." under Readiness and no score at all (BGV-11,
    2026-09-24).
    """
    if not isinstance(readiness, dict) or not readiness.get("level"):
        return ""
    level = _LEVEL_WORDS.get(str(readiness["level"]), str(readiness["level"]))
    score = readiness.get("score")
    head = f"{level} ({score}/5)." if score else f"{level}."
    summary = str(readiness.get("executive_summary") or "").strip()
    return f"{head} {summary}".strip()


def _outcome_for(result: dict[str, Any]) -> Outcome:
    """Map one issue's review result onto the contract's closed outcome set.

    A review never creates anything and never asks the router to treat it like
    a build clarification, so the only outcomes it can produce are REVIEWED and
    FAILED. NEEDS_INFO belongs exclusively to the work-breakdown agent's "too
    thin to build" path — a review that finds nothing to read is still a
    *completed* review; the gap is reported as a finding (see "What was
    missing or unreadable"), not as a request for more information before the
    agent can act.
    """
    status = result.get("status")
    if status == "error":
        # "no requirement text" is not a crash — it is a finding about the
        # ticket, reported the same way any other unreadable source is.
        if result.get("error") == "no_requirement_text":
            return Outcome.REVIEWED
        return Outcome.FAILED
    return Outcome.REVIEWED


async def run_delegated(
    payload: dict[str, Any],
    execute: Execute,
    *,
    run_review: Callable[..., Awaitable[list[dict]]],
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Review one issue for the router and return the result contract as a dict.

    ``run_review`` is injected so this stays testable without the SDK, and so
    there is visibly one review implementation shared by both modes.
    """
    issue_key = str(payload.get("issue_key") or "").strip()
    run_id = str(payload.get("run_id") or "").strip()
    comment_id = str(payload.get("comment_id") or "").strip()

    def _result(**over: Any) -> dict[str, Any]:
        base: dict[str, Any] = dict(
            produced_by=AGENT_NAME,
            display_name=AGENT_DISPLAY_NAME,
            agent_version=agent_version(),
            run_id=run_id,
            outcome=Outcome.FAILED,
            headline="Requirement review could not run",
            summary="",
            email_recommended=False,
            email_kind=EmailKind.NONE,
        )
        base.update(over)
        return AgentResult(**base).to_dict()

    if not issue_key:
        return _result(
            summary="No issue key was supplied, so there was nothing to review.",
            errors=["issue_key is required in delegated mode"],
        )

    # The webhook path never writes. Forced here rather than trusted from the
    # payload so no configuration or caller can turn it back on.
    delegated_payload = dict(payload)
    delegated_payload["post_to_jira"] = False
    delegated_payload["generate_docx"] = False
    delegated_payload["issue_key"] = issue_key
    delegated_payload["jql_override"] = ""  # a webhook is always one issue
    if comment_id:
        delegated_payload["comment_id"] = comment_id

    try:
        results = await run_review(delegated_payload, execute, workflow_id=workflow_id)
    except Exception as exc:  # noqa: BLE001 — a crash must still produce a reply
        logger.error("Delegated review of %s failed: %s", issue_key, exc, exc_info=True)
        return _result(
            summary=f"The review of {issue_key} could not be completed.",
            errors=[str(exc)],
            degraded=True,
            degraded_reason=str(exc),
        )

    if not results:
        return _result(
            summary=f"The review of {issue_key} produced no result.",
            errors=["the review returned nothing"],
        )

    return _to_contract(results[0], run_id=run_id).to_dict()


def _to_contract(result: dict[str, Any], *, run_id: str) -> AgentResult:
    """One issue's review result -> the shared contract."""
    documents = result.get("documents") or []
    target = result.get("target_bundle") or {}
    warnings = list(result.get("warnings") or [])

    read = sources_read(documents)
    missing = sources_missing(target, documents, warnings)

    outcome = _outcome_for(result)
    findings = result.get("findings") or []
    readiness = result.get("readiness")
    readiness_level = (readiness or {}).get("level") if isinstance(readiness, dict) else readiness

    if result.get("error") == "no_requirement_text":
        # Still a REVIEWED outcome (see _outcome_for) — this headline and
        # "missing" is how it reads as a finding rather than a request for
        # more information.
        headline = "Requirement review — nothing to review yet"
        summary = result.get("message") or (
            f"{result.get('issue_key')} has no description, attachment or linked page "
            "that could be read."
        )
    elif outcome is Outcome.FAILED:
        headline = "Requirement review could not run"
        summary = result.get("message") or "The review did not complete."
    else:
        headline = _headline(readiness_level, len(findings))
        summary = _readiness_line(readiness) or summarise(read, missing)
        known = result.get("known_questions") or []
        if known:
            summary += (
                f" {len(known)} open question(s) already listed on the ticket were "
                "not repeated below."
            )

    analysis_error = result.get("analysis_error")

    return AgentResult(
        produced_by=AGENT_NAME,
        display_name=AGENT_DISPLAY_NAME,
        agent_version=agent_version(),
        run_id=run_id,
        outcome=outcome,
        headline=headline,
        summary=summary,
        findings=findings,
        questions=_questions_from(findings),
        # A number, so the router can hold a build back after a poor review
        # without reading it out of the summary's wording.
        readiness_score=_score(readiness),
        # A review never creates anything. Stated as empty rather than omitted,
        # because the router renders "Created in Jira" from this list.
        created=[],
        duplicates=[],
        sources_read=read,
        sources_missing=missing,
        conflicts=[],
        degraded=bool(analysis_error),
        degraded_reason=str(analysis_error or ""),
        # The router decides and sends. A review that created nothing has
        # nothing to announce, so it never recommends one.
        email_recommended=False,
        email_kind=EmailKind.NONE,
        errors=[str(result["error"])] if result.get("error") else [],
    )


def _score(readiness: Any) -> int:
    """Readiness 1-5, or 0 when the review produced none."""
    try:
        return int((readiness or {}).get("score") or 0) if isinstance(readiness, dict) else 0
    except (TypeError, ValueError):
        return 0


def _questions_from(findings: list[dict[str, Any]]) -> list[str]:
    """The open questions, lifted out so the router can show them as questions.

    Only the two genuinely interrogative categories: everything else is a
    statement about the ticket, and listing it under "questions" would tell the
    reader to answer something that is not a question.
    """
    wanted = {"open_question", "dependency_question"}
    out: list[str] = []
    for finding in findings:
        category = str(finding.get("category") or "").strip().lower()
        text = str(finding.get("description") or finding.get("title") or "").strip()
        if category in wanted and text and text not in out:
            out.append(text)
    return out


def missing_as_sentences(missing: list[MissingSource]) -> list[str]:
    """Rendering helper for callers that want plain lines."""
    return [m.as_sentence() for m in missing]
