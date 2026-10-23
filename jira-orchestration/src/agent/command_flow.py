"""The TDD, design and code commands, step by step (2026-10-08).

    @Aetherion create TDD      on the Jira story: Requirement Review checks it is ready
                               (4/5 by default); then Planning writes the TDD and it is
                               published in the project's Confluence space, linked on the story
    @Aetherion explain why …   on the TDD page (or the story): Planning answers from the TDD,
    @Aetherion why not …       the discussion and the code, and says if the design should change
    @Aetherion update the TDD  Planning revises the design with what the discussion agreed;
                               the page gets a new version
    @Aetherion code            the branches are proposed and confirmed first; then Coding
                               implements the TDD and raises a pull request, linked on the page
                               and on the story. A design Coding cannot implement goes back to
                               Planning for a revision, and Coding tries again.

The same sequence answers a command asked on the story or on the page: what
differs — where the reply goes — is passed in (``say``). SDK-free like
``router_flow``: the executors are passed in. Rules are in ``routing/commands.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio.common import RetryPolicy

from routing import pipeline as rules
from routing.catalog import AgentSpec
from routing.classify import strip_mention
from routing.commands import BranchPlan, branch_question, parse_code_command
from shared.contract import AgentResult, Outcome
from shared.keywords import AGENT_FOOTER, AGENT_SIGNATURE

logger = logging.getLogger(__name__)

Execute = Callable[..., Awaitable[dict]]
Dispatch = Callable[..., Awaitable[Any]]
Blocks = list[tuple[str, object]]
# say(blocks): show where the command is — the "working on it" reply, edited.
Say = Callable[[Blocks], Awaitable[None]]

_TOOL = timedelta(minutes=2)
PLANNING, CODING, REVIEW = "Planning", "Coding", "Requirement Review"


@dataclass
class Command:
    """One command, wherever it was asked."""

    intent: str  # TDD | DESIGN | UPDATE_TDD | CODE
    text: str  # the comment, as written
    issue_key: str  # the story ("" until known, for a page comment)
    run_id: str
    execute: Execute
    dispatch: Dispatch
    say: Say
    settings: dict[str, Any]
    keyword: str = "Aetherion"
    review_spec: AgentSpec | None = None
    page_id: str = ""  # the TDD page, when asked there
    comment_id: str = ""  # the Jira comment that asked, for Review to read
    previous_questions: list[str] = field(default_factory=list)
    # A review of the unchanged story, already done, to reuse (a dict, or None).
    reusable_review: dict[str, Any] | None = None
    # What the story says now (summary, description, attachments, people's
    # comments), hashed; "" when asked on a TDD page.
    fingerprint: str = ""


@dataclass
class CommandResult:
    """What the command did: the reply, who ran, and anything to keep."""

    blocks: Blocks
    ran: list[str] = field(default_factory=list)
    review: dict[str, Any] | None = None  # a review run on the way (kept for reuse)
    links: dict[str, str] = field(default_factory=dict)


def reply(headline: str, *sections: tuple[str, object]) -> Blocks:
    """A reply in the same envelope as every other: headline, sections, signature."""
    return [
        (f"{AGENT_SIGNATURE} · {headline}", ""),
        *[s for s in sections if s[1]],
        ("", AGENT_FOOTER),
    ]


async def run_command(cmd: Command) -> CommandResult:
    flows = {"TDD": create_tdd, "DESIGN": design_question, "UPDATE_TDD": update_tdd, "CODE": code}
    flow = flows.get(cmd.intent)
    if flow is None:
        return CommandResult(
            reply("Not a command I know", ("What happened", f"{cmd.intent} has no flow."))
        )
    try:
        result = await flow(cmd)
    except Exception as exc:  # noqa: BLE001 — a command always ends in a reply
        logger.error(f"run {cmd.run_id}: {cmd.intent} failed: {exc}", exc_info=True)
        result = CommandResult(
            reply(
                "Could not complete this request",
                ("Problems", [f"{type(exc).__name__}: {str(exc)[:300]}"]),
            )
        )
    await _monitor(cmd, result)
    return result


# What a coding run ending like this means: nobody gets working code without a person.
_CODING_GAVE_UP = ("tests FAILED", "No pull request", "Could not complete", "did not respond")


async def _monitor(cmd: Command, result: CommandResult) -> None:
    """The step in the ticket's history, and an alert when coding gave up (2026-10-15).

    Recorded here, once for every command, so ``@Aetherion status`` shows a TDD
    published, a design question answered, a pull request raised or refused."""
    headline = _headline(result)
    link = result.links.get("pr") or result.links.get("tdd") or ""
    await _tool(cmd, "record_step", cmd.issue_key, cmd.intent, headline, link, cmd.run_id)
    if cmd.intent == "CODE" and any(marker in headline for marker in _CODING_GAVE_UP):
        await _tool(
            cmd,
            "notify_admins",
            f"Coding gave up on {cmd.issue_key or 'a TDD page'}",
            f"{headline}. {link or 'No pull request is usable.'} A person is needed.",
            cmd.run_id,
        )


def _headline(result: CommandResult) -> str:
    """The reply's first line, without the agent's signature."""
    first = str(result.blocks[0][0]) if result.blocks else ""
    return first.split(" · ", 1)[-1].strip()


# --- create TDD ------------------------------------------------------------------------


async def create_tdd(cmd: Command) -> CommandResult:
    story = await _tool(cmd, "story_context", cmd.issue_key)
    if not story.get("ok"):
        return CommandResult(
            reply("Could not read the story", ("Problems", [story.get("error", "")]))
        )
    if story.get("is_subtask"):
        parent = story.get("parent_key") or "its parent story"
        return CommandResult(
            reply(
                "Ask for the TDD on the story",
                (
                    "What happened",
                    f"{cmd.issue_key} is a sub-task. A TDD is written for a story — comment "
                    f'"@{cmd.keyword} create TDD" on {parent}. Nothing was created.',
                ),
            )
        )
    if not cmd.settings.get("repos"):
        return CommandResult(
            reply(
                "No repositories to design against",
                ("Problems", ["ORCH_PLANNING_REPOS is not set, so Planning has no code to read."]),
            )
        )
    unchanged = await _unchanged_tdd(cmd, story)
    if unchanged is not None:
        return unchanged

    ran: list[str] = []
    await cmd.say(_working("Checking the story is ready (Requirement Review)…", "1 of 3"))
    review = await _readiness(cmd, ran)
    minimum = int(cmd.settings.get("tdd_min_readiness") or 0)
    points = rules.review_points(review) if review else []
    score = review.readiness_score if review else 0
    ready = (
        review is not None
        and review.outcome is Outcome.REVIEWED
        and (not minimum or score >= minimum)
    )
    if not ready:
        why = (
            f"The review scored {cmd.issue_key} {score}/5; {minimum}/5 is needed to design it."
            if review is not None and review.outcome is Outcome.REVIEWED
            else "The readiness review could not run, so the story was not designed."
        )
        return CommandResult(
            reply(
                f"Not ready for a TDD yet — {score}/5" if score else "Not ready for a TDD yet",
                ("What happened", why + " Answer these in the comments, then ask again:"),
                ("Open questions", points or [review.summary if review else "See the review."]),
            ),
            ran,
            review.to_dict() if review else None,
        )

    await cmd.say(
        _working(f"Ready ({score}/5). Planning is writing the design from the code…", "2 of 3")
    )
    text = await _tool(
        cmd,
        "planning_input",
        cmd.issue_key,
        list(story.get("subtask_keys") or []),
        [],
        review.to_dict(),
    )
    if not text.get("issue_text"):
        return CommandResult(
            reply("Could not read the story for Planning", ("Problems", [text.get("error", "")])),
            ran,
        )
    earlier = await _earlier_designs(cmd)
    document, problem = await _plan(
        cmd, str(text["issue_text"]) + _earlier_text(earlier), attempt=1
    )
    if not document:
        return CommandResult(
            reply("Planning wrote no design", ("Problems", [problem])), ran, review.to_dict()
        )
    ran.append(PLANNING)

    await cmd.say(_working("Publishing the TDD in Confluence…", "3 of 3"))
    page = await _publish(cmd, story, document, earlier)
    if not page.get("ok"):
        return CommandResult(
            reply("The TDD could not be published", ("Problems", [page.get("error", "")])), ran
        )
    where = f"{page.get('space_name') or page['space_key']} ({page['space_key']})"
    return CommandResult(
        reply(
            f"{page['title']} ready" if page.get("title") else "Technical Design Document ready",
            (
                "What happened",
                f"Readiness {score}/5. The Planning agent wrote the design from {cmd.issue_key}, "
                f"its sub-tasks and the code, and it is in Confluence: "
                f"[{page['title']}]({page['url']})"
                + (" (a new version of the existing page)." if not page.get("created") else "."),
            ),
            (
                "Space",
                f"{where}" + (" — created for this project" if page.get("space_created") else ""),
            ),
            ("Assumed in the design (the review's remaining points)", points),
            (
                "Next, on the TDD page",
                [
                    f'"@{cmd.keyword} explain why this approach was selected"',
                    f'"@{cmd.keyword} why can\'t we use <alternative>?"',
                    f'"@{cmd.keyword} update the TDD" — apply what the discussion agreed',
                    f'"@{cmd.keyword} code" — implement it as a pull request',
                ],
            ),
        ),
        ran,
        review.to_dict(),
        {"tdd": page["url"]},
    )


async def _readiness(cmd: Command, ran: list[str]) -> AgentResult | None:
    """The review of the story — reused when it has not changed since."""
    if cmd.reusable_review:
        return AgentResult.from_dict(cmd.reusable_review)
    spec = cmd.review_spec
    if spec is None:
        return None
    payload = {
        "issue_key": cmd.issue_key,
        "comment_id": cmd.comment_id,
        "run_id": cmd.run_id,
        **({"previous_questions": cmd.previous_questions} if cmd.previous_questions else {}),
        **spec.forced_payload,
    }
    out, problem = await _call(
        cmd,
        spec.agent_name,
        payload,
        f"tdd-review-{cmd.run_id}",
        spec.timeout_minutes,
        spec.task_queue or "",
    )
    if out is None:
        logger.warning(f"run {cmd.run_id}: readiness review failed: {problem}")
        return None
    ran.append(spec.display_name or REVIEW)
    return AgentResult.from_dict(out)


async def _unchanged_tdd(cmd: Command, story: dict[str, Any]) -> CommandResult | None:
    """The story's TDD as it is, when the story has not changed since it was written.

    Asking twice wrote a second, different design (BTS-25 v1.0 -> v1.1,
    2026-10-23): the same story must give the same TDD. A new version comes from
    a changed story, or from the discussion on the page.
    """
    known = story.get("tdd") or {}
    if not (
        cmd.fingerprint and known.get("page_id") and known.get("fingerprint") == cmd.fingerprint
    ):
        return None
    page = await _tool(cmd, "read_tdd", str(known["page_id"]))
    if not page.get("ok"):
        return None  # the page is gone: write it again
    version = known.get("tdd_version") or known.get("version") or ""
    title = known.get("title") or "Technical Design Document"
    return CommandResult(
        reply(
            f"{title} — unchanged",
            (
                "What happened",
                f"{cmd.issue_key} has not changed since its TDD was written, so this is the "
                f"same design ({version}): [{title}]({known.get('url')}). "
                "Nothing was regenerated.",
            ),
            (
                "To change it",
                [
                    "Edit the story or answer its open questions here, then ask again; or",
                    f'on the TDD page: "@{cmd.keyword} why this approach?" or '
                    f'"@{cmd.keyword} why not <alternative>?" — the design is revised when '
                    "the alternative is better",
                ],
            ),
        ),
        links={"tdd": str(known.get("url") or "")},
    )


# --- explain why / why not ---------------------------------------------------------


async def design_question(cmd: Command) -> CommandResult:
    page = await _the_page(cmd)
    if not page.get("ok"):
        return CommandResult(
            reply("No TDD to discuss yet", ("What happened", page.get("error", "")))
        )
    question = strip_mention(cmd.text, cmd.keyword)
    await cmd.say(_working("The Planning agent is looking at the design and the code…", ""))
    out, problem = await _call(
        cmd,
        str(cmd.settings.get("planning_agent") or "planning_agent"),
        {
            "mode": "discuss",
            "question": question,
            "tdd_text": page["text"],
            "discussion": page.get("discussion") or [],
            "issue_key": cmd.issue_key,
            "run_id": cmd.run_id,
        },
        f"design-{cmd.run_id}",
        int(cmd.settings.get("planning_timeout_minutes") or 15),
        str(cmd.settings.get("planning_queue") or ""),
    )
    if out is None or out.get("status") != "success":
        return CommandResult(
            reply(
                "The question could not be answered",
                ("Problems", [problem or rules.planning_problem(out)]),
            )
        )
    sections: list[tuple[str, object]] = [("Answer", str(out.get("answer") or ""))]
    if not out.get("change_recommended"):
        sections.append(("The design", "Kept as it is: the current approach is the better one."))
        return CommandResult(reply(f"About the design — {question[:80]}", *sections), [PLANNING])

    # The architect agrees the alternative is better: the design is revised now
    # (2026-10-23), instead of waiting for a separate "update the TDD".
    change = str(out.get("proposed_change") or "").strip() or str(out.get("answer") or "")
    revised = await update_tdd(cmd, agreed=f"{question} — agreed change: {change}")
    url = revised.links.get("tdd")
    if url:
        sections.append(("The design was revised", f"{change} New version: {url}"))
        headline = f"Design revised — {question[:70]}"
    else:
        sections.append(
            (
                "Agreed change, not applied yet",
                [change, f'{_headline(revised)}. Reply "@{cmd.keyword} update the TDD" to retry.'],
            )
        )
        headline = f"About the design — {question[:70]}"
    return CommandResult(reply(headline, *sections), [PLANNING], links=revised.links)


# --- update the TDD -------------------------------------------------------------------


async def update_tdd(
    cmd: Command, reasons: list[str] | None = None, agreed: str = ""
) -> CommandResult:
    """The design revised: with the page's discussion, Coding's ``reasons``, or a
    change the architect ``agreed`` to in answer to a question."""
    page = await _the_page(cmd)
    if not page.get("ok"):
        return CommandResult(
            reply("No TDD to update yet", ("What happened", page.get("error", "")))
        )
    story = await _tool(cmd, "story_context", cmd.issue_key)
    if not story.get("ok"):
        return CommandResult(
            reply("Could not read the story", ("Problems", [story.get("error", "")]))
        )
    await cmd.say(_working("The Planning agent is revising the design…", ""))
    earlier = await _earlier_designs(cmd)
    document, problem = await _revise(cmd, story, page, reasons, earlier, agreed)
    if not document:
        return CommandResult(reply("The TDD was not updated", ("Problems", [problem])))
    published = await _publish(cmd, story, document, earlier)
    if not published.get("ok"):
        return CommandResult(
            reply(
                "The revised TDD could not be published", ("Problems", [published.get("error", "")])
            ),
            [PLANNING],
        )
    return CommandResult(
        reply(
            f"TDD updated — {published.get('tdd_version') or published.get('version')}",
            (
                "What happened",
                f"The Planning agent revised the design with the discussion on the page: "
                f"[{published['title']}]({published['url']}). "
                "The earlier version is in the page history.",
            ),
        ),
        [PLANNING],
        links={"tdd": published["url"]},
    )


# The whole current design goes to Planning when it is revised. It used to be
# cut at 20,000 characters: a 35,000-character TDD lost its end (the files,
# the tests, the risks) and the revision silently dropped an earlier feature's
# rule (BTS-8 v1.1 -> v1.2, 2026-10-15). Larger than any TDD written so far.
_CURRENT_DESIGN_CHARS = 120_000


async def _revise(
    cmd: Command,
    story: dict[str, Any],
    page: dict[str, Any],
    reasons: list[str] | None,
    earlier: list[dict[str, str]] | None = None,
    agreed: str = "",
) -> tuple[dict[str, Any] | None, str]:
    """A revised design: the story, the whole current design, and what to change."""
    base = await _tool(
        cmd, "planning_input", cmd.issue_key, list(story.get("subtask_keys") or []), [], None
    )
    asked = strip_mention(cmd.text, cmd.keyword)
    changes = [*(page.get("discussion") or [])]
    if reasons:
        changes.append("Coding could not implement the design: " + "; ".join(reasons))
    text = (
        str(base.get("issue_text") or "")
        + "\n\nThis is a REVISION of the technical design below, not a new one. Keep "
        "every requirement, decision, file change, test and item of existing behaviour "
        "to preserve that the discussion does not change; change only what it asks, "
        "and say what changed.\n\nThe current technical design:\n"
        + str(page.get("text") or "")[:_CURRENT_DESIGN_CHARS]
        + "\n\nThe discussion on the design page (oldest first):\n"
        + "\n".join(f"- {c}" for c in changes[-20:])
        + (f"\n\nAgreed now, apply it: {agreed}" if agreed else "")
        + (f"\n\nAsked now: {asked}" if asked and not reasons and not agreed else "")
        + _earlier_text(earlier or [])
    )
    return await _plan(cmd, text, attempt=2)


# --- code -------------------------------------------------------------------------------


async def code(cmd: Command) -> CommandResult:
    page = await _the_page(cmd)
    if not page.get("ok"):
        return CommandResult(
            reply("No TDD to implement yet", ("What happened", page.get("error", "")))
        )
    story = await _tool(cmd, "story_context", cmd.issue_key)
    if not story.get("ok"):
        return CommandResult(
            reply("Could not read the story", ("Problems", [story.get("error", "")]))
        )

    asked, confirmed = parse_code_command(cmd.text, cmd.keyword)
    defaults = await _tool(cmd, "branch_defaults", cmd.issue_key, str(story.get("summary") or ""))
    proposed = BranchPlan.from_dict((page.get("code_plan") or {}).get("proposed"))
    base = proposed if proposed.complete() else BranchPlan.from_dict(defaults)
    if confirmed:
        # "code confirm" — with any one repository's own branches named alongside.
        plan = asked.merged_over(base)
    elif asked.source or asked.branch or asked.target or asked.per_repo:
        plan = asked.merged_over(base)
    else:
        await _tool(
            cmd,
            "save_code_plan",
            page["page_id"],
            {"proposed": base.to_dict(), "asked_by": cmd.run_id},
        )
        return CommandResult(
            [
                (f"{AGENT_SIGNATURE} · Confirm the branches", ""),
                *branch_question(
                    base,
                    str(defaults.get("repo") or ""),
                    cmd.keyword,
                    dict(defaults.get("repo_names") or {}),
                )[1:],
                ("", AGENT_FOOTER),
            ]
        )
    if not plan.complete():
        return CommandResult(
            reply(
                "Branches missing",
                ("What happened", "Name the source, the new branch and the target."),
            )
        )
    await _tool(
        cmd,
        "save_code_plan",
        page["page_id"],
        {"proposed": plan.to_dict(), "confirmed": plan.to_dict(), "run_id": cmd.run_id},
    )

    requirements = await _tool(
        cmd, "planning_input", cmd.issue_key, list(story.get("subtask_keys") or []), [], None
    )
    ran: list[str] = []
    attempts = max(1, int(cmd.settings.get("coding_attempts") or 1))
    tdd_text = str(page["text"])
    reasons: list[str] = []
    superseded: list[str] = []
    for attempt in range(1, attempts + 1):
        await cmd.say(
            _working(
                f"Coding is implementing the TDD on {plan.branch} "
                f"(from {plan.source}, PR into {plan.target})"
                + (
                    f" — attempt {attempt} of {attempts}, with the revised design"
                    if attempt > 1
                    else ""
                )
                + "…",
                "",
            )
        )
        out, problem = await _call(
            cmd,
            str(cmd.settings.get("coding_agent") or "coding-agent"),
            {
                "mode": "delegated",
                "tdd_text": tdd_text,
                "requirements": str(requirements.get("issue_text") or ""),
                "branches": plan.to_dict(),
                "supersedes": superseded,
                # The only repositories Coding may write to (2026-10-14).
                "repos": str(cmd.settings.get("repos") or ""),
                "issue_key": cmd.issue_key,
                "run_id": cmd.run_id,
            },
            f"code-{cmd.run_id}-{attempt}",
            int(cmd.settings.get("coding_timeout_minutes") or 20),
            str(cmd.settings.get("coding_queue") or ""),
        )
        if out is None:
            return CommandResult(reply("Coding did not respond", ("Problems", [problem])), ran)
        if CODING not in ran:
            ran.append(CODING)
        prs = rules.pull_requests(out)
        if prs and not (rules.tests_failed(out) and attempt < attempts):
            return _pr_result(cmd, page, plan, out, prs, ran)
        if prs:
            # Raised, but its tests failed: back to Planning with the errors; the
            # retry's PR replaces this one (Coding closes it), so one PR stays open.
            superseded = [pr["pr_url"] for pr in prs]
            reasons = ["The pull request's tests failed: " + "; ".join(rules.test_failures(out))]
            revised = await update_tdd(cmd, reasons)
            if not revised.links.get("tdd"):
                return _pr_result(cmd, page, plan, out, prs, ran)
            if PLANNING not in ran:
                ran.append(PLANNING)
            page = await _the_page(cmd)
            tdd_text = str(page.get("text") or tdd_text)
            continue
        reasons = rules.coding_problems(out)
        if rules.is_access_problem(out):
            return CommandResult(
                reply(
                    "No pull request", ("What happened", rules.ACCESS_HINT), ("Problems", reasons)
                ),
                ran,
            )
        if not rules.is_design_problem(out):
            # Not the design's fault: a revision would only repeat the work.
            return CommandResult(
                reply(
                    "No pull request",
                    ("What happened", "Coding did not finish; the design was not changed."),
                    ("Problems", reasons),
                ),
                ran,
            )
        if attempt == attempts:
            break
        # Back to Planning: revise the page with why, then try again.
        revised = await update_tdd(cmd, reasons)
        if not revised.links.get("tdd"):
            break
        if PLANNING not in ran:
            ran.append(PLANNING)
        page = await _the_page(cmd)
        tdd_text = str(page.get("text") or tdd_text)
    return CommandResult(
        reply(
            f"No pull request after {attempts} attempt(s)",
            ("What happened", "Coding could not implement the design; the reasons are below."),
            ("Problems", reasons),
        ),
        ran,
    )


# --- shared steps ---------------------------------------------------------------------


def _pr_result(
    cmd: Command,
    page: dict[str, Any],
    plan: BranchPlan,
    out: dict[str, Any],
    prs: list[dict[str, Any]],
    ran: list[str],
) -> CommandResult:
    """The pull request, linked — with the test failures when they still fail."""
    failed = rules.tests_failed(out)
    lines = rules.pr_lines(out)
    what = (
        f"The Coding agent implemented [{page['title']}]({page['url']}) for {cmd.issue_key}. "
        + (
            "Its tests FAILED after every attempt — the PR is open for a person to fix or close; "
            "do not merge it as it is."
            if failed
            else "Review it in GitHub and merge it — nothing is merged automatically. The merge "
            "refreshes the Planning agent's code graph."
        )
    )
    sections: list[tuple[str, object]] = [
        ("What happened", what),
        ("Pull request" if len(lines) < 2 else "Pull requests", lines),
    ]
    order = rules.rollout_lines(out)
    if order:
        # Several services changed: merged out of order, a caller would reach a
        # provider that does not yet accept what it sends (2026-10-15).
        sections.append(("Merge and deploy in this order", order))
    sections += [
        (
            "Branches",
            [f"{pr.get('branch') or plan.branch} → {pr.get('base') or plan.target}" for pr in prs],
        ),
    ]
    if failed:
        sections.append(("Test failures", rules.test_failures(out)))
    if out.get("closed"):
        sections.append(
            ("Replaced", [f"{url} (closed — its tests failed)" for url in out["closed"]])
        )
    headline = "Pull request raised — tests FAILED" if failed else "Pull request raised"
    return CommandResult(
        reply(headline, *sections),
        ran,
        links={"pr": prs[0]["pr_url"], "page_id": str(page.get("page_id") or "")},
    )


async def _the_page(cmd: Command) -> dict[str, Any]:
    """The TDD page: the one asked on, or the story's."""
    page_id = cmd.page_id
    if not page_id:
        story = await _tool(cmd, "story_context", cmd.issue_key)
        page_id = str((story.get("tdd") or {}).get("page_id") or "")
        if not page_id:
            return {
                "ok": False,
                "error": f"{cmd.issue_key} has no TDD yet — comment "
                f'"@{cmd.keyword} create TDD" on the story first.',
            }
    page = await _tool(cmd, "read_tdd", page_id)
    if page.get("ok") and not cmd.issue_key:
        cmd.issue_key = str(page.get("issue_key") or "")
    if page.get("ok") and not cmd.issue_key:
        return {"ok": False, "error": "This page is not linked to a Jira story."}
    return page


async def _plan(cmd: Command, issue_text: str, attempt: int) -> tuple[dict[str, Any] | None, str]:
    """One Planning run that returns the document itself; (document, or None and why)."""
    out, problem = await _call(
        cmd,
        str(cmd.settings.get("planning_agent") or "planning_agent"),
        {
            "issue_text": issue_text,
            "repos": cmd.settings.get("repos") or "",
            "return_document": True,
            "issue_key": cmd.issue_key,
            "run_id": cmd.run_id,
        },
        f"tdd-plan-{cmd.run_id}-{attempt}",
        int(cmd.settings.get("planning_timeout_minutes") or 15),
        str(cmd.settings.get("planning_queue") or ""),
    )
    document = (out or {}).get("document") or {}
    if out is None or out.get("status") != "success" or not document.get("confluence_html"):
        return None, problem or rules.planning_problem(out)
    return document, ""


async def _publish(
    cmd: Command,
    story: dict[str, Any],
    document: dict[str, Any],
    earlier: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return await _tool(
        cmd,
        "publish_tdd",
        cmd.issue_key,
        str(story.get("project_key") or ""),
        str(story.get("project_name") or ""),
        str(story.get("summary") or ""),
        str(document.get("title") or ""),
        str(document.get("confluence_html") or ""),
        cmd.run_id,
        [{"title": d["title"], "url": d["url"]} for d in earlier or []],
        cmd.fingerprint,
    )


async def _earlier_designs(cmd: Command) -> list[dict[str, str]]:
    """The project's other TDDs (2026-10-15); none when they cannot be read."""
    found = await _tool(cmd, "earlier_designs", cmd.issue_key)
    return list(found.get("designs") or [])


def _earlier_text(designs: list[dict[str, str]]) -> str:
    """The earlier designs as Planning reads them: decisions already made."""
    if not designs:
        return ""
    return (
        "\n\nEarlier designs in this project — decisions already made. Keep them: this "
        "change extends the project and must not break what they deliver.\n"
        + "\n".join(f"- {d['title']} ({d['url']}): {d.get('excerpt', '')}" for d in designs)
    )


async def _call(
    cmd: Command, agent: str, payload: dict[str, Any], workflow_id: str, minutes: int, queue: str
) -> tuple[dict[str, Any] | None, str]:
    options: dict[str, Any] = {
        "workflow_id": workflow_id,
        "execution_timeout": timedelta(minutes=minutes),
        "retry_policy": RetryPolicy(maximum_attempts=1),
    }
    if queue:
        options["task_queue"] = queue
    try:
        out = await cmd.dispatch(agent, payload, **options)
    except Exception as exc:  # noqa: BLE001 — reported in the reply
        return None, f"{type(exc).__name__}: {str(exc)[:300]}"
    if isinstance(out, list):
        out = out[0] if out else {}
    return (out if isinstance(out, dict) else {}), ""


async def _tool(cmd: Command, name: str, *args: Any) -> dict[str, Any]:
    try:
        out = await cmd.execute(name, *args, start_to_close_timeout=_TOOL)
    except Exception as exc:  # noqa: BLE001 — the caller says what failed
        logger.error(f"run {cmd.run_id}: {name} raised: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out if isinstance(out, dict) else {}


def _working(what: str, step: str) -> Blocks:
    return reply("⏳ Working on it" + (f" ({step})" if step else ""), ("What is happening", what))
