"""What Planning is given for a TDD, and what each card leaves in its history
(2026-10-29).

The story is read once per command (``story_bundle``). From that one read come
the memory's story text, Planning's text, and the agreed input:

    {"card": {"key", "type", "title", "description", "comments", "files": [{"name", "content"}]},
     "services": [{"service", "repo", "branch", "rank", "reason", "prs": [...]}],
     "questions": [{"question", "answers": ["..."]}]}

The review's questions and their answers, the ranked services, and the
history are kept in Memsy by the Requirement Review agent (2026-10-30); here
is only how they are put together and shown. Pure — no network, no
environment, no clock.
"""

from __future__ import annotations

import re
from typing import Any

from routing import memory, pipeline
from shared.keywords import AGENT_FOOTER

MAX_SERVICE_LINES = 10
MAX_ANSWER_LINES = 8
_PR_URL = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")


def _subtask_lines(tickets: list[dict[str, Any]]) -> list[str]:
    lines = []
    for t in tickets:
        if not t.get("key"):
            continue
        lines.append(f"Sub-task {t['key']}: {t.get('summary') or ''}".rstrip(": "))
        if t.get("description"):
            lines.append(str(t["description"]).strip()[: pipeline.MAX_DESCRIPTION])
    return lines


def card(bundle: dict[str, Any], files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The card as the agreed input gives it: its sub-tasks are part of its
    description; its comments are people's (requests reduced to what they add)."""
    root = bundle.get("root") or {}
    description = str(root.get("description") or "").strip()
    subtasks = _subtask_lines(list(bundle.get("tickets") or []))
    if subtasks:
        description = "\n\n".join(filter(None, [description, "\n".join(subtasks)]))
    return {
        "key": str(root.get("key") or ""),
        "type": str(root.get("type") or ""),
        "title": str(root.get("summary") or ""),
        "description": description[: pipeline.MAX_STORY_DESCRIPTION * 2],
        "comments": [str(c) for c in bundle.get("comments") or [] if str(c).strip()],
        "files": [
            {"name": str(f.get("filename") or "file"), "content": str(f.get("text") or "")}
            for f in (files if files is not None else bundle.get("files") or [])
        ],
    }


def card_text(card: dict[str, Any]) -> str:
    """The card as one text, for ranking its services."""
    lines = [f"{card.get('key', '')}: {card.get('title', '')}", card.get("description") or ""]
    lines += [f"- {c}" for c in card.get("comments") or []]
    for f in card.get("files") or []:
        lines.append(f"=== {f['name']} ===")
        if f.get("content"):
            lines.append(str(f["content"])[: memory.MAX_FILE_CHARS])
    return "\n".join(line for line in lines if line).strip()


def memory_text(bundle: dict[str, Any]) -> str:
    """The story for Planning's same-meaning check: documents by their text,
    images and videos by their file (a model reads them a little differently
    each time)."""
    files = [
        f if f.get("kind") == "document" else {**f, "text": ""} for f in bundle.get("files") or []
    ]
    return memory.story_text(
        bundle.get("root") or {},
        list(bundle.get("tickets") or []),
        list(bundle.get("comments") or []),
        files,
    )


def planning_input(
    bundle: dict[str, Any], files: list[dict[str, Any]], review: dict[str, Any] | None
) -> dict[str, Any]:
    """What the ``planning_input`` tool returns, from the one read."""
    return {
        "issue_text": pipeline.planning_text(
            bundle.get("root") or {},
            list(bundle.get("tickets") or []),
            [],
            review,
            list(bundle.get("discussion") or []),
            files,
        ),
        "tickets": len(bundle.get("tickets") or []),
        "unread": list(bundle.get("unread") or []),
        "files": pipeline.files_read(files),
    }


def questions_for_planning(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The agreed input's questions: the question and its answers."""
    return [
        {"question": str(q.get("question") or ""), "answers": list(q.get("answers") or [])}
        for q in questions
        if str(q.get("question") or "").strip()
    ]


def payload(
    card: dict[str, Any], services: list[dict[str, Any]], questions: list[dict[str, Any]]
) -> dict[str, Any]:
    return {"card": card, "services": services, "questions": questions_for_planning(questions)}


# --- what the reply shows ---------------------------------------------------------------


def service_lines(services: list[dict[str, Any]]) -> list[str]:
    """One line per service, rank 1 (the main one) first:
    "1. imps-service: spec.pdf names POST /imps/transfer (earlier: BTS-20 PR #41, merged)"."""
    lines = []
    for s in services[:MAX_SERVICE_LINES]:
        line = f"{s.get('rank')}. {s.get('service')}"
        if s.get("reason"):
            line += f": {s['reason']}"
        earlier = [
            f"{p.get('card')} PR #{p.get('pr')}" + (f", {p['state']}" if p.get("state") else "")
            for p in s.get("prs") or []
        ]
        if earlier:
            line += f" (earlier: {'; '.join(earlier)})"
        lines.append(line)
    return lines


def _numbered(q: dict[str, Any]) -> str:
    return f"{q['id']} {q.get('question')}" if q.get("id") else str(q.get("question"))


def answer_lines(questions: list[dict[str, Any]]) -> list[str]:
    """The questions answered so far, each with its answer."""
    lines = []
    for q in questions:
        answers = [str(a).strip() for a in q.get("answers") or [] if str(a).strip()]
        if answers:
            lines.append(f"{_numbered(q)} → {' / '.join(answers)}"[:400])
    return lines[:MAX_ANSWER_LINES]


def open_lines(questions: list[dict[str, Any]]) -> list[str]:
    """The questions not answered yet, numbered so a person can answer "Q3: …"."""
    return [_numbered(q)[:300] for q in questions if not q.get("answers")][:MAX_ANSWER_LINES]


def memory_sections(kept: dict[str, Any] | None) -> list[tuple[str, object]]:
    """What a review's reply shows of the card's memory."""
    if not kept:
        return []
    questions = list(kept.get("questions") or [])
    return [
        (
            "Services this card touches (1 = the main one)",
            service_lines(list(kept.get("services") or [])),
        ),
        ("Answered so far", answer_lines(questions)),
        ('Still open — answer in a comment starting "Q<number>:"', open_lines(questions)),
    ]


def with_sections(
    blocks: list[tuple[str, object]], sections: list[tuple[str, object]]
) -> list[tuple[str, object]]:
    """``blocks`` with ``sections`` added before the footer; an empty section is left out."""
    added = [s for s in sections if s[1]]
    if not added:
        return blocks
    footer = next(
        (i for i in range(len(blocks) - 1, -1, -1) if blocks[i][1] == AGENT_FOOTER), len(blocks)
    )
    return [*blocks[:footer], *added, *blocks[footer:]]


# --- the history each card leaves ---------------------------------------------------------


def design_event(
    card: dict[str, Any], services: list[dict[str, Any]], page: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """A published TDD in the history: its version and services. Its decisions
    live in the TDD memory only, one place (2026-10-30)."""
    version = str(page.get("tdd_version") or page.get("version") or "")
    return version, {
        "title": card.get("title") or "",
        "version": version,
        "page": str(page.get("url") or ""),
        "services": [s["service"] for s in services if s.get("service")],
    }


def pr_events(out: dict[str, Any], title: str) -> list[tuple[str, dict[str, Any]]]:
    """Each pull request Coding opened, as a history event: (event id, data)."""
    events = []
    for pr in pipeline.pull_requests(out):
        found = _PR_URL.search(str(pr.get("pr_url") or ""))
        if not found:
            continue
        repo, number = found.group(1), int(found.group(2))
        events.append(
            (
                str(number),
                {
                    "title": title,
                    "repo": repo,
                    "pr": number,
                    "url": str(pr["pr_url"]),
                    "branch": str(pr.get("branch") or ""),
                    "base": str(pr.get("base") or ""),
                    "service": str(pr.get("service") or ""),
                    "tests_passed": bool(pr.get("tests_passed")),
                },
            )
        )
    return events


def merge_event(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A merged pull request (GitHub's webhook) as a history event."""
    number = int(event.get("pr_number") or 0)
    return f"{number}-merged", {
        "repo": str(event.get("repo") or ""),
        "pr": number,
        "url": str(event.get("pr_url") or ""),
        "state": "merged",
        "branch": str(event.get("branch") or ""),
        "files": [str(f) for f in event.get("files") or []],
    }
