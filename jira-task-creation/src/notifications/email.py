"""Gmail notifications for situations that need a human.

Three situations stop the pipeline and are reported by email:

* **clarification_required** — the requirement is missing something essential.
* **duplicates_found** — the work already exists in Jira. Nothing is created.
* **failed** — the agent could not complete for any other reason.

A fourth, **created**, is an optional success receipt; add ``created`` to
``LTW_NOTIFY_ON`` to turn it on.

Delivery is Gmail SMTP with an app password, read from the environment and
never logged. A send failure is reported in the result and never fails the run:
the Jira side is already decided by the time an email goes out, and losing the
tickets because a mail server hiccuped would be worse than a missed message.
"""

from __future__ import annotations

import asyncio
from typing import Any

from common_lib.utils.logger import setup_logger

from src.config.settings import env_float, env_list, mask
from src.models.schemas import NotificationKind, NotificationResult

# Delivery is shared — one sender for every agent (shared/mailer.py). The names
# below are kept so callers and tests that patch ``_send_sync`` keep working.
from src.shared.mailer import explain_failure, is_repeat, remember, smtp_settings
from src.shared.mailer import forget_recent_sends as forget_recent_sends  # re-exported
from src.shared.mailer import send_sync as _send_sync

logger = setup_logger(__name__)

SUBJECT_PREFIX = "[Work Breakdown]"

# Fallbacks, used only when the run gave nothing specific to count or quote.
# `_subject_for` prefers the specific form every time; these exist so a subject
# is never empty, not as the normal path.
_SUBJECTS = {
    NotificationKind.CLARIFICATION_REQUIRED: "{key} — Not created: details missing",
    NotificationKind.DUPLICATES_FOUND: "{key} — Not created: this work already exists",
    NotificationKind.PARTIAL_DUPLICATE: "{key} — Partly exists: confirmation needed",
    NotificationKind.INVALID_REQUEST: "{key} — Not a work requirement",
    NotificationKind.FAILED: "{key} — Failed: the request could not be processed",
    NotificationKind.CREATED: "{key} — Items created",
}


def _plural(count: int, noun: str, plural: str = "") -> str:
    """'1 story' / '2 stories'. An irregular plural is passed in, never guessed."""
    if count == 1:
        return f"{count} {noun}"
    return f"{count} {plural or noun + 's'}"


def _created_subject(key: str, created_keys: list[str], counts: dict[str, int]) -> str:
    """'FL-123 — 5 items created (1 epic, 2 stories, 2 sub-tasks)'.

    The reader should never have to open the mail to learn how much was made.
    """
    total = len(created_keys)
    parts = [
        _plural(counts.get(name, 0), noun, plural)
        for name, noun, plural in (
            ("epics", "epic", ""),
            ("stories", "story", "stories"),
            ("subtasks", "sub-task", ""),
        )
        if counts.get(name, 0)
    ]
    detail = f" ({', '.join(parts)})" if parts else ""
    return f"{key} — {_plural(total, 'item')} created{detail}"


def _subject_for(
    kind: NotificationKind,
    issue_key: str,
    *,
    questions: list[str],
    duplicates: list[dict[str, Any]],
    created_keys: list[str],
    errors: list[str],
    counts: dict[str, int],
) -> str:
    """The one line that has to carry the whole outcome.

    Every branch names a number or quotes the actual problem. A subject that
    says only "could not be processed" forces the reader into the body, and
    then into the logs, which is how a broken run stays invisible for hours.
    """
    key = issue_key or "a Jira issue"

    if kind is NotificationKind.CREATED and created_keys:
        body = _created_subject(key, created_keys, counts)
    elif kind is NotificationKind.CLARIFICATION_REQUIRED and questions:
        body = f"{key} — Not created: {_plural(len(questions), 'detail')} missing"
    elif kind is NotificationKind.DUPLICATES_FOUND and duplicates:
        first = str(duplicates[0].get("existing_key") or "").strip()
        match = f" (matches {first})" if first else ""
        body = f"{key} — Not created: this work already exists{match}"
    elif kind is NotificationKind.PARTIAL_DUPLICATE and duplicates:
        body = (
            f"{key} — Partly exists: {_plural(len(duplicates), 'item')} "
            f"already there, confirmation needed"
        )
    elif kind is NotificationKind.FAILED and errors:
        # The first error is the one that stopped the run; the rest are fallout.
        reason = " ".join(str(errors[0]).split())
        if len(reason) > 80:
            reason = f"{reason[:77]}..."
        body = f"{key} — Failed: {reason}"
    else:
        body = _SUBJECTS[kind].format(key=key)

    return f"{SUBJECT_PREFIX} {body}"


def _mask(secret: str) -> str:
    """Show only the shape of a secret in logs — never the value.

    Nothing is revealed: a Gmail app password is short enough that even a few
    characters narrow it usefully.
    """
    return mask(secret)


def recipients() -> list[str]:
    """Comma-separated recipients from ``LTW_NOTIFY_EMAILS``."""
    return env_list("LTW_NOTIFY_EMAILS")


def admin_recipients() -> list[str]:
    """Who hears about a system failure, from ``LTW_ADMIN_EMAILS``.

    A failed run is an operations problem, not a project update: the Jira token
    expired, the Sub-task type is missing, the API refused a write. Sending that
    to everyone who wanted to hear about their own requirements trains the whole
    team to filter the agent out. Unset falls back to the normal list, so an
    unconfigured install still hears about breakage.
    """
    return env_list("LTW_ADMIN_EMAILS") or recipients()


def recipients_for(kind: NotificationKind) -> list[str]:
    """The audience for one kind of email."""
    return admin_recipients() if kind is NotificationKind.FAILED else recipients()


# One setting decides every email, so "when does this thing mail me?" has a
# single answer.
#
# The default is "an email means you have something to do": the run stopped and
# is waiting on a person. A successful creation needs no chasing — the comment
# on the ticket is the record — and a comment that was never a requirement is
# not an event at all.
DEFAULT_NOTIFY_ON = ("clarification", "duplicates", "failed")

_KIND_EVENT = {
    NotificationKind.CREATED: "created",
    NotificationKind.FAILED: "failed",
    NotificationKind.CLARIFICATION_REQUIRED: "clarification",
    NotificationKind.DUPLICATES_FOUND: "duplicates",
    NotificationKind.PARTIAL_DUPLICATE: "duplicates",
}


def notify_on() -> tuple[str, ...]:
    """Which events email, from ``LTW_NOTIFY_ON``.

    Comma-separated, any of ``created``, ``failed``, ``clarification``,
    ``duplicates``. Unset means the default above.
    """
    chosen = tuple(item.lower() for item in env_list("LTW_NOTIFY_ON"))
    return chosen or DEFAULT_NOTIFY_ON


def should_notify(kind: NotificationKind) -> bool:
    """Whether this outcome is worth an email.

    A comment that was never a requirement — "hi", "who is the PM?" — is not
    configurable and never mails. Treating idle chatter as something to alert a
    human about is how a useful agent becomes one people mute.
    """
    if kind is NotificationKind.INVALID_REQUEST:
        return False
    event = _KIND_EVENT.get(kind)
    return bool(event) and event in notify_on()


def is_configured() -> bool:
    _, _, sender, password = smtp_settings()
    return bool(sender and password and recipients())


# --------------------------------------------------------------------------
# Not mailing the same person the same thing twice
# --------------------------------------------------------------------------
# A failing workflow is retried by Temporal indefinitely, and every retry
# reaches this module with identical arguments. Left alone that is one email
# per retry: a stuck FL-94 produced runs every few seconds for hours.
#
# The ledger is per worker process and deliberately so. It needs no storage, no
# migration and nothing to clean up, and a retry storm is served by one worker,
# which is the case that actually hurts. A genuine re-request minutes later
# still gets through, because the window is short and the key includes the
# subject — a different outcome says something different and is not a repeat.

DEFAULT_REPEAT_WINDOW_SECONDS = 900  # 15 minutes


def repeat_window_seconds() -> float:
    """How long an identical email is suppressed for. 0 disables suppression."""
    return max(env_float("LTW_EMAIL_REPEAT_WINDOW", DEFAULT_REPEAT_WINDOW_SECONDS), 0.0)


def _is_repeat(kind: NotificationKind, issue_key: str, subject: str) -> bool:
    """True when this exact email already went out inside the window."""
    return is_repeat((kind.value, issue_key, subject), repeat_window_seconds())


def _remember_send(kind: NotificationKind, issue_key: str, subject: str) -> None:
    """Record a message that actually went out."""
    remember((kind.value, issue_key, subject), repeat_window_seconds())


def _issue_line(base_url: str, key: str) -> str:
    return f"{base_url}/browse/{key}" if base_url and key else key


def _indent(text: str, prefix: str = "  ") -> str:
    """Indent a block so the original request reads as quoted material."""
    return "\n".join(f"{prefix}{line}" for line in (text or "").strip().splitlines())


def build_body(
    kind: NotificationKind,
    *,
    issue_key: str,
    issue_summary: str = "",
    project_key: str = "",
    base_url: str = "",
    situation: str = "",
    questions: list[str] | None = None,
    duplicates: list[dict[str, Any]] | None = None,
    created_keys: list[str] | None = None,
    errors: list[str] | None = None,
    requirement: str = "",
    remaining: list[str] | None = None,
) -> str:
    """Render the email body.

    Every template answers the same three questions in the same order: what
    happened, what the agent did about it, and what the reader should do next.
    """
    lines = [
        f"Jira issue : {_issue_line(base_url, issue_key)}",
    ]
    if issue_summary:
        lines.append(f"Summary    : {issue_summary}")
    if project_key:
        lines.append(f"Project    : {project_key}")
    lines.append("")

    if kind is NotificationKind.CLARIFICATION_REQUIRED:
        lines.append("SITUATION")
        lines.append(
            situation
            or "The description does not contain enough information to break the work down."
        )
        lines.append("")
        lines.append("NO JIRA ISSUES WERE CREATED.")
        lines.append("")
        if requirement:
            lines.append("THE REQUEST")
            lines.append(_indent(requirement))
            lines.append("")
        if questions:
            lines.append("The agent needs answers to:")
            lines.extend(f"  {n}. {q}" for n, q in enumerate(questions, start=1))
            lines.append("")
        lines.append(
            "WHAT TO DO NEXT\n"
            "Add the answers to the Jira issue description (or as a comment) and\n"
            "mention the trigger keyword again. The agent will re-read the ticket\n"
            "and continue from there."
        )

    elif kind in (NotificationKind.DUPLICATES_FOUND, NotificationKind.PARTIAL_DUPLICATE):
        partial = kind is NotificationKind.PARTIAL_DUPLICATE
        lines.append("SITUATION")
        lines.append(
            situation
            or (
                "Part of the requested work already exists in this project."
                if partial
                else "The requested work already exists in this project."
            )
        )
        lines.append("")
        lines.append("NOTHING WAS CREATED.")
        lines.append("")

        if requirement:
            lines.append("THE REQUEST")
            lines.append(_indent(requirement))
            lines.append("")

        if duplicates:
            lines.append("ALREADY COVERED BY")
            for match in duplicates:
                key = match.get("existing_key", "")
                where = "title" if match.get("matched_on") == "summary" else "description"
                lines.append(
                    f'  - {key} [{match.get("existing_type") or "Issue"}] '
                    f'"{match.get("existing_summary", "")}"'
                )
                lines.append(f'      status     : {match.get("existing_status") or "unknown"}')
                lines.append(
                    f"      link       : "
                    f'{match.get("existing_url") or _issue_line(base_url, key)}'
                )
                lines.append(f'      covers     : "{match.get("proposed_title", "")}"')
                lines.append(
                    f"      matched on : the existing {where} "
                    f'(similarity {match.get("score", 0)})'
                )
            lines.append("")

        if partial and remaining:
            lines.append("NOT YET COVERED — this part of the request is new")
            lines.extend(f"  - {item}" for item in remaining)
            lines.append("")
            lines.append(
                "WHAT TO DO NEXT\n"
                "Decide how to handle the overlap, then either:\n"
                "  a) reword the ticket so it asks only for the uncovered work above, or\n"
                "  b) extend the existing tickets instead.\n"
                "Update the Jira issue and mention the keyword again to re-run."
            )
        else:
            lines.append(
                "WHAT TO DO NEXT\n"
                "Review the existing tickets above. If this really is new work, reword\n"
                "the description so it is distinct from them and trigger the agent again."
            )

    elif kind is NotificationKind.INVALID_REQUEST:
        lines.append("SITUATION")
        lines.append(situation or "This request is not something that can be broken down.")
        lines.append("")
        lines.append("NO JIRA ISSUES WERE CREATED.")
        lines.append("")
        if requirement:
            lines.append("THE REQUEST")
            lines.append(_indent(requirement))
            lines.append("")
        lines.append(
            "WHAT TO DO NEXT\n"
            "Describe what people need to be able to do, for example:\n"
            '  "@Aetherion Allow drivers to record a rest break."'
        )

    elif kind is NotificationKind.FAILED:
        lines.append("SITUATION")
        lines.append(situation or "The agent could not process this issue.")
        lines.append("")
        lines.append("NO JIRA ISSUES WERE CREATED." if not created_keys else "PARTIAL RESULT.")
        lines.append("")
        if errors:
            lines.append("Reported problems:")
            lines.extend(f"  - {e}" for e in errors if e)
            lines.append("")
        if created_keys:
            lines.append("Issues that were created before the failure:")
            lines.extend(f"  - {_issue_line(base_url, k)}" for k in created_keys)
            lines.append("")
            lines.append(
                "Re-running the same issue continues from these rather than duplicating them."
            )
            lines.append("")
        lines.append(
            "WHAT TO DO NEXT\n"
            "Fix the problem above, then trigger the agent again on the same issue."
        )

    else:  # CREATED
        lines.append("SITUATION")
        lines.append(situation or "The work breakdown was created successfully.")
        lines.append("")
        if created_keys:
            lines.append("Created in Jira:")
            lines.extend(f"  - {_issue_line(base_url, k)}" for k in created_keys)
            lines.append("")
        lines.append("WHAT TO DO NEXT\nReview the generated tickets and adjust as needed.")

    return "\n".join(lines)


async def send(
    kind: NotificationKind,
    *,
    issue_key: str,
    issue_summary: str = "",
    project_key: str = "",
    base_url: str = "",
    situation: str = "",
    questions: list[str] | None = None,
    duplicates: list[dict[str, Any]] | None = None,
    created_keys: list[str] | None = None,
    errors: list[str] | None = None,
    requirement: str = "",
    remaining: list[str] | None = None,
    counts: dict[str, int] | None = None,
) -> NotificationResult:
    """Send one notification. Never raises.

    Returns a result describing what was attempted, so the agent can report an
    undelivered email instead of silently swallowing it.
    """
    host, port, sender, password = smtp_settings()
    to = recipients_for(kind)
    subject = _subject_for(
        kind,
        issue_key,
        questions=questions or [],
        duplicates=duplicates or [],
        created_keys=created_keys or [],
        errors=errors or [],
        counts=counts or {},
    )

    logger.info(f"Notification requested: kind={kind.value} recipients={len(to)}")

    # The decision lives here because the agent runs in a workflow sandbox and
    # cannot read the environment itself: it always asks, and this decides
    # whether anything is actually sent.
    if not should_notify(kind):
        logger.info(f"Email suppressed for {kind.value}; LTW_NOTIFY_ON={list(notify_on())}")
        return NotificationResult(attempted=False, kind=kind, subject=subject, error="")

    if _is_repeat(kind, issue_key, subject):
        logger.info(
            f"Email suppressed for {kind.value}: the identical message for {issue_key or '-'} "
            f"went out within the last {int(repeat_window_seconds())}s (retry)."
        )
        return NotificationResult(attempted=False, kind=kind, subject=subject, error="")
    logger.info(f"GMAIL_SENDER: {sender or '<empty>'} | GMAIL_APP_PASSWORD: {_mask(password)}")

    if not (sender and password):
        return NotificationResult(
            attempted=False,
            kind=kind,
            subject=subject,
            error="Email not sent: GMAIL_SENDER or GMAIL_APP_PASSWORD is not set.",
        )
    if not to:
        return NotificationResult(
            attempted=False,
            kind=kind,
            subject=subject,
            error="Email not sent: LTW_NOTIFY_EMAILS is empty.",
        )

    body = build_body(
        kind,
        issue_key=issue_key,
        issue_summary=issue_summary,
        project_key=project_key,
        base_url=base_url,
        situation=situation,
        questions=questions,
        duplicates=duplicates,
        created_keys=created_keys,
        errors=errors,
        requirement=requirement,
        remaining=remaining,
    )

    try:
        await asyncio.to_thread(_send_sync, host, port, sender, password, to, subject, body)
    except Exception as exc:  # noqa: BLE001 — a mail failure must not fail the run
        reason = explain_failure(exc)
        logger.error(reason)
        return NotificationResult(
            attempted=True, kind=kind, recipients=to, subject=subject, error=reason
        )

    _remember_send(kind, issue_key, subject)
    logger.info(f"Notification sent to {len(to)} recipient(s): {subject}")
    return NotificationResult(attempted=True, sent=True, kind=kind, recipients=to, subject=subject)
