"""Building the one reply, from the contract the child returned.

**The composer never branches on which agent produced the result.** It reads
``outcome`` → section layout from one table and everything else from the
contract's fields. That is what makes a fourth agent a catalog entry rather than
a code change, and it is asserted directly by a test that feeds it a synthetic
agent this module has never heard of.

The envelope is identical in every reply, with no exceptions:

    AetherionAgent · <headline>

    Handled by
    Jira Orchestration → <Display Name>

    Routed to
    <why, in one sentence, naming the layer that decided>

    <… outcome-specific sections …>

    — AetherionAgent · automated reply

Three rules about the envelope that are load-bearing rather than cosmetic:

* **"Handled by" names no child unless that child actually ran.** The router
  knows which agent it *chose*; claiming it *ran* when it timed out is a lie the
  reader cannot check.
* **The run_id never appears in the comment.** It is an internal correlation
  id — it belongs in every log line and in the email, so a run can be traced —
  but it means nothing to a person reading a Jira ticket and only clutters the
  reply. ``handled_by`` takes no run_id parameter at all, so it structurally
  cannot leak in.
* **The footer must never match the mention filter.** ``AetherionAgent`` is
  spelled so that the whole-word matcher for ``Aetherion`` cannot fire on it.
  Without that, the system answers its own reply, forever.

Pure — no I/O, no environment. It runs inside the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.contract import AgentResult, MissingSource, Outcome
from shared.keywords import AGENT_FOOTER, AGENT_SIGNATURE

ROUTER_DISPLAY_NAME = "Jira Orchestration"


@dataclass(frozen=True)
class Section:
    heading: str
    body: str | list[str]


# Which sections an outcome gets, in order. ONE table — the composer consults
# this and never asks who produced the result.
#
# A section named here is rendered only if it has content, so an outcome that
# happens to have no duplicates simply omits that heading rather than showing an
# empty one.
#
# "read" and "missing" appear on EVERY outcome (Bug 6): the contract's own
# docstring calls sources_read/sources_missing mandatory on every run, but
# FAILED and NOT_A_REQUIREMENT used to leave them out of the layout — so a
# child that read three sources before crashing, or that already knew a
# linked spec was never supplied, had that silently dropped. A section that
# has no content renders nothing regardless, so this costs nothing on the
# outcomes where they are typically empty (NOT_A_REQUIREMENT).
LAYOUT: dict[Outcome, tuple[str, ...]] = {
    # "confirm": what the input did not say (D1). A build that found nothing
    # missing says so, rather than leaving the reader to wonder.
    Outcome.CREATED: ("what_happened", "created", "confirm", "duplicates", "read", "missing"),
    # "questions" too: a partial duplicate asks whether to build the rest. The
    # questions used to reach the reply only inside the child's status block;
    # once that block was dropped they vanished (BGV-32, 2026-09-24).
    Outcome.ALREADY_EXISTS: ("what_happened", "duplicates", "questions", "read", "missing"),
    # "behaviour", "cause", "files", "fix", "info": a bug investigation
    # (2026-10-22); each is left out for an ordinary review.
    Outcome.REVIEWED: (
        "readiness",
        "behaviour",
        "cause",
        "files",
        "fix",
        "info",
        "findings",
        "questions",
        "read",
        "missing",
    ),
    Outcome.NEEDS_INFO: ("what_happened", "questions", "read", "missing"),
    Outcome.NOT_A_REQUIREMENT: ("what_happened", "read", "missing"),
    Outcome.FAILED: ("what_happened", "read", "missing", "problems"),
    Outcome.ANSWERED: ("answer", "read", "missing"),
    # GENERATE_LOCAL_PDF: the breakdown as a PDF, nothing created in Jira.
    Outcome.PLANNED: ("what_happened", "proposed", "confirm", "duplicates", "read", "missing"),
}

# Used when a child returns an outcome this build does not know. Better a plain
# reply than no reply: the router owns the comment, so it cannot simply give up.
FALLBACK_LAYOUT = ("what_happened", "read", "missing", "problems")

_DEGRADED_WARNING = (
    "⚠ Please review the wording. The model was unreachable, so this was "
    "generated from a local fallback and reads more woodenly than usual."
)


def _lines(items: list[str]) -> list[str]:
    return [item for item in items if item and item.strip()]


def _missing_lines(missing: list[MissingSource]) -> list[str]:
    return _lines([m.as_sentence() for m in missing])


def handled_by(display_names: list[str]) -> str:
    """The attribution line.

    With no child — because none ran, or none was meant to — it names only the
    router. With several, it names them in execution order, which is what makes
    a BOTH route readable as one story rather than two events.

    Deliberately takes no run_id. The run_id is an internal correlation id: it
    belongs in every log line and in the email, so a run can be traced, but it
    means nothing to someone reading a Jira comment and must never appear in
    one — see ``test_no_run_id_shaped_token_reaches_a_comment``.
    """
    who = ROUTER_DISPLAY_NAME
    names = _lines(display_names)
    if names:
        who += " → " + ", then ".join(names)
    return who


def _section(name: str, result: AgentResult) -> Section | None:
    """One section's content, or None when there is nothing to say."""
    if name == "what_happened":
        return Section("What happened", result.summary) if result.summary else None

    if name == "created":
        items = [
            f"{c.issue_type} {f'[{c.key}]({c.url})' if c.url else c.key} — {c.summary}".strip(" —")
            for c in result.created
        ]
        return Section("Created in Jira", items) if items else None

    if name == "duplicates":
        # No proposed title means "this exists" rather than "this matches":
        # the tickets an identical earlier request created.
        items = [
            (f"{d.proposed_title} matches {d.existing_key}" if d.proposed_title else d.existing_key)
            + (f" — {d.existing_summary}" if d.existing_summary else "")
            + (f" (score {d.score:.2f})" if d.score else "")
            for d in result.duplicates
        ]
        return Section("Work that already exists", items) if items else None

    if name == "confirm":
        items = _lines(result.questions)
        if items:
            return Section("Details I could not determine — please confirm", items)
        return Section(
            "Details I could not determine — please confirm",
            "Nothing was missing — every detail came from your description.",
        )

    if name == "answer":
        return Section("Answer", result.summary) if result.summary else None

    if name == "readiness":
        return Section("Readiness", result.summary) if result.summary else None

    if name in ("behaviour", "cause", "files", "fix", "info"):
        ready = (result.readiness_score or 0) >= 4
        return _investigation_section(name, result.investigation or {}, ready)

    if name == "findings":
        items = []
        for finding in result.findings:
            category = str(finding.get("category") or "").replace("_", " ").strip()
            text = str(finding.get("description") or finding.get("title") or "").strip()
            if not text:
                continue
            items.append(f"{category.capitalize()}: {text}" if category else text)
        return Section(f"Findings ({len(items)})", items) if items else None

    if name == "questions":
        items = _lines(result.questions)
        return Section("What I need to know", items) if items else None

    if name == "read":
        items = _lines(result.sources_read)
        return Section("What I read", items) if items else None

    if name == "missing":
        items = _missing_lines(result.sources_missing)
        return Section("What was missing or unreadable", items) if items else None

    if name == "proposed":
        items = _lines(result.proposed)
        return Section("Proposed — not created in Jira", items) if items else None

    if name == "problems":
        items = _lines(result.errors)
        return Section("Problems", items) if items else None

    return None


def _investigation_section(name: str, found: dict, ready: bool = False) -> Section | None:
    """One part of a bug investigation, or None when it has nothing to say."""
    if not found:
        return None

    if name == "behaviour":
        items = _lines(
            [
                f"Actual: {found.get('actual')}" if found.get("actual") else "",
                f"Expected: {found.get('expected')}" if found.get("expected") else "",
            ]
        )
        return Section("What is wrong", items) if items else None

    if name == "cause":
        cause = str(found.get("root_cause") or "").strip()
        if not cause:
            return None
        confidence = str(found.get("root_cause_confidence") or "").strip().lower()
        heading = (
            f"Likely root cause ({confidence} confidence)" if confidence else "Likely root cause"
        )
        evidence = _lines([str(e) for e in found.get("evidence") or []])
        return Section(heading, [cause, *[f"Evidence: {e}" for e in evidence]])

    if name == "files":
        items = []
        for entry in found.get("affected_files") or []:
            if isinstance(entry, dict):
                path = str(entry.get("path") or "").strip()
                why = str(entry.get("why") or "").strip()
                if path:
                    items.append(f"{path} — {why}" if why else path)
            elif str(entry).strip():
                items.append(str(entry).strip())
        note = str(found.get("code_note") or "").strip()
        if not items and note:
            return Section("Affected files", note)
        if note:
            items.append(note)
        return Section("Affected files", items) if items else None

    if name == "fix":
        fix = str(found.get("proposed_fix") or "").strip()
        if not fix:
            return None
        return Section(
            "Proposed fix — not implemented",
            [
                fix,
                'Nothing was changed. Say "@Aetherion create TDD" for the design, or '
                '"@Aetherion code" to implement it.',
            ],
        )

    if name == "info":
        items = _lines([str(i) for i in found.get("missing_information") or []])
        if not items:
            return None
        # A report that is ready to fix needs nothing more; anything left is
        # worth checking while fixing, not a reason to wait (BTS-31, 2026-10-23).
        title = (
            "Worth confirming while fixing (not blocking)" if ready else "Information still needed"
        )
        return Section(title, items)

    return None


def compose(
    result: AgentResult,
    *,
    routed_to: str,
    ran: list[str] | None = None,
    notification: str = "",
) -> list[tuple[str, object]]:
    """The whole reply, as ``(heading, body)`` blocks ready for the ADF builder.

    ``ran`` is the display names of the children that actually completed. It is
    passed in rather than read from ``result.display_name`` because only the
    router knows whether the child finished — a timed-out child still has a name.

    No ``run_id`` parameter, deliberately — see ``handled_by``.
    """
    blocks: list[tuple[str, object]] = [
        (f"{AGENT_SIGNATURE} · {result.headline}", ""),
    ]

    if result.degraded:
        reason = result.degraded_reason.strip()
        blocks.append(("", f"{_DEGRADED_WARNING}{(' (' + reason + ')') if reason else ''}"))

    blocks.append(("Handled by", handled_by(ran or [])))
    blocks.append(("Routed to", routed_to))

    layout = LAYOUT.get(result.outcome, FALLBACK_LAYOUT)
    for name in layout:
        section = _section(name, result)
        if section:
            blocks.append((section.heading, section.body))

    if notification:
        blocks.append(("Notification", notification))

    blocks.append(("", AGENT_FOOTER))
    return blocks


# --------------------------------------------------------------------------
# Replies the router writes itself, when no child ran
# --------------------------------------------------------------------------


def router_only(
    headline: str,
    *,
    routed_to: str,
    what_happened: str = "",
    options: list[str] | None = None,
    problems: list[str] | None = None,
    notification: str = "",
) -> list[tuple[str, object]]:
    """A reply with no child involved — ambiguous, chatter, help, or a dead child.

    "Handled by" deliberately names the router alone: no arrow, no child name.
    No ``run_id`` parameter — see ``handled_by``.

    ``what_happened`` and ``problems`` share their heading text with
    ``compose()``'s own sections (Bug 3: a dispatch failure must be able to
    name the real underlying error, not just say "did not respond" with
    nowhere to put the detail).
    """
    blocks: list[tuple[str, object]] = [
        (f"{AGENT_SIGNATURE} · {headline}", ""),
        ("Handled by", handled_by([])),
        ("Routed to", routed_to),
    ]
    if what_happened:
        blocks.append(("What happened", what_happened))
    if options:
        blocks.append(("", options))
    problems = _lines(problems or [])
    if problems:
        blocks.append(("Problems", problems))
    if notification:
        blocks.append(("Notification", notification))
    blocks.append(("", AGENT_FOOTER))
    return blocks


AMBIGUOUS_OPTIONS = [
    '"@Aetherion build" — I create the Epic, Stories and Sub-tasks',
    '"@Aetherion review" — I assess how clear it is and list what is missing, ' "creating nothing",
]

# What a Bug card can ask for instead of a build (2026-10-22).
BUG_OPTIONS = [
    '"@Aetherion investigate" — find the likely root cause, the affected files and a '
    "proposed fix, creating nothing",
    '"@Aetherion create TDD" — write the Technical Design Document for the fix',
    '"@Aetherion code" — implement the fix from the TDD, when you are ready',
]

HELP_OPTIONS = [
    '"@Aetherion build <requirement>" — break the requirement, or this ticket\'s requirements, '
    "into Jira work items (a Story gets Sub-tasks, an Epic gets Stories)",
    '"@Aetherion review" — review this ticket: findings, gaps, missing information and open '
    "questions. Answer them in the comments and ask again",
    '"@Aetherion investigate" — on a bug: find the likely root cause, the affected files '
    "and a proposed fix, creating nothing",
    '"@Aetherion explain this card" — explain what this ticket is asking for',
    '"@Aetherion explain why this approach and why not another approach" — the Planning agent '
    "explains the design's reasoning and weighs the alternative",
    '"@Aetherion create TDD" — on a Story: check the requirement is ready, then write the '
    "Technical Design Document in Confluence and link it here",
    '"@Aetherion code" — start coding from the Confluence TDD, after confirming the branches; '
    "the pull request is linked here and on the TDD page",
    '"@Aetherion status" — what has happened on this ticket so far, and what is running now',
    '"@Aetherion help" — show this list',
]


# How many of a ticket's steps the status reply shows, newest last.
STATUS_STEPS = 15


def status_blocks(issue_key: str, state: dict) -> list[tuple[str, object]]:
    """The reply to "@Aetherion status" (2026-10-15): the job running now, if any,
    and the ticket's latest steps — each with its time and run id."""
    blocks: list[tuple[str, object]] = [(f"{AGENT_SIGNATURE} · Status of {issue_key}", "")]
    active = state.get("active") or {}
    blocks.append(
        (
            "Running now",
            (
                f"{active.get('intent', 'A job')} — started {active.get('started', '')} "
                f"(run {active.get('run_id', '')})"
                if active
                else "Nothing is running on this ticket."
            ),
        )
    )
    steps = list(state.get("history") or [])[-STATUS_STEPS:]
    blocks.append(
        (
            "What has happened",
            [
                f"{s.get('at', '')} — {s.get('step', '')} {s.get('status', '')}"
                + (f": {s['detail']}" if s.get("detail") else "")
                + f" (run {s.get('run_id', '')})"
                for s in steps
            ]
            or ["Nothing has been asked of the agents on this ticket yet."],
        )
    )
    blocks.append(("", AGENT_FOOTER))
    return blocks
