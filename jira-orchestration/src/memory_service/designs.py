"""Each story's TDD memory and its conversation, kept by Orchestration
(2026-10-30). Planning designs from it and hands back what is to be kept;
it no longer stores anything itself.

    actor  tdd:<STORY>        snapshots memory:<STORY>:<seq>:<part>  (encrypted)
                              conversation page:<id> | story:<STORY> | versions:<STORY>
    actor  project:<PROJECT>  snapshots memory:project:<PROJECT>:... (encrypted)

The story's snapshot (Planning's format, unchanged): {"facts", "facts_hash",
"story_text_hash", "story_text", "versions": [...], "decisions": [{"uid", "id",
"story_key", "title", "choice", "reason", "status", "override_by", "source",
"applies_to": [{"service", "file_path", "symbol"}]}]}. The project's holds
every story's agreed decisions. Decisions live here, in one place only.
"""

from __future__ import annotations

from memory_service import store

AGREED = "agreed"
RECALL_LIMIT = 5
MAX_CONTENT = 7_500  # Memsy's limit is 8,000 characters per event


def _scope(story_key: str = "", project: str = "") -> tuple[str, str, str]:
    if project:
        return f"project:{project}", f"memory:project:{project}", "project_memory"
    key = story_key.strip().upper()
    return f"tdd:{key}", f"memory:{key}", "tdd_memory"


def agreed(snapshot: dict) -> list[dict]:
    return [d for d in snapshot.get("decisions") or [] if d.get("status") == AGREED]


async def load(story_key: str) -> tuple[int, dict]:
    actor, _, kind = _scope(story_key)
    return await store.load(actor, kind)


async def save(story_key: str, seq: int, snapshot: dict) -> None:
    """The story's new snapshot, and its agreed decisions in its project's."""
    actor, session, kind = _scope(story_key)
    latest = (snapshot.get("versions") or [{}])[-1].get("version") or "no version"
    summary = (
        f"{story_key.upper()} TDD {latest}: {len(snapshot.get('facts') or [])} requirement "
        f"facts and {len(agreed(snapshot))} decisions in force."
    )
    await store.save(actor, session, kind, seq, snapshot, summary)
    await _publish_to_project(story_key, snapshot)


async def _publish_to_project(story_key: str, snapshot: dict) -> None:
    project = store.project_of(story_key)
    actor, session, kind = _scope(project=project)
    seq, current = await store.load(actor, kind)
    keep = [d for d in current.get("decisions") or [] if d["story_key"] != story_key.upper()]
    merged = {"decisions": sorted(keep + agreed(snapshot), key=lambda d: d["uid"])}
    if merged == {"decisions": current.get("decisions") or []}:
        return
    summary = f"Project {project}: {len(merged['decisions'])} agreed design decisions."
    await store.save(actor, session, kind, seq + 1, merged, summary)


async def decisions_in_force(story_key: str, services: list[str] | None = None) -> list[dict]:
    """The story's agreed decisions, then the project's agreed decisions on the
    same services (a later story's design must keep them too)."""
    story_key = story_key.upper()
    own = agreed((await load(story_key))[1])
    actor, _, kind = _scope(project=store.project_of(story_key))
    wanted = set(services or [])
    project = [
        d
        for d in (await store.load(actor, kind))[1].get("decisions") or []
        if d["story_key"] != story_key
        and d.get("status") == AGREED
        and (not wanted or any(t.get("service") in wanted for t in d.get("applies_to") or []))
    ]
    return own + project


# --- the conversation about a TDD ------------------------------------------------------------


def _conversation_event(story_key: str, session: str, kind: str, content: str, meta: dict):
    key = story_key.strip().upper()
    return store.event(
        f"tdd:{key}",
        session,
        kind,
        content[:MAX_CONTENT],
        {"story_key": key, **meta},
        store.project_of(key),
    )


async def recall(story_key: str, question: str) -> list[str]:
    """What was said before about this TDD, most relevant first."""
    if not story_key or not question.strip():
        return []
    found = await store.search(question[:1000], f"tdd:{story_key.strip().upper()}", RECALL_LIMIT)
    return [r["content"].strip() for r in found if r["content"].strip()]


async def remember_exchange(
    story_key: str, page_id: str, question: str, answer: str, kind: str, verdict: str
) -> None:
    """A question about the TDD and Planning's answer, as one turn of its conversation."""
    if not story_key or not question.strip():
        return
    session = f"page:{page_id}" if page_id else f"story:{story_key.strip().upper()}"
    meta = {"kind": kind, "verdict": verdict, "page_id": page_id}
    events = [_conversation_event(story_key, session, "user_message", question, meta)]
    if answer.strip():
        events.append(_conversation_event(story_key, session, "assistant_message", answer, meta))
    await store.write(events)


async def remember_version(
    story_key: str, version: str, page_id: str, decisions: list[dict], superseded: list[str]
) -> None:
    """A note that a TDD version was published, with the decisions it carries."""
    if not story_key or not version:
        return
    carried = "; ".join(
        f"{d.get('title') or d.get('id')}: {d.get('choice') or ''}".strip(": ")
        for d in decisions
        if d.get("id")
    )
    text = f"{story_key.upper()} TDD {version} was published."
    if carried:
        text += f" Decisions in force: {carried}."
    if superseded:
        text += f" Replaced decisions: {', '.join(superseded)}."
    await store.write(
        [
            _conversation_event(
                story_key,
                f"versions:{story_key.strip().upper()}",
                "app_event",
                text,
                {"tdd_version": version, "page_id": page_id},
            )
        ]
    )
