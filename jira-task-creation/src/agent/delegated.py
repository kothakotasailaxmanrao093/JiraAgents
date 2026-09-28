"""Delegated mode — this agent as a pure function the router calls.

The manual and webhook entry points are unchanged. This adds a third way in, for
when the router has received a Jira comment and decided this agent should answer
it.

What changes in delegated mode, and what does not:

* **Still creates the Jira issues.** That is the agent's purpose, and the router
  never creates anything.
* **Does not post a reply, stamp a label, record the answered comment, or send
  an email.** The router owns all four, so "exactly one comment per user
  comment" is structural rather than a promise this agent has to keep at nine
  separate return points.
* **Returns the shared result contract** instead, so the router can compose a
  reply without knowing which agent produced it.

There is one implementation of the breakdown, not two. The mode is a flag that
gates two write functions (:func:`agent.agent._report` and
:func:`agent.agent._notify`); everything else — validation, duplicate detection,
classification, creation, the four outcomes — runs exactly as it does today.
This module only translates the result afterwards.

**Guarantees that move, and how they survive** (see WORKING.md):

| Guarantee | Owner in delegated mode |
|---|---|
| Duplicate protection | unchanged — still this agent, before any write |
| Idempotency label on the created hierarchy | unchanged — still this agent |
| Answered-comment property | the ROUTER, which knows whether the reply posted |
| Marker label on the issue | the ROUTER, for the same reason |
| Never email an invalid request | the ROUTER, from ``email_recommended`` |
"""

from __future__ import annotations

from typing import Any

from common_lib.utils.logger import setup_logger

from src.models.schemas import ResultStatus
from src.shared.contract import (
    AgentResult,
    CreatedIssue,
    DuplicateRef,
    EmailKind,
    MissingSource,
    Outcome,
)

logger = setup_logger(__name__)

AGENT_NAME = "JiraTaskCreation"
# Stable, human-readable, and never the class name. A person reading a Jira
# comment sees this, so it must stay recognisable across versions.
AGENT_DISPLAY_NAME = "JiraTaskCreation"

# This agent runs inside a Temporal workflow, which may not read files or the
# environment — that is `RestrictedWorkflowAccessError`, and it has taken this
# service down before. So the version is a literal, NOT a read of metadata.json:
# reading the file here would be workflow I/O on every single run.
#
# It is kept in step by scripts/publish.py, which writes all three (pyproject,
# metadata.json and this constant) together, and by
# tests/test_version_agreement.py, which fails if they ever diverge.
AGENT_VERSION = "4.3.40"


def agent_version() -> str:
    """This build's version. A constant — see AGENT_VERSION above for why."""
    return AGENT_VERSION


# One place that maps this agent's terminal states onto the contract's closed
# outcome set. The router renders sections from the outcome and never looks at
# which agent produced it, so an unmapped status must not reach it.
_OUTCOME_MAP = {
    ResultStatus.JIRA_CREATED: Outcome.CREATED,
    ResultStatus.CLARIFICATION_REQUIRED: Outcome.NEEDS_INFO,
    ResultStatus.VALIDATION_ERROR: Outcome.NOT_A_REQUIREMENT,
    ResultStatus.OUT_OF_SCOPE: Outcome.NOT_A_REQUIREMENT,
    ResultStatus.EXPLAINED: Outcome.ANSWERED,
    ResultStatus.JIRA_CREATION_FAILED: Outcome.FAILED,
    # "Ready but not written" happens when creation was switched off, or the
    # event carried no issue. Nothing was made and nothing failed.
    ResultStatus.READY_FOR_JIRA: Outcome.REVIEWED,
}

_HEADLINE = {
    Outcome.CREATED: "Work breakdown created",
    Outcome.ALREADY_EXISTS: "This work already exists",
    Outcome.NEEDS_INFO: "More information needed before this can be broken down",
    Outcome.NOT_A_REQUIREMENT: "Invalid request — this is not a work requirement",
    Outcome.REVIEWED: "Nothing was created",
    Outcome.FAILED: "Could not complete this request",
    # Replaced by "About <KEY>" in to_contract, which knows the key.
    Outcome.ANSWERED: "About this ticket",
}

# What the suppressed email would have been -> the contract's kind.
_EMAIL_KIND = {
    "created": EmailKind.CREATED,
    "clarification_required": EmailKind.CLARIFICATION,
    "duplicates_found": EmailKind.DUPLICATES,
    "partial_duplicate": EmailKind.DUPLICATES,
    "failed": EmailKind.FAILED,
    # Deliberately absent: "invalid_request". An invalid request must never
    # produce an email, and leaving it unmapped means the rule holds even if a
    # future branch asks for one.
}


def _status(result: dict[str, Any]) -> ResultStatus:
    raw = result.get("status")
    try:
        return ResultStatus(raw)
    except ValueError:
        logger.warning(f"Unknown status {raw!r} from the breakdown; treating as failed")
        return ResultStatus.JIRA_CREATION_FAILED


def _created(result: dict[str, Any]) -> list[CreatedIssue]:
    """The issues this run actually created in Jira, in creation order.

    Read from the Epic, Stories and Sub-tasks the write step returns. This used
    to read ``jira_result["created"]``, a field that never exists, so no reply
    ever carried a "Created in Jira" list — only keys, in the internal summary
    (BGV-16, 2026-09-24). Reused issues are not "created" and are left out.
    """
    jira = result.get("jira_result") or {}
    made = {str(k) for k in jira.get("created_keys") or []}
    refs = [jira.get("epic"), *(jira.get("stories") or []), *(jira.get("subtasks") or [])]
    out: list[CreatedIssue] = []
    for item in refs:
        if not isinstance(item, dict) or str(item.get("key") or "") not in made:
            continue
        out.append(
            CreatedIssue(
                key=str(item.get("key")),
                issue_type=str(item.get("issue_type") or ""),
                summary=str(item.get("summary") or ""),
                url=str(item.get("url") or ""),
            )
        )
    return out


def _still_open(result: dict[str, Any]) -> list[str]:
    """After a build: what the input did not say, and what the self-review
    gate could not fix — the reply's "please confirm" list (D1).

    Empty means every detail came from the input; the router then says so.
    """
    questions = [
        f"{story.get('title')}: {q}"
        for story in result.get("stories") or []
        for q in story.get("open_questions") or []
    ]
    return questions + [f"Please check: {note}" for note in result.get("quality_notes") or []]


def _reused(result: dict[str, Any]) -> list[DuplicateRef]:
    """The issues an identical earlier request created, which this run reused."""
    jira = result.get("jira_result") or {}
    if not jira.get("reused_existing"):
        return []
    refs = [jira.get("epic"), *(jira.get("stories") or []), *(jira.get("subtasks") or [])]
    return [
        DuplicateRef(
            existing_key=str(item.get("key")),
            existing_summary=str(item.get("summary") or ""),
            url=str(item.get("url") or ""),
        )
        for item in refs
        if isinstance(item, dict) and item.get("key")
    ]


def _duplicates(result: dict[str, Any]) -> list[DuplicateRef]:
    out: list[DuplicateRef] = []
    for item in result.get("duplicate_matches") or []:
        if not isinstance(item, dict):
            continue
        out.append(
            DuplicateRef(
                proposed_title=str(item.get("proposed_title") or item.get("proposed") or ""),
                existing_key=str(item.get("existing_key") or item.get("key") or ""),
                existing_summary=str(item.get("existing_summary") or item.get("summary") or ""),
                score=float(item.get("score") or 0.0),
                matched_on=str(item.get("matched_on") or ""),
                url=str(item.get("url") or ""),
            )
        )
    return out


def _sources_read(result: dict[str, Any]) -> list[str]:
    """Everything that was read, by name.

    Built from what the read step actually returned, so it can never claim a
    source that failed — an unreadable attachment appears in ``sources_missing``
    instead, never here.
    """
    source = result.get("source_issue") or {}
    read: list[str] = []

    key = source.get("key") or ""
    if (source.get("description") or "").strip():
        read.append(f"{key} description".strip())

    comments = source.get("comments") or []
    if comments:
        read.append(f"{len(comments)} comment(s)")

    for item in source.get("attachments") or []:
        if (item.get("text") or "").strip():
            read.append(f"Attachment: {item.get('filename') or 'unnamed file'}")

    for page in source.get("confluence_pages") or []:
        if (page.get("text") or "").strip():
            read.append(f"Confluence: {page.get('title') or page.get('url') or 'linked page'}")

    if source.get("linked_issues"):
        read.append(f"{len(source['linked_issues'])} linked work item(s)")

    context = result.get("jira_context") or {}
    if context.get("issue_count"):
        read.append(f"{context['issue_count']} existing ticket(s) in the project")

    return read


def _sources_missing(result: dict[str, Any]) -> list[MissingSource]:
    """Everything that was linked or attached and could not be read.

    Surfacing this is the entire point: a Confluence page that would not open or
    a PDF that would not parse otherwise produces a thin breakdown that looks
    like a badly written requirement.

    ``unread_links`` (Bug 6): a link *referenced* partway through a longer
    requirement, as opposed to gate 0c's narrower "the request is only a
    link" case. Without this, a routed reply never named it — the router's
    "What was missing or unreadable" section is built entirely from
    ``sources_missing``, and this was the one unreadable-source kind that
    never reached it.
    """
    source = result.get("source_issue") or {}
    missing: list[MissingSource] = []

    for url in source.get("unread_links") or []:
        missing.append(
            MissingSource(
                what=f"Link in the request: {url}",
                why="could not be read",
                what_to_do=(
                    "only pages on this Atlassian site can be read — paste the "
                    "relevant section into the ticket, or attach the document"
                ),
            )
        )

    for page in source.get("confluence_pages") or []:
        if (page.get("text") or "").strip() or not page.get("note"):
            continue
        label = page.get("title") or page.get("url") or "A linked Confluence page"
        missing.append(
            MissingSource(
                what=f"Confluence: {label}",
                why=str(page["note"]),
                what_to_do="please grant access, or paste the relevant section into the ticket",
            )
        )

    for item in source.get("attachments") or []:
        if (item.get("text") or "").strip() or not item.get("note"):
            continue
        missing.append(
            MissingSource(
                what=f"Attachment: {item.get('filename') or 'an attachment'}",
                why=str(item["note"]),
                what_to_do="please re-attach it in a readable format",
            )
        )

    return missing


def _email(result: dict[str, Any], outcome: Outcome) -> tuple[bool, EmailKind]:
    """Whether to recommend an email, and which one.

    The child only ever *recommends*. The router decides and sends, so the
    "never email an invalid request" rule has exactly one enforcement point —
    and it is enforced twice here anyway, because an unmapped kind cannot
    produce a recommendation.
    """
    if outcome is Outcome.NOT_A_REQUIREMENT:
        return False, EmailKind.NONE

    notification = result.get("notification") or {}
    kind = str(notification.get("kind") or "")
    mapped = _EMAIL_KIND.get(kind)
    if mapped is None:
        return False, EmailKind.NONE
    return True, mapped


def to_contract(result: dict[str, Any], *, run_id: str, agent_version: str) -> AgentResult:
    """Translate one completed run into the shared contract.

    Deliberately a pure function over the existing result: the breakdown has
    already happened, so this cannot change what the agent did — only how it is
    described to the router.
    """
    status = _status(result)
    outcome = _OUTCOME_MAP.get(status, Outcome.FAILED)
    created = _created(result)
    duplicates = _duplicates(result)

    # "Ready" with duplicates found is the already-exists outcome; the contract
    # distinguishes it because the router renders a different section for it.
    if duplicates and not created and outcome in (Outcome.REVIEWED, Outcome.NEEDS_INFO):
        outcome = Outcome.ALREADY_EXISTS

    # The identical request was built before (the idempotency label matched),
    # so nothing was written. It said "Work breakdown created" and named no
    # keys (BGV-46 reusing BGV-34..42, 2026-09-24). It already exists — say so,
    # and list what exists.
    if outcome is Outcome.CREATED and not created and _reused(result):
        outcome = Outcome.ALREADY_EXISTS
        duplicates = _reused(result)

    email_recommended, email_kind = _email(result, outcome)
    key = str((result.get("source_issue") or {}).get("key") or "")
    generator = str(result.get("generator") or "")

    return AgentResult(
        produced_by=AGENT_NAME,
        display_name=AGENT_DISPLAY_NAME,
        agent_version=agent_version,
        run_id=run_id,
        outcome=outcome,
        headline=(
            f"About {key}"
            if outcome is Outcome.ANSWERED and key
            # The run's own heading when it has one ("BGV-25 is now an Epic").
            else str(result.get("headline") or "") or _HEADLINE.get(outcome, "Nothing was created")
        ),
        # The plain message, not ``summary_text``: that is the standalone
        # run's status block ("Outcome : … / Status : JIRA_CREATED / Generated
        # by : llm"), which reached every reply and contradicted headlines —
        # "This work already exists" over "Outcome : Paused" (BGV-9).
        summary=str(result.get("message") or ""),
        created=created,
        findings=[],
        questions=(
            _still_open(result)
            if outcome is Outcome.CREATED
            else list(result.get("clarifying_questions") or [])
        ),
        duplicates=duplicates,
        sources_read=_sources_read(result),
        sources_missing=_sources_missing(result),
        conflicts=list((result.get("source_issue") or {}).get("conflicts") or []),
        # The heuristic generator runs when the model was unreachable. Its
        # wording is wooden, and the reader has to be told so — the child knows,
        # the router renders it.
        degraded=generator == "heuristic",
        degraded_reason=(
            # Not "unreachable": the fallback also runs when the model answered
            # but its answer could not be used (cut off, invalid JSON). The
            # worker log carries the real cause.
            "The AI model's answer could not be used, so the wording was generated locally."
            if generator == "heuristic"
            else ""
        ),
        email_recommended=email_recommended,
        email_kind=email_kind,
        errors=list(result.get("validation_errors") or []),
    )
