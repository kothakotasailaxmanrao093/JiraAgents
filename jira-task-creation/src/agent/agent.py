"""Agent definitions for the project."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import timedelta
from typing import Any, Dict

from aetherion_sdk import agent, toolExecutor
from common_lib.utils.logger import setup_logger

from src.agent.delegated import agent_version, to_contract
from src.models.schemas import (
    Classification,
    NotificationKind,
    RequestBucket,
    ResultStatus,
    SprintPlacement,
    TicketWiseResult,
)

logger = setup_logger(__name__)

# --------------------------------------------------------------------------
# Delegated mode
# --------------------------------------------------------------------------
#
# When the router invoked this agent, the router owns the single reply: it posts
# the comment, stamps the label, records the answered comment and sends at most
# one email. This agent still CREATES the Jira issues — that is its purpose —
# but must not write any of the rest, or a routed run produces two comments.
#
# It is a ContextVar rather than a parameter threaded through the outcome
# branches because there are nine write-back sites and eight notify sites.
# Passing a flag to each would mean editing every branch to add a mode, which is
# precisely the change Open/Closed says to avoid — and a branch missed in the
# edit would post a second comment in production without failing any test.
# Gating the two write functions instead means there is exactly one place per
# side effect where the decision is made.
_DELEGATED: ContextVar[bool] = ContextVar("ltw_delegated", default=False)


def delegated() -> bool:
    """True when the router, not this agent, owns the reply."""
    return _DELEGATED.get()


# Reading the trigger issue downloads and parses attachments, so it gets the
# most headroom of the read-only steps.
_READ_TIMEOUT = timedelta(minutes=5)
_INSPECT_TIMEOUT = timedelta(minutes=3)
_VALIDATE_TIMEOUT = timedelta(minutes=2)
_BREAKDOWN_TIMEOUT = timedelta(minutes=6)  # D1 self-review adds 1-2 model calls
_JIRA_TIMEOUT = timedelta(minutes=10)
_NOTIFY_TIMEOUT = timedelta(minutes=2)
_REPORT_TIMEOUT = timedelta(minutes=2)


# Said in the run result, in the Jira comment and in the email, because a run
# that degrades silently produces tickets a reader blames on the agent rather
# than on the outage that caused them.
_QUALITY_WARNING = (
    "These items were written without the language model (the AI Gateway was "
    "unreachable), so the wording is generic and priority and complexity both "
    "default to Medium. The breakdown itself is sound — please reword before "
    "the team picks the work up, or delete it and ask again once the model is "
    "available."
)


# The single source of truth for every boolean trigger's default, read by both
# this module and tests/test_trigger_defaults.py, which fails metadata.json's
# "default" key ever drifts from this — the way it silently did for the
# requirement-review agent's booleans (FLAWS.md F5). One table so there is
# exactly one place to change a default, and one place a form and the code
# can disagree.
TRIGGER_DEFAULTS: dict[str, bool] = {
    "create_in_jira": True,
    "force": False,
}


def _as_bool(value: Any, default: bool = True) -> bool:
    """Coerce a UI value to bool.

    Blank fields arrive from the local form as empty strings, which must not be
    read as False for a flag that defaults to True.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _summarise(
    *,
    status: ResultStatus,
    message: str,
    classification: Classification | None = None,
    breakdown: dict[str, Any] | None = None,
    jira_result: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    questions: list[str] | None = None,
    errors: list[str] | None = None,
    duplicates: list[dict[str, Any]] | None = None,
    generator: str = "",
) -> tuple[dict[str, Any], str]:
    """Condense a run into a flat dict and a plain-text block.

    The full result stays nested because callers need the detail. These two
    fields exist so a human can see what happened without unpacking it.
    """
    jira_result = jira_result or {}
    project = (context or {}).get("project") or {}
    epic = jira_result.get("epic") or {}
    stories = jira_result.get("stories") or []
    subtasks = jira_result.get("subtasks") or []

    overview: dict[str, Any] = {
        "status": status.value,
        "outcome": _OUTCOME.get(status, status.value),
        "project": project.get("key", ""),
        "classification": classification.value if classification else "",
        # Did THIS run write anything? A reuse run did not.
        "created_in_jira": bool(jira_result.get("created_keys")),
        "reused_existing": bool(jira_result.get("reused_existing")),
        "epic_key": epic.get("key", ""),
        "epic_reused": bool(jira_result.get("epic_reused")),
        "story_count": len(stories),
        "subtask_count": len(subtasks),
        "story_keys": [s.get("key", "") for s in stories],
        "subtask_keys": [s.get("key", "") for s in subtasks],
        "placement": jira_result.get("placement", ""),
        "sprint": ((jira_result.get("sprint") or {}) or {}).get("name", ""),
        "generator": generator,
        "message": message,
    }
    if generator == "heuristic":
        # Production runs the gateway. If a run silently degrades, the tickets
        # still appear — just thinner — so say so where it will be read.
        overview["quality_warning"] = _QUALITY_WARNING
    if not jira_result:
        # Nothing reached Jira: describe the proposal instead of the outcome.
        proposed = breakdown or {}
        overview["proposed_stories"] = len(proposed.get("stories") or [])
        overview["proposed_subtasks"] = sum(
            len(st.get("subtasks") or []) for st in (proposed.get("stories") or [])
        )
        overview["proposed_epic"] = bool(proposed.get("epic"))

    lines = [
        f"Outcome        : {overview['outcome']}",
        f"Status         : {status.value}",
    ]
    if project.get("key"):
        lines.append(f"Jira project   : {project['key']}")
    if classification:
        lines.append(f"Classification : {classification.value}")
    if generator:
        lines.append(f"Generated by   : {generator}")
    if overview.get("quality_warning"):
        lines.append(f"Warning        : {overview['quality_warning']}")

    reused_run = bool(jira_result.get("reused_existing"))
    if jira_result.get("created_keys") or jira_result.get("reused_keys"):
        lines.append("")
        if epic.get("key"):
            # On a reuse run nothing was created, so nothing may be labelled
            # "(created)" — including the epic.
            tag = "reused" if (reused_run or jira_result.get("epic_reused")) else "created"
            lines.append(f"Epic           : {epic['key']} ({tag})")
        verb = "reused" if reused_run else "created"
        if stories:
            lines.append(f"Stories ({len(stories)}, {verb}) : " + ", ".join(overview["story_keys"]))
        if subtasks:
            lines.append(
                f"Subtasks ({len(subtasks)}, {verb}) : " + ", ".join(overview["subtask_keys"])
            )
        lines.append(f"Placement      : {overview['placement'] or 'backlog'}")
        if reused_run:
            lines.append("Note           : existing tickets reused, nothing created again")
    elif breakdown:
        lines.append("")
        ns = overview.get("proposed_stories", 0)
        nt = overview.get("proposed_subtasks", 0)
        lines.append(
            f"Proposed       : {'1 Epic, ' if overview.get('proposed_epic') else ''}"
            f"{ns} Stor{'y' if ns == 1 else 'ies'}, "
            f"{nt} Subtask{'' if nt == 1 else 's'}"
        )
        lines.append("Created in Jira: no")
    else:
        lines.append("")
        lines.append("Created in Jira: no")

    for q in questions or []:
        lines.append(f"Question       : {q}")
    for d in duplicates or []:
        lines.append(
            f"Possible dupe  : {d.get('existing_key')} "
            f"(score {d.get('score')}) {d.get('existing_summary', '')[:50]}"
        )
    for e in errors or []:
        if e:
            lines.append(f"Error          : {e}")
    if jira_result.get("failure_reason"):
        lines.append(f"Failure        : {jira_result['failure_reason']}")
    if jira_result.get("sprint_error"):
        lines.append(f"Sprint problem : {jira_result['sprint_error']}")

    lines.append("")
    lines.append(f"Summary        : {message}")
    return overview, "\n".join(lines)


_OUTCOME = {
    ResultStatus.VALIDATION_ERROR: "Rejected - the input is not a usable requirement",
    ResultStatus.OUT_OF_SCOPE: "Rejected - not project or product work",
    ResultStatus.CLARIFICATION_REQUIRED: "Paused - more information needed",
    ResultStatus.READY_FOR_JIRA: "Breakdown ready - not written to Jira",
    ResultStatus.EXPLAINED: "Answered - a question, so nothing was created",
    ResultStatus.JIRA_CREATED: "Done - tickets are in Jira",
    ResultStatus.JIRA_CREATION_FAILED: "Failed - see the reason below",
}


def _result(**kwargs: Any) -> dict:
    """Build the structured return value, adding the flat summary views."""
    overview, summary_text = _summarise(
        status=kwargs["status"],
        message=kwargs.get("message", ""),
        classification=kwargs.get("classification"),
        breakdown=(
            {
                "epic": kwargs.get("epic"),
                "stories": kwargs.get("stories") or [],
            }
            if kwargs.get("stories")
            else None
        ),
        jira_result=kwargs.get("jira_result"),
        context=kwargs.get("jira_context"),
        questions=kwargs.get("clarifying_questions"),
        errors=kwargs.get("validation_errors"),
        duplicates=kwargs.get("duplicate_matches"),
        generator=kwargs.get("generator", ""),
    )
    return TicketWiseResult(**kwargs, overview=overview, summary_text=summary_text).model_dump(
        mode="json", exclude_none=False
    )


async def _notify(
    kind: NotificationKind,
    *,
    issue_key: str,
    issue_summary: str = "",
    project_key: str = "",
    situation: str = "",
    questions: list[str] | None = None,
    duplicates: list[dict[str, Any]] | None = None,
    created_keys: list[str] | None = None,
    errors: list[str] | None = None,
    requirement: str = "",
    remaining: list[str] | None = None,
    counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Send one email. A delivery failure is recorded, never raised.

    In delegated mode nothing is sent: the router decides and sends at most one
    email for the whole run. What this branch *would* have sent is returned
    instead, so the result contract can recommend it — one decision point means
    "never email an invalid request" cannot be broken by a child that forgets.
    """
    if delegated():
        logger.info(f"Delegated mode: email '{kind.value}' recommended, not sent")
        return {"attempted": False, "suppressed": True, "kind": kind.value}

    result = await toolExecutor.execute(
        "notify_email",
        kind.value,
        issue_key,
        issue_summary,
        project_key,
        situation,
        questions or [],
        duplicates or [],
        created_keys or [],
        errors or [],
        requirement,
        remaining or [],
        counts or {},
        start_to_close_timeout=_NOTIFY_TIMEOUT,
    )
    if result.get("error"):
        logger.error(f"Notification not delivered: {result['error']}")
    return result


def _notification_note(notification: dict[str, Any] | None) -> str:
    """One line for the Jira comment saying whether the email got out."""
    if not notification or not notification.get("attempted"):
        return ""
    if notification.get("sent"):
        return f"Emailed {', '.join(notification.get('recipients') or [])}."
    return f"Email could not be sent: {notification.get('error', 'unknown error')}"


def _unreadable_notes(source: dict[str, Any] | None) -> list[str]:
    """Everything that was linked or attached but could not be read.

    Surfacing this on the ticket is the point: a Confluence page the agent
    could not open, or a PDF it could not parse, otherwise produces a thin
    breakdown that looks like a badly written requirement.

    ``unread_links`` (Bug 6) is the case where the requirement is not *only* a
    link — gate 0c already handles that narrower case on its own — but
    *references* one partway through ("...per the policy at <link>, and also
    show a warning if..."). Without this, that link silently vanished: nothing
    downstream of ``find_unread_links`` named it once the request had enough
    other text to pass validation.
    """
    if not source:
        return []
    notes: list[str] = []
    for page in source.get("confluence_pages") or []:
        if not (page.get("text") or "").strip() and page.get("note"):
            label = page.get("url") or page.get("title") or "A linked Confluence page"
            notes.append(f"{label} — {page['note']}")
    for item in source.get("attachments") or []:
        if not (item.get("text") or "").strip() and item.get("note"):
            notes.append(f"{item.get('filename') or 'An attachment'} — {item['note']}")
    for url in source.get("unread_links") or []:
        notes.append(f"{url} — could not be read; only pages on this Atlassian site can be")
    return notes


async def _report(
    issue_key: str,
    *,
    headline: str,
    situation: str = "",
    created: list[dict[str, Any]] | None = None,
    questions: list[str] | None = None,
    duplicates: list[dict[str, Any]] | None = None,
    errors: list[str] | None = None,
    context_notes: list[str] | None = None,
    conflicts: list[str] | None = None,
    notification: dict[str, Any] | None = None,
    mark_processed: bool = False,
    mark_awaiting: bool = False,
    answered_comment_id: str = "",
    generator: str = "",
) -> dict[str, Any]:
    """Write the outcome back onto the trigger issue.

    Skipped in manual mode (there is no issue to write to) and in delegated mode
    (the router posts the one reply, stamps the label and records the answered
    comment, so that "exactly one comment" is structural rather than something
    every branch here has to remember).
    """
    if not issue_key or delegated():
        return {}
    result = await toolExecutor.execute(
        "report_to_issue",
        issue_key,
        headline,
        situation,
        created or [],
        questions or [],
        duplicates or [],
        errors or [],
        context_notes or [],
        conflicts or [],
        _notification_note(notification),
        mark_processed,
        mark_awaiting,
        answered_comment_id,
        _QUALITY_WARNING if generator == "heuristic" else "",
        start_to_close_timeout=_REPORT_TIMEOUT,
    )
    if result.get("error"):
        logger.error(f"Write-back problem on {issue_key}: {result['error']}")
    return result


@agent()
async def JiraTaskCreation(payload: Dict[str, Any]) -> dict:
    """Entry point. Runs the breakdown, then shapes the result for the caller.

    Three ways in, one implementation:

    * **Webhook** — a Jira Automation rule supplies ``issue_key``. Reads the
      issue, creates the hierarchy, writes the outcome back as a comment and a
      label, and emails.
    * **Manual** — ``requirement`` and ``project_key`` are typed in. Nothing is
      read from an issue and nothing is written back.
    * **Delegated** (``mode="delegated"``) — the router invoked this agent. The
      hierarchy is still created, but the reply, the label, the answered-comment
      record and the email all belong to the router, and the shared result
      contract is returned instead.

    The mode is decided here, once, and the delegated result is translated here,
    once. The breakdown below does not branch on it — there are twelve exits,
    and a mode check at each would be twelve chances to post a second comment.
    """
    is_delegated = str(payload.get("mode") or "").strip().lower() == "delegated"

    # Set on EVERY run, not only the delegated ones, and reset afterwards.
    # Setting it only when delegated left it true for whatever ran next in the
    # same context — which silently switched write-back off for a direct run.
    # A leak here is invisible: nothing errors, the reply simply never appears.
    token = _DELEGATED.set(is_delegated)
    try:
        result = await _run_breakdown(payload)
    finally:
        _DELEGATED.reset(token)

    if not is_delegated:
        return result

    return to_contract(
        result,
        run_id=str(payload.get("run_id") or "").strip(),
        agent_version=agent_version(),
    ).to_dict()


async def _run_breakdown(payload: Dict[str, Any]) -> dict:
    """Turn a Jira requirement into an Epic/Story/Subtask hierarchy.

    Two entry modes:

    * **Webhook** — ``issue_key`` is supplied by a Jira Automation rule. The
      agent reads that issue in full (description, attachments, comments,
      history, work logs, links), derives the project key from it, and writes
      the outcome back as a comment plus a marker label.
    * **Manual** — ``requirement`` and ``project_key`` are typed in. No issue is
      read and nothing is written back.

    Every stop that needs a human — clarification, duplicates, failure — sends
    an email saying what the situation is. Duplicates are never created.
    """
    logger.info("Started the execution of agent JiraTaskCreation")

    issue_key = str(payload.get("issue_key") or "").strip().upper()
    requirement = payload.get("requirement") or payload.get("input") or ""
    project_key = payload.get("project_key") or ""
    project_name = payload.get("project_name") or ""
    existing_epic_key = payload.get("existing_epic_key") or ""
    placement = payload.get("placement") or SprintPlacement.BACKLOG.value
    correlation_id = payload.get("correlation_id") or ""
    # The webhook names the comment that asked, so the right one is answered
    # even when several arrive close together.
    comment_id = str(payload.get("comment_id") or "").strip()
    create_in_jira = _as_bool(
        payload.get("create_in_jira"), default=TRIGGER_DEFAULTS["create_in_jira"]
    )
    force = _as_bool(payload.get("force"), default=TRIGGER_DEFAULTS["force"])

    webhook_mode = bool(issue_key)
    logger.info(
        f"mode : {'webhook' if webhook_mode else 'manual'} | issue_key : {issue_key or '-'}"
    )

    # Not every event carries an issue. Project-level events render the params
    # template to the bare project key ("FL"), which then fails to read and used
    # to be reported as a failure — one email per stray event. Nothing went
    # wrong for a person to act on, so this leaves quietly.
    prefix, _, number = issue_key.partition("-")
    if webhook_mode and not (number.isdigit() and prefix.isalnum()):
        note = f"{issue_key!r} is not an issue key; the event carried no issue. Nothing was done."
        logger.info(note)
        return _result(
            status=ResultStatus.READY_FOR_JIRA,
            message=note,
            skipped_reason="no_issue_in_event",
        )

    source: dict[str, Any] | None = None
    issue_summary = ""
    trigger_comment_id = ""

    # ---- Gate 0 (webhook only): read the issue that triggered the run ----
    if webhook_mode:
        source = await toolExecutor.execute(
            "read_jira_issue",
            issue_key,
            not force,
            not force,
            comment_id,
            start_to_close_timeout=_READ_TIMEOUT,
        )
        if not source.get("ok"):
            reason = source.get("error", "The trigger issue could not be read.")
            logger.error(f"Trigger issue unreadable: {reason}")
            notification = await _notify(
                NotificationKind.FAILED,
                issue_key=issue_key,
                situation=reason,
                errors=[reason],
            )
            return _result(
                status=ResultStatus.JIRA_CREATION_FAILED,
                message=reason,
                validation_errors=[reason],
                source_issue=source,
                notification=notification,
            )

        issue_summary = source.get("summary", "")
        trigger_comment_id = source.get("trigger_comment_id", "")
        # The idempotency key is NOT derived from the comment id.
        #
        # It used to default to "<issue>#<comment>", on the reasoning that one
        # ticket carries many requests. But the key already hashes the request
        # text, so two different requests on one ticket differ anyway — while
        # the SAME request asked in a second comment got a different label,
        # found nothing to reuse, and created the whole hierarchy again.
        # (create_hierarchy carries a hand-written workaround for exactly this
        # on the attach-to-parent path: "three runs of one request left FL-53
        # carrying nine Sub-tasks, three of each".)
        #
        # An explicit correlation_id from the payload still wins — that is a
        # documented trigger field for callers with their own request identity.
        # Which comment was answered is tracked separately, by the
        # ltw-answered-comments issue property.

        # Silent stops: neither is a problem, so neither emails anyone.
        if source.get("already_processed"):
            note = (
                f"That request on {source.get('key')} has already been answered; "
                f"nothing was done. Re-run with 'force' to answer it again."
            )
            logger.info(note)
            return _result(
                status=ResultStatus.READY_FOR_JIRA,
                message=note,
                skipped_reason="already_processed",
                source_issue=source,
            )
        if not source.get("trigger_matched"):
            note = (
                f"No unanswered comment on {source.get('key')} mentions the "
                f"trigger keyword; nothing was done."
            )
            logger.info(note)
            return _result(
                status=ResultStatus.READY_FOR_JIRA,
                message=note,
                skipped_reason="trigger_keyword_absent",
                source_issue=source,
            )

        # ---- Gate 0b: a question is answered, never decomposed -----------
        # "@Aetherion please explain me this ticket properly" used to be read
        # as an instruction to work from the ticket, and answering it created
        # sub-tasks. A question gets prose and writes nothing.
        # Both the decision and the wording were prepared by read_jira_issue.
        # Working either out here would read the trigger keyword from the
        # environment, which a Temporal workflow is not allowed to do.
        if source.get("trigger_is_question"):
            explanation = str(source.get("explanation") or "")
            logger.info(f"Question on {source.get('key')}; answering without creating anything.")
            await _report(
                issue_key,
                headline=f"About {source.get('key') or 'this issue'}",
                situation=explanation,
                context_notes=_unreadable_notes(source),
                conflicts=(source or {}).get("conflicts") or [],
                answered_comment_id=trigger_comment_id,
            )
            return _result(
                status=ResultStatus.EXPLAINED,
                message=explanation,
                source_issue=source,
            )

        # ---- Gate 0c: the request was only a link we could not read ------
        # "Build what this spec says: <link>" says nothing once an unreadable
        # link is removed. It used to pass validation on the word "build" and
        # then get matched against existing tickets on the four words left.
        if source.get("request_is_only_link"):
            ignored = list(source.get("unread_links") or [])
            reason = (
                "The request points at a link, and that link could not be read: "
                f"{', '.join(ignored)}. Only pages on this Atlassian site can be "
                "read, and the page must still exist. Nothing was created."
            )
            questions = [
                "What should be built? Paste the requirement into the ticket, "
                "or attach the document to it.",
                "If it is a Confluence page on this site, check the agent's " "account can see it.",
            ]
            logger.info(f"Request on {source.get('key')} was only an unreadable link.")
            notification = await _notify(
                NotificationKind.CLARIFICATION_REQUIRED,
                issue_key=issue_key,
                issue_summary=issue_summary,
                project_key=source.get("project_key", ""),
                situation=reason,
                questions=questions,
                requirement=str(source.get("trigger_comment_body") or ""),
            )
            await _report(
                issue_key,
                headline="More information is needed — nothing created",
                situation=reason,
                questions=questions,
                notification=notification,
                context_notes=_unreadable_notes(source),
                answered_comment_id=trigger_comment_id,
                mark_awaiting=True,
            )
            return _result(
                status=ResultStatus.CLARIFICATION_REQUIRED,
                message=reason,
                clarifying_questions=questions,
                source_issue=source,
                notification=notification,
            )

        requirement = source.get("requirement_text") or ""
        project_key = source.get("project_key") or project_key
        project_name = project_name or source.get("project_name") or ""
        # The requester says where the work goes in their own words
        # ("Add the stories to the current sprint"), so that wins unless the
        # caller explicitly set the field.
        if not str(payload.get("placement") or "").strip():
            placement = source.get("requested_placement") or SprintPlacement.BACKLOG.value
        logger.info(f"requested_placement : {placement}")
        logger.info(
            f"read {source.get('key')} | project={project_key} | "
            f"sources={source.get('source_counts', {})} | "
            f"requirement_chars={len(requirement)}"
        )

    logger.info(f"requirement_length : {len(str(requirement))}")
    logger.info(f"project_key : {project_key} | epic : {existing_epic_key or '-'}")
    logger.info(f"placement : {placement} | create_in_jira : {create_in_jira}")

    # ---- Gate 1: read and validate the Jira side -------------------------
    context = await toolExecutor.execute(
        "inspect_jira_context",
        project_key,
        existing_epic_key,
        placement,
        start_to_close_timeout=_INSPECT_TIMEOUT,
    )
    if not context.get("ok"):
        reason = context.get("error", "Jira context could not be established.")
        asks = bool(context.get("needs_clarification"))
        logger.error(f"Jira context unusable: {reason} (needs_clarification={asks})")
        notification = await _notify(
            NotificationKind.CLARIFICATION_REQUIRED if asks else NotificationKind.FAILED,
            issue_key=issue_key,
            issue_summary=issue_summary,
            project_key=project_key,
            situation=reason,
            questions=(
                [
                    "Which sprint should these stories go into?",
                    "Or should they go to the product backlog instead?",
                ]
                if asks
                else []
            ),
            errors=[] if asks else [reason],
            requirement=requirement,
        )
        await _report(
            issue_key,
            headline=(
                "More information is needed before this can be created."
                if asks
                else "Could not process this issue."
            ),
            situation=reason,
            errors=[] if asks else [reason],
            notification=notification,
            context_notes=_unreadable_notes(source),
            conflicts=(source or {}).get("conflicts") or [],
            answered_comment_id=trigger_comment_id,
            # Either way a person has to act — the difference is only whether
            # the agent is asking them a question or reporting a fault.
            mark_awaiting=True,
        )
        return _result(
            status=(
                ResultStatus.CLARIFICATION_REQUIRED if asks else ResultStatus.JIRA_CREATION_FAILED
            ),
            message=reason,
            validation_errors=[reason],
            jira_context=context,
            source_issue=source,
            notification=notification,
        )

    project = context.get("project") or {}
    existing = context.get("existing_issues") or []
    epic_ctx = context.get("epic")
    sprint = context.get("sprint")
    logger.info(
        f"project={project.get('key')} existing={len(existing)} "
        f"epic={(epic_ctx or {}).get('key','-')} sprint={(sprint or {}).get('id','-')}"
    )

    project_overrides = {
        "project": project,
        "existing_issues": existing,
        "project_name": project_name,
        # So the overlap check does not match the request against itself.
        "trigger_key": issue_key,
    }

    # ---- Gate 2: input validation, scope, relevance, clarification -------
    validation = await toolExecutor.execute(
        "validate_requirement",
        requirement,
        project_overrides,
        start_to_close_timeout=_VALIDATE_TIMEOUT,
    )
    status = validation.get("status", ResultStatus.VALIDATION_ERROR.value)
    logger.info(f"validation_status : {status}")

    if status != ResultStatus.READY_FOR_JIRA.value:
        reason = validation.get("reason", "")
        questions = validation.get("clarifying_questions", []) or []
        errors = validation.get("validation_errors", []) or []
        needs_answers = status == ResultStatus.CLARIFICATION_REQUIRED.value
        bucket = validation.get("bucket", "")

        # Bucket A — this was never a requirement. One line back, nothing
        # asked, nothing created, and no email at any setting. Answering "hi"
        # with a list of questions about the project is how a useful agent
        # becomes one people mute.
        if bucket == RequestBucket.NOT_A_REQUIREMENT.value:
            questions = []
            errors = []
            needs_answers = False

        logger.info(f"Validation gate stopped the run (bucket={bucket or 'n/a'}).")
        notification = await _notify(
            (
                NotificationKind.INVALID_REQUEST
                if bucket == RequestBucket.NOT_A_REQUIREMENT.value
                else (
                    NotificationKind.CLARIFICATION_REQUIRED
                    if needs_answers
                    else NotificationKind.FAILED
                )
            ),
            issue_key=issue_key,
            issue_summary=issue_summary,
            project_key=project.get("key", ""),
            situation=reason,
            questions=questions,
            errors=errors,
            requirement=requirement,
        )
        await _report(
            issue_key,
            headline=(
                "Invalid request — this is not a work requirement"
                if bucket == RequestBucket.NOT_A_REQUIREMENT.value
                else (
                    "More information is needed — nothing created"
                    if needs_answers
                    else "Could not be processed"
                )
            ),
            situation=reason,
            questions=questions,
            errors=errors,
            notification=notification,
            context_notes=_unreadable_notes(source),
            conflicts=(source or {}).get("conflicts") or [],
            answered_comment_id=trigger_comment_id,
            mark_awaiting=needs_answers,
        )
        return _result(
            status=ResultStatus(status),
            message=reason,
            clarifying_questions=questions,
            validation_errors=errors,
            jira_context=context,
            source_issue=source,
            notification=notification,
        )

    clean_requirement = validation.get("requirement", "") or str(requirement).strip()

    # ---- Gate 3: classification and decomposition ------------------------
    breakdown_result = await toolExecutor.execute(
        "generate_work_breakdown",
        clean_requirement,
        project_overrides,
        start_to_close_timeout=_BREAKDOWN_TIMEOUT,
    )
    if breakdown_result.get("error") or not breakdown_result.get("breakdown"):
        reason = breakdown_result.get("error", "Unknown decomposition error.")
        logger.error(f"Breakdown failed: {reason}")
        message = (
            "A work breakdown could not be produced from this requirement, "
            "so no Jira issues were created."
        )
        notification = await _notify(
            NotificationKind.FAILED,
            issue_key=issue_key,
            issue_summary=issue_summary,
            project_key=project.get("key", ""),
            situation=message,
            errors=[reason],
        )
        await _report(
            issue_key,
            headline="This issue could not be broken down.",
            situation=message,
            errors=[reason],
            notification=notification,
            context_notes=_unreadable_notes(source),
            conflicts=(source or {}).get("conflicts") or [],
            answered_comment_id=trigger_comment_id,
            # Every other stop that needs a person marks the ticket. This one
            # did not, so a run that failed here left no marker at all and the
            # ticket looked untouched next to one that had never been asked.
            mark_awaiting=True,
        )
        return _result(
            # A breakdown that could not be produced is a system failure, not a
            # verdict on the request — VALIDATION_ERROR rendered as "this is not
            # a work requirement" and suppressed every email.
            status=ResultStatus.JIRA_CREATION_FAILED,
            message=message,
            validation_errors=[reason],
            jira_context=context,
            source_issue=source,
            notification=notification,
        )

    breakdown = breakdown_result["breakdown"]
    classification = Classification(breakdown["classification"])
    generator = breakdown_result.get("generator", "")
    counts = breakdown_result.get("counts", {})
    duplicates = breakdown_result.get("duplicate_matches", [])
    best_match = breakdown_result.get("best_match")
    logger.info(f"classification : {classification.value} | counts : {counts}")

    # ---- Gate 3b: overlap with work that already exists ------------------
    # Duplicates are never created. The run stops, the situation is emailed,
    # and a human decides whether this really is new work.
    if duplicates:
        # Partly-existing work needs a different answer from wholly-existing
        # work: one asks "shall I build the rest?", the other says "this is done".
        overlap = breakdown_result.get("overlap") or {}
        remaining = overlap.get("remaining_titles") or []
        is_partial = bool(breakdown_result.get("overlap_is_partial"))

        covered = "; ".join(
            f'"{d["proposed_title"]}" is covered by {d["existing_key"]} '
            f'("{d["existing_summary"]}", {d.get("existing_status") or "status unknown"})'
            for d in duplicates
        )
        if is_partial:
            situation = (
                f"Part of this requirement already exists in {project.get('key')}: "
                f"{covered}. Still uncovered: {', '.join(remaining)}. "
                f"No Jira issues were created."
            )
            # Nothing reads a reply to these questions, so each one says the
            # exact comment that acts on it (BGV-32: "still uncovered" with no
            # way forward). No "@" + keyword: this text is posted as the agent's
            # own comment, and must never read as a fresh request.
            questions = [
                "To create only the missing part, add a new comment that mentions "
                f"the agent followed by: build {'; '.join(remaining)}",
                "If the existing tickets should cover it instead, extend them in "
                "Jira — nothing more is needed from the agent.",
            ]
            headline = "Nothing created — part of this work already exists."
        else:
            situation = (
                f"This requirement already exists in {project.get('key')}: {covered}. "
                f"No Jira issues were created."
            )
            questions = [
                "Is this genuinely new work? If so, reword the description so it "
                "is distinct from the tickets listed above and trigger the agent again.",
                "Otherwise, should the existing tickets be updated instead?",
            ]
            headline = "Nothing created — this work already exists."

        logger.info(f"Overlap stop (partial={is_partial}): {covered}")
        notification = await _notify(
            NotificationKind.PARTIAL_DUPLICATE if is_partial else NotificationKind.DUPLICATES_FOUND,
            issue_key=issue_key,
            issue_summary=issue_summary,
            project_key=project.get("key", ""),
            situation=situation,
            duplicates=duplicates,
            requirement=clean_requirement,
            remaining=remaining,
        )
        await _report(
            issue_key,
            headline=headline,
            situation=situation,
            duplicates=duplicates,
            questions=questions,
            notification=notification,
            context_notes=_unreadable_notes(source),
            conflicts=(source or {}).get("conflicts") or [],
            answered_comment_id=trigger_comment_id,
            mark_awaiting=True,
        )
        return _result(
            status=ResultStatus.CLARIFICATION_REQUIRED,
            message=situation,
            clarifying_questions=questions,
            classification=classification,
            analysis=breakdown.get("analysis", ""),
            epic=breakdown.get("epic"),
            stories=breakdown.get("stories", []),
            generator=generator,
            jira_context=context,
            duplicate_matches=duplicates,
            best_match=best_match,
            source_issue=source,
            notification=notification,
        )

    if not create_in_jira:
        logger.info("create_in_jira is false; returning the breakdown for review.")
        review_message = (
            f"{classification.value} requirement decomposed into "
            f"{counts.get('epics', 0)} Epic(s), {counts.get('stories', 0)} "
            f"Story/Stories and {counts.get('subtasks', 0)} Subtask(s). "
            f"Jira creation was not requested."
        )
        # Someone asked and got a full breakdown; saying nothing on the ticket
        # reads as the agent having ignored them. No email: nothing here needs
        # acting on, the breakdown is a preview.
        await _report(
            issue_key,
            headline="Breakdown ready — nothing was created.",
            situation=review_message,
            questions=[],
            context_notes=list(source.get("notes") or []) if source else [],
            answered_comment_id=trigger_comment_id,
            generator=generator,
        )
        return _result(
            status=ResultStatus.READY_FOR_JIRA,
            message=review_message,
            classification=classification,
            analysis=breakdown.get("analysis", ""),
            epic=breakdown.get("epic"),
            stories=breakdown.get("stories", []),
            generator=generator,
            jira_context=context,
            duplicate_matches=duplicates,
            best_match=best_match,
            source_issue=source,
        )

    # ---- Gate 4: Jira creation -------------------------------------------
    jira_result = await toolExecutor.execute(
        "create_jira_issues",
        breakdown,
        clean_requirement,
        project.get("key") or project_key,
        correlation_id,
        None,
        (epic_ctx or {}).get("key", ""),
        (sprint or {}).get("id"),
        # When the request was "break THIS ticket down", the ticket is the work
        # item. create_jira_issues attaches the sub-tasks to it instead of
        # creating a Story that repeats it — but only when the breakdown really
        # is one capability; several need Stories of their own.
        issue_key if (source or {}).get("trigger_is_pointer") else "",
        # The stated request, which is what identifies this piece of work.
        # Falls back to the bundle only when validation did not supply it.
        validation.get("request_text") or clean_requirement,
        # Linked back to what is created, so the ticket shows where it went.
        issue_key,
        start_to_close_timeout=_JIRA_TIMEOUT,
    )
    jira_status = jira_result.get("status", ResultStatus.JIRA_CREATION_FAILED.value)
    created_keys = jira_result.get("created_keys") or []
    logger.info(f"jira_status : {jira_status} | created_keys : {created_keys}")
    if jira_result.get("failure_reason"):
        logger.error(f"Jira failure: {jira_result.get('failure_reason')}")

    created_refs = [
        ref
        for ref in (
            [jira_result.get("epic")]
            + (jira_result.get("stories") or [])
            + (jira_result.get("subtasks") or [])
        )
        if ref
    ]

    if jira_status == ResultStatus.JIRA_CREATED.value:
        message = jira_result.get("summary", "Jira issues created.")
        attached = jira_result.get("attached_to")
        if attached:
            # Say plainly what was used and what was not. The old reply read
            # "Work breakdown created" after silently ignoring half the
            # instruction and building a duplicate ticket instead.
            message = (
                f"{message} The work was read from {attached}'s own description. "
                f"Its description was not edited — only sub-tasks were added."
            )
        if jira_result.get("sprint_error"):
            message += f" {jira_result['sprint_error']}"
        notification = await _notify(
            NotificationKind.CREATED,
            issue_key=issue_key,
            issue_summary=issue_summary,
            project_key=project.get("key", ""),
            # The email says it too. Someone who only reads the notification
            # would otherwise never learn the wording came from the fallback.
            situation=(f"{message}\n\n{_QUALITY_WARNING}" if generator == "heuristic" else message),
            created_keys=created_keys or jira_result.get("reused_keys") or [],
            counts=counts,
        )
        await _report(
            issue_key,
            headline=(
                "Reused the tickets created earlier for this requirement."
                if jira_result.get("reused_existing")
                else (
                    f"Sub-tasks added to {jira_result['attached_to']}"
                    if jira_result.get("attached_to")
                    else "Work breakdown created."
                )
            ),
            situation=message,
            created=created_refs,
            notification=notification,
            context_notes=_unreadable_notes(source),
            conflicts=(source or {}).get("conflicts") or [],
            answered_comment_id=trigger_comment_id,
            mark_processed=True,
            generator=generator,
        )
    else:
        if created_keys:
            message = (
                f"Jira creation stopped partway: {jira_result.get('failure_reason', '')} "
                f"Already created: {', '.join(created_keys)}. "
                f"Re-running the same requirement continues from there rather than "
                f"creating duplicates."
            )
        else:
            message = (
                f"Jira creation did not run: {jira_result.get('failure_reason', '')} "
                f"No issues were created, so re-running is safe."
            )
        notification = await _notify(
            NotificationKind.FAILED,
            issue_key=issue_key,
            issue_summary=issue_summary,
            project_key=project.get("key", ""),
            situation=message,
            created_keys=created_keys,
            errors=[jira_result.get("failure_reason", "")],
        )
        await _report(
            issue_key,
            headline="Jira creation did not complete.",
            situation=message,
            created=created_refs,
            errors=[jira_result.get("failure_reason", "")],
            notification=notification,
            context_notes=_unreadable_notes(source),
            conflicts=(source or {}).get("conflicts") or [],
            answered_comment_id=trigger_comment_id,
            generator=generator,
            mark_awaiting=True,
        )

    logger.info("Completed the execution of agent JiraTaskCreation")

    return _result(
        status=ResultStatus(jira_status),
        message=message,
        classification=classification,
        analysis=breakdown.get("analysis", ""),
        epic=breakdown.get("epic"),
        stories=breakdown.get("stories", []),
        jira_result=jira_result,
        generator=generator,
        quality_notes=breakdown_result.get("quality_notes") or [],
        jira_context=context,
        duplicate_matches=duplicates,
        best_match=best_match,
        source_issue=source,
        notification=notification,
    )
