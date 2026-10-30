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

from agent import handoff_flow
from routing import handoff, memory
from routing import pipeline as rules
from routing.catalog import AgentSpec
from routing.classify import strip_mention
from routing.commands import (
    BranchPlan,
    branch_question,
    design_kind,
    parse_code_command,
    parse_update,
)
from shared.contract import AgentResult, Outcome
from shared.keywords import AGENT_FOOTER, AGENT_SIGNATURE

logger = logging.getLogger(__name__)

Execute = Callable[..., Awaitable[dict]]
Dispatch = Callable[..., Awaitable[Any]]
Blocks = list[tuple[str, object]]
# say(blocks): show where the command is — the "working on it" reply, edited.
Say = Callable[[Blocks], Awaitable[None]]

_TOOL = timedelta(minutes=2)
# Steps that read the story's files (2026-10-27): videos are watched by a model.
_FILES_TOOL = timedelta(minutes=10)
# A memory tool: a few Memsy calls, one model call, a snapshot save that waits.
_MEMORY_TOOL = timedelta(minutes=4)
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
    # The story read once for the whole command (2026-10-29): the memory's
    # check, Planning's text and the agreed input all come from this one read.
    bundle = await _bundle(cmd, story)
    # Did the requirement change, or only its words? (Planning's memory, 2026-10-26)
    facts = await _story_facts(cmd, story, bundle)
    if facts is not None and facts.get("verdict") in memory.KEEP_TDD:
        kept = await _kept_tdd(cmd, story, facts)
        if kept is not None:
            return kept
    changes = memory.change_lines((facts or {}).get("changes") or [])

    ran: list[str] = []
    await cmd.say(_working("Checking the story is ready (Requirement Review)…", "1 of 3"))
    # What Orchestration remembers about the card, for the review (2026-10-30).
    recalled = (
        await handoff_flow.recall(
            _run(cmd), cmd.issue_key, bundle, str(cmd.settings.get("repos") or "")
        )
        if bundle is not None and not cmd.reusable_review
        else None
    )
    review = await _readiness(cmd, ran, recalled)
    kept = None
    if recalled and review is not None and review.outcome is Outcome.REVIEWED:
        # Its questions are remembered even when the story is not ready yet, so
        # the answers people give next are linked to them.
        kept = await handoff_flow.keep(
            _run(cmd),
            cmd.issue_key,
            recalled,
            list(review.questions),
            handoff.card(bundle)["title"],
            review.summary,
        )
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
    if bundle is not None:
        text, bundle = await _bundle_input(cmd, bundle, review.to_dict())
        given = await _handoff(cmd, bundle, kept)
    else:
        text = await _tool(
            cmd,
            "planning_input",
            cmd.issue_key,
            list(story.get("subtask_keys") or []),
            [],
            review.to_dict(),
            timeout=_FILES_TOOL,
        )
        given = None
    if not text.get("issue_text"):
        return CommandResult(
            reply("Could not read the story for Planning", ("Problems", [text.get("error", "")])),
            ran,
        )
    earlier = await _earlier_designs(cmd)
    current = await _current_design(cmd, story)
    if current is not None:
        # The story already has a TDD: the new version starts from it, so what
        # was decided there (BTS-25 v1.1's integer paise) is not lost to a
        # fresh run (v1.2 went back to double, 2026-10-23).
        agreed = _STORY_CHANGED + (
            "\n\nWhat changed in the requirement:\n" + "\n".join(f"- {c}" for c in changes)
            if changes
            else ""
        )
        document, problem = await _revise(
            cmd,
            story,
            current,
            None,
            earlier,
            agreed=agreed,
            review=review.to_dict(),
            base=text,
            given=given,
        )
    elif given is not None:
        document, problem = await _plan(cmd, _earlier_text(earlier).strip(), 1, given)
    else:
        document, problem = await _plan(
            cmd, str(text["issue_text"]) + _earlier_text(earlier), attempt=1
        )
    if not document:
        return CommandResult(
            reply("Planning wrote no design", ("Problems", [problem])), ran, review.to_dict()
        )
    if PLANNING not in ran:
        ran.append(PLANNING)

    await cmd.say(_working("Publishing the TDD in Confluence…", "3 of 3"))
    page = await _publish(cmd, story, document, earlier)
    if not page.get("ok"):
        return CommandResult(
            reply("The TDD could not be published", ("Problems", [page.get("error", "")])), ran
        )
    await _remember(cmd, story, page, document, facts)
    if given is not None:
        version, data = handoff.design_event(given["card"], given["services"], page)
        await handoff_flow.remember(_run(cmd), cmd.issue_key, "design", data, version)
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
            ("Files read from the story", rules.files_summary(list(text.get("files") or []))),
            (
                "Services designed, by rank",
                handoff.service_lines(given["services"]) if given else [],
            ),
            (
                "Answers the design follows",
                handoff.answer_lines(given["questions"]) if given else [],
            ),
            ("What changed in the requirement", changes),
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
                    f'"@{cmd.keyword} update the TDD: <change>" — checked first, applied when '
                    "valid",
                    f'"@{cmd.keyword} code" — implement it as a pull request',
                ],
            ),
        ),
        ran,
        review.to_dict(),
        {"tdd": page["url"]},
    )


async def _readiness(
    cmd: Command, ran: list[str], recalled: dict[str, Any] | None = None
) -> AgentResult | None:
    """The review of the story — reused when it has not changed since. With
    ``recalled``, the card's memory is one more source it reads (2026-10-30)."""
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
        **(
            {"memory_document": recalled["document"]}
            if recalled and recalled.get("document")
            else {}
        ),
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


_STORY_CHANGED = (
    "The TDD was asked for again because the story changed (or this design was written "
    "before changes were tracked). Bring the design up to date with the story as it is "
    "now. Keep every decision already in the current design and its discussion — for "
    "example how money is calculated, the validation order, the tests — unless the "
    "story now contradicts it; say what changed and why."
)


async def _current_design(cmd: Command, story: dict[str, Any]) -> dict[str, Any] | None:
    """The story's TDD page as it stands, or None when it has none (or it is gone)."""
    page_id = str((story.get("tdd") or {}).get("page_id") or "")
    if not page_id:
        return None
    page = await _tool(cmd, "read_tdd", page_id)
    return page if page.get("ok") else None


# --- Planning's memory (2026-10-26) -----------------------------------------------------


def _memory(cmd: Command) -> bool:
    """ORCH_MEMORY: ask Planning before rewriting or changing a TDD."""
    return bool(cmd.settings.get("memory"))


async def _planning(
    cmd: Command, payload: dict[str, Any], name: str
) -> tuple[dict[str, Any] | None, str]:
    return await _call(
        cmd,
        str(cmd.settings.get("planning_agent") or "planning_agent"),
        payload,
        f"{name}-{cmd.run_id}",
        int(cmd.settings.get("planning_timeout_minutes") or 15),
        str(cmd.settings.get("planning_queue") or ""),
    )


def _handoff_on(cmd: Command) -> bool:
    """ORCH_HANDOFF (2026-10-29): the story read once, the review's questions
    with their answers, the ranked services and the history, for Planning."""
    return _memory(cmd) and bool(cmd.settings.get("handoff"))


def _run(cmd: Command) -> handoff_flow.Run:
    """One memory tool for the handoff steps; a failure is a result, not a crash."""

    async def run(name: str, *args: Any) -> dict[str, Any]:
        out = await _tool(cmd, name, *args, timeout=_MEMORY_TOOL)
        return out if "ok" in out else {"ok": False, "error": f"{name} returned nothing"}

    return run


async def _bundle(cmd: Command, story: dict[str, Any]) -> dict[str, Any] | None:
    """The story read once (ORCH_HANDOFF); None when off or unreadable — the
    command then reads it as before."""
    if not _handoff_on(cmd):
        return None
    bundle = await _tool(
        cmd,
        "story_bundle",
        cmd.issue_key,
        list(story.get("subtask_keys") or []),
        timeout=_FILES_TOOL,
    )
    if not bundle.get("ok"):
        logger.warning(f"run {cmd.run_id}: story read once failed: {bundle.get('error')}")
        return None
    return bundle


async def _bundle_input(
    cmd: Command, bundle: dict[str, Any], review: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(Planning's text, the bundle with its files read) from the one read; its
    pictures and videos are looked at now, only because a design is really
    written."""
    root = bundle.get("root") or {}
    seen = await _tool(
        cmd,
        "read_media",
        cmd.issue_key,
        str(root.get("summary") or ""),
        list(bundle.get("files") or []),
        timeout=_FILES_TOOL,
    )
    files = (
        list(seen.get("files") or [])
        if seen.get("ok")
        else [
            {k: v for k, v in f.items() if k not in ("url", "mime")}
            for f in bundle.get("files") or []
        ]
    )
    return handoff.planning_input(bundle, files, review), {**bundle, "files": files}


async def _handoff(
    cmd: Command, bundle: dict[str, Any], kept: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The agreed input for Planning: the card, its ranked services and the
    review's questions with every answer in the comments so far. ``kept`` is the
    memory as the readiness review just left it; without it the memory is
    brought up to date now. None when the memory cannot give it — the TDD is
    then written from the old input."""
    card = handoff.card(bundle, list(bundle.get("files") or []))
    kept = kept or await handoff_flow.refresh(
        _run(cmd), cmd.issue_key, bundle, str(cmd.settings.get("repos") or "")
    )
    if not kept:
        return None
    return handoff.payload(card, kept["services"], kept["questions"])


async def _story_facts(
    cmd: Command, story: dict[str, Any], bundle: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Planning's verdict on the story against its current TDD: same, reworded,
    changed, unsure or new. None when memory is off or Planning cannot say — the
    TDD is then written as before, never skipped."""
    if not _memory(cmd):
        return None
    if bundle is not None:
        story_text = handoff.memory_text(bundle)
        text = {"ok": True, "text": story_text, "hash": memory.normalised_hash(story_text)}
    else:
        text = await _tool(
            cmd,
            "story_text",
            cmd.issue_key,
            list(story.get("subtask_keys") or []),
            False,
            timeout=_FILES_TOOL,
        )
    if not text.get("ok") or not text.get("text"):
        logger.warning(f"run {cmd.run_id}: no story text for the facts check: {text.get('error')}")
        return None
    stored = await _tool(cmd, "tdd_memory", cmd.issue_key, timeout=_MEMORY_TOOL)
    if not stored.get("ok"):
        logger.warning(f"run {cmd.run_id}: no TDD memory for the facts check: {stored}")
        return None
    out, problem = await _planning(
        cmd,
        {
            "mode": "facts",
            "issue_key": cmd.issue_key,
            "story_text": text["text"],
            # The story's TDD memory, kept by Orchestration (2026-10-30).
            "memory": {"snapshot": stored["snapshot"]},
            "run_id": cmd.run_id,
        },
        "tdd-facts",
    )
    if out is None or out.get("status") != "success" or not out.get("verdict"):
        logger.warning(f"run {cmd.run_id}: the facts check failed: {problem or out}")
        return None
    return {**out, "story_text": text["text"], "story_text_hash": text.get("hash", "")}


_KEPT_BECAUSE = {
    memory.SAME: "has not changed since its TDD was written",
    memory.REWORDED: (
        "was reworded, but its requirement is the same: Planning compared its facts, and "
        "every rule, limit and acceptance criterion still means the same"
    ),
    memory.UNSURE: (
        "changed in wording, and Planning could not be sure that the meaning changed, "
        "so the TDD was kept"
    ),
}


async def _kept_tdd(
    cmd: Command, story: dict[str, Any], facts: dict[str, Any]
) -> CommandResult | None:
    """The current TDD, when Planning found the requirement unchanged. None when
    the story has no readable TDD (it is written then)."""
    known = story.get("tdd") or {}
    if not known.get("page_id"):
        return None
    page = await _tool(cmd, "read_tdd", str(known["page_id"]))
    if not page.get("ok"):
        return None
    # The next identical request is answered at once, without asking Planning.
    await _tool(cmd, "remember_fingerprint", cmd.issue_key, cmd.fingerprint)
    verdict = str(facts.get("verdict"))
    version = known.get("tdd_version") or known.get("version") or ""
    title = known.get("title") or "Technical Design Document"
    sections: list[tuple[str, object]] = [
        (
            "What happened",
            f"{cmd.issue_key} {_KEPT_BECAUSE.get(verdict, _KEPT_BECAUSE[memory.SAME])}. "
            f"This is the same design ({version}): [{title}]({known.get('url')}). "
            "Nothing was regenerated.",
        ),
        ("Why", str(facts.get("reason") or "")),
    ]
    if verdict == memory.UNSURE:
        sections.append(
            (
                "If the new wording was meant to change the requirement",
                f'Comment "@{cmd.keyword} update the TDD: <what changed>" — the change is '
                "checked and applied when it is valid.",
            )
        )
    sections.append(
        (
            "To change it",
            [
                "Change a rule, limit or acceptance criterion in the story, then ask again; or",
                f'on the TDD page: "@{cmd.keyword} why not <alternative>?" — the design is '
                "revised when the alternative is better",
            ],
        )
    )
    return CommandResult(
        reply(f"{title} — unchanged", *sections), [PLANNING], links={"tdd": str(known.get("url"))}
    )


async def _remember(
    cmd: Command,
    story: dict[str, Any],
    page: dict[str, Any],
    document: dict[str, Any],
    facts: dict[str, Any] | None = None,
    new_decisions: list[dict[str, Any]] | None = None,
    supersedes: list[str] | None = None,
    source: str = "",
) -> None:
    """A TDD version was published: Planning makes the story's new snapshot
    (its facts and the decisions in it) from the stored one, and Orchestration
    keeps it (2026-10-30). Best effort — the TDD is published either way; a miss
    is logged."""
    if not _memory(cmd):
        return
    stored = await _tool(cmd, "tdd_memory", cmd.issue_key, timeout=_MEMORY_TOOL)
    if not stored.get("ok"):
        logger.warning(f"run {cmd.run_id}: the TDD memory could not be read: {stored}")
        return
    payload: dict[str, Any] = {
        "mode": "record",
        "issue_key": cmd.issue_key,
        "project_key": str(story.get("project_key") or cmd.issue_key.split("-")[0]),
        "tdd_version": str(page.get("tdd_version") or page.get("version") or ""),
        "page_id": str(page.get("page_id") or ""),
        "decisions": [*(document.get("decisions") or []), *(new_decisions or [])],
        "superseded": list(supersedes or []),
        "source": source or str(page.get("url") or ""),
        "memory": {"snapshot": stored["snapshot"]},
        "run_id": cmd.run_id,
    }
    if facts:
        payload |= {
            "facts": facts.get("facts") or [],
            "facts_hash": str(facts.get("facts_hash") or ""),
            "story_text_hash": str(facts.get("story_text_hash") or ""),
            "story_text": str(facts.get("story_text") or ""),
        }
    out, problem = await _planning(cmd, payload, "tdd-record")
    snapshot = ((out or {}).get("stored") or {}).get("snapshot")
    if out is None or out.get("status") != "success" or not isinstance(snapshot, dict):
        logger.warning(f"run {cmd.run_id}: Planning did not record the TDD: {problem or out}")
        return
    kept = await _tool(
        cmd,
        "keep_tdd_memory",
        cmd.issue_key,
        int(stored.get("seq") or 0) + 1,
        snapshot,
        payload["tdd_version"],
        payload["page_id"],
        payload["decisions"],
        payload["superseded"],
        timeout=_MEMORY_TOOL,
    )
    if not kept.get("ok"):
        logger.warning(f"run {cmd.run_id}: the TDD memory was not kept: {kept.get('error')}")


# --- explain why / why not / update the TDD ------------------------------------------


async def design_question(cmd: Command) -> CommandResult:
    page = await _the_page(cmd)
    if not page.get("ok"):
        return CommandResult(
            reply("No TDD to discuss yet", ("What happened", page.get("error", "")))
        )
    question = strip_mention(cmd.text, cmd.keyword)
    kind = design_kind(cmd.text, cmd.keyword) if _memory(cmd) else ""
    return await _discuss(cmd, page, question, kind)


async def _discuss(cmd: Command, page: dict[str, Any], request: str, kind: str) -> CommandResult:
    """Planning answers, and checks a proposed change before it is applied.

    question → an answer only; proposal / update → applied only when Planning
    finds it valid (the story's facts, the code, the decisions in force, not
    already in the TDD); update_override → a person insists, applied as asked.
    """
    await cmd.say(_working("The Planning agent is looking at the design and the code…", ""))
    payload: dict[str, Any] = {
        "mode": "discuss",
        "question": request,
        "tdd_text": page["text"],
        "discussion": page.get("discussion") or [],
        "issue_key": cmd.issue_key,
        "run_id": cmd.run_id,
    }
    page_id = str(page.get("page_id") or cmd.page_id or "")
    if kind:
        payload["kind"] = kind
        # The story's facts, decisions in force and earlier conversation about
        # this TDD, kept by Orchestration (2026-10-30).
        known = await _tool(cmd, "design_context", cmd.issue_key, request, timeout=_MEMORY_TOOL)
        if known.get("ok"):
            payload["memory"] = {k: known[k] for k in ("facts", "decisions", "earlier")}
    out, problem = await _planning(cmd, payload, "design")
    if out is None or out.get("status") != "success":
        return CommandResult(
            reply(
                "The question could not be answered",
                ("Problems", [problem or rules.planning_problem(out)]),
            )
        )
    verdict = memory.verdict_of(out)
    answer = str(out.get("answer") or "")
    if kind:
        # The question and the answer, one turn of the TDD's conversation.
        await _tool(
            cmd,
            "remember_exchange",
            cmd.issue_key,
            page_id,
            request,
            answer,
            kind,
            verdict,
            timeout=_MEMORY_TOOL,
        )
    refused = memory.refused_lines(out)
    sections: list[tuple[str, object]] = [("Answer", answer)]
    topic = request[:70]

    if verdict == memory.ANSWER_ONLY:
        if kind != "question":
            sections.append(
                ("The design", "Kept as it is: the current approach is the better one.")
            )
        return CommandResult(reply(f"About the design — {topic}", *sections), [PLANNING])
    if verdict == memory.ALREADY_PRESENT:
        sections.append(("The design", "Already in the TDD, so no new version was written."))
        return CommandResult(reply(f"Already in the design — {topic}", *sections), [PLANNING])
    if verdict == memory.REFUSE:
        sections.append(("Not applied", refused or ["See the answer for why."]))
        sections.append(
            (
                "If you still want it",
                f'Comment "@{cmd.keyword} update the TDD anyway: <the change>" — it is applied '
                "as asked and recorded as your decision.",
            )
        )
        return CommandResult(reply(f"Not changed — {topic}", *sections), [PLANNING])

    # apply / partial: the agreed part is applied now.
    changes = memory.to_apply(out) or [answer]
    agreed = f"{request} — agreed change: " + "; ".join(changes)
    revised = await _revise_and_publish(
        cmd,
        agreed=agreed,
        new_decisions=list(out.get("new_decisions") or []),
        supersedes=[str(s) for s in out.get("supersedes") or []],
    )
    url = revised.links.get("tdd")
    if url:
        sections.append(("The design was revised", [*changes, f"New version: {url}"]))
        headline = f"Design revised — {topic}"
    else:
        sections.append(
            (
                "Agreed change, not applied yet",
                [
                    *changes,
                    f'{_headline(revised)}. Reply "@{cmd.keyword} update the TDD" to retry.',
                ],
            )
        )
        headline = f"About the design — {topic}"
    if refused:
        sections.append(("Not applied", refused))
    return CommandResult(reply(headline, *sections), [PLANNING], links=revised.links)


async def update_tdd(
    cmd: Command, reasons: list[str] | None = None, agreed: str = ""
) -> CommandResult:
    """The design revised: with Coding's ``reasons`` or an ``agreed`` change as
    before; a person's "update the TDD: X" is checked first (memory on)."""
    if reasons or agreed or not _memory(cmd):
        return await _revise_and_publish(cmd, reasons, agreed)
    page = await _the_page(cmd)
    if not page.get("ok"):
        return CommandResult(
            reply("No TDD to update yet", ("What happened", page.get("error", "")))
        )
    kind, change = parse_update(cmd.text, cmd.keyword)
    return await _discuss(cmd, page, change, kind)


async def _revise_and_publish(
    cmd: Command,
    reasons: list[str] | None = None,
    agreed: str = "",
    new_decisions: list[dict[str, Any]] | None = None,
    supersedes: list[str] | None = None,
) -> CommandResult:
    """Revise the TDD (the discussion, Coding's ``reasons`` or an ``agreed``
    change), publish it as a new version, and tell Planning's memory."""
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
    await _remember(
        cmd, story, published, document, None, new_decisions, supersedes, published.get("url", "")
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
    review: dict[str, Any] | None = None,
    base: dict[str, Any] | None = None,
    given: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """A revised design: the story, the whole current design, and what to change.
    ``base`` is the story as Planning's input when the caller has read it already
    (its files are read once, 2026-10-27); ``given`` the agreed input
    (2026-10-29), which then stands for the story."""
    if given is None:
        base = base or await _tool(
            cmd,
            "planning_input",
            cmd.issue_key,
            list(story.get("subtask_keys") or []),
            [],
            review,
            timeout=_FILES_TOOL,
        )
    asked = strip_mention(cmd.text, cmd.keyword)
    changes = [*(page.get("discussion") or [])]
    if reasons:
        changes.append("Coding could not implement the design: " + "; ".join(reasons))
    text = (
        ("" if given is not None else str((base or {}).get("issue_text") or ""))
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
    if given is not None:
        return await _plan(cmd, text.strip(), 2, given)
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
        cmd,
        "planning_input",
        cmd.issue_key,
        list(story.get("subtask_keys") or []),
        [],
        None,
        timeout=_FILES_TOOL,
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
            return await _pr_done(cmd, page, plan, out, prs, ran)
        if prs:
            # Raised, but its tests failed: back to Planning with the errors; the
            # retry's PR replaces this one (Coding closes it), so one PR stays open.
            superseded = [pr["pr_url"] for pr in prs]
            reasons = ["The pull request's tests failed: " + "; ".join(rules.test_failures(out))]
            revised = await update_tdd(cmd, reasons)
            if not revised.links.get("tdd"):
                return await _pr_done(cmd, page, plan, out, prs, ran)
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


async def _pr_done(
    cmd: Command,
    page: dict[str, Any],
    plan: BranchPlan,
    out: dict[str, Any],
    prs: list[dict[str, Any]],
    ran: list[str],
) -> CommandResult:
    """The pull request's reply — and, with ORCH_HANDOFF, each PR in the card's
    history (branch, service, link), so a later card about the same thing finds it."""
    if _handoff_on(cmd):
        title = str(page.get("title") or cmd.issue_key)
        for event_id, data in handoff.pr_events(out, title):
            await handoff_flow.remember(_run(cmd), cmd.issue_key, "pr_opened", data, event_id)
    return _pr_result(cmd, page, plan, out, prs, ran)


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


async def _plan(
    cmd: Command, issue_text: str, attempt: int, given: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, str]:
    """One Planning run that returns the document itself; (document, or None and why).

    With ``given`` (the agreed input, 2026-10-29) Planning builds its text from
    the card, the ranked services and the questions with their answers, and
    ``issue_text`` is what follows them (a revision's instructions, the earlier
    designs). Every repository is synced: the ranked services are named first,
    so they are designed, and Planning's own code search adds any service the
    ranking missed. The decisions in force come from Orchestration's memory."""
    payload: dict[str, Any] = {
        "issue_text": issue_text,
        "repos": cmd.settings.get("repos") or "",
        "return_document": True,
        "issue_key": cmd.issue_key,
        "project_key": cmd.issue_key.split("-")[0],
        "run_id": cmd.run_id,
    }
    if given is not None:
        payload |= {**given, "issue_text": "", "notes": issue_text}
    if _memory(cmd):
        # The story's decisions in force, and its project's on the ranked
        # services (all of the project's when none is ranked).
        services = [s["service"] for s in (given or {}).get("services") or []]
        found = await _tool(
            cmd, "decisions_in_force", cmd.issue_key, services, timeout=_MEMORY_TOOL
        )
        payload["decisions"] = list(found.get("decisions") or [])
    out, problem = await _call(
        cmd,
        str(cmd.settings.get("planning_agent") or "planning_agent"),
        payload,
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


async def _tool(cmd: Command, name: str, *args: Any, timeout: timedelta = _TOOL) -> dict[str, Any]:
    try:
        out = await cmd.execute(name, *args, start_to_close_timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — the caller says what failed
        logger.error(f"run {cmd.run_id}: {name} raised: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out if isinstance(out, dict) else {}


def _working(what: str, step: str) -> Blocks:
    return reply("⏳ Working on it" + (f" ({step})" if step else ""), ("What is happening", what))
