"""What every card in a project did, kept in Memsy (2026-10-30): the services
it touches, its TDD, its branch and pull request, the files that changed.

    actor  work:<PROJECT>   one session per event: card:<KEY>:<event>:<id>

A new card's review finds earlier cards about the same thing by meaning
(Memsy's search), and gets their exact branches, PRs and files back from the
event's metadata, which Memsy returns unchanged; its readable text is only
what the search matches on (Memsy rewrites it). One session per event,
because Memsy merges similar events of one session into one memory.

Event metadata: {"type": "work_history", "event": card | design | pr_opened |
pr_state, "key", "v": 1, "at", ...}. No code and no secrets: names, paths,
numbers.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from card_memory import store

logger = logging.getLogger(__name__)

TYPE = "work_history"
EVENTS = ("card", "design", "pr_opened", "pr_state")
MAX_METADATA = 3_900  # Memsy keeps 4,096 characters of metadata per event
MAX_CONTENT = 2_000
MAX_QUERY = 1_000
SEARCH_LIMIT = 20
MIN_SCORE = 0.3  # Memsy's similarity below which a hit is not about the same thing
MAX_CARDS = 5


def actor_of(card_key: str) -> str:
    return f"work:{store.project_of(card_key)}"


def _fit(metadata: dict[str, Any]) -> str:
    """The metadata as JSON within Memsy's limit: long file lists are cut."""
    text = json.dumps(metadata, sort_keys=True)
    files = list(metadata.get("files") or [])
    while len(text) > MAX_METADATA and files:
        files = files[: len(files) // 2]
        text = json.dumps({**metadata, "files": files, "files_cut": True}, sort_keys=True)
    return text[:MAX_METADATA]


def sentence(card_key: str, event: str, data: dict[str, Any]) -> str:
    """What Memsy searches: the event in plain words."""
    key = card_key.upper()
    title = str(data.get("title") or "").strip()
    services = ", ".join(str(s) for s in data.get("services") or [])
    if event == "card":
        text = f"{key} {title}. Touches {services or 'no known service'}."
        if data.get("summary"):
            text += f" {data['summary']}"
    elif event == "design":
        text = f"{key} {title}: TDD {data.get('version', '')} designed {services}."
        decisions = "; ".join(str(d) for d in data.get("decisions") or [])
        if decisions:
            text += f" Decisions: {decisions}."
    elif event == "pr_opened":
        text = (
            f"{key} {title}: pull request #{data.get('pr')} on branch {data.get('branch')} "
            f"in {data.get('repo')} for {data.get('service') or 'its service'}."
        )
    else:
        files = ", ".join(str(f).rsplit("/", 1)[-1] for f in data.get("files") or [])
        text = (
            f"{key}: pull request #{data.get('pr')} in {data.get('repo')} was "
            f"{data.get('state')}" + (f"; it changed {files}." if files else ".")
        )
    return text[:MAX_CONTENT]


def write(card_key: str, event: str, data: dict[str, Any], event_id: str = "") -> None:
    """One fact about a card, as its own Memsy event.

    ``event_id`` names the event (a PR number and its state, a TDD version);
    without it the event's own content names it. The same card, event and id
    is one session, so a repeated webhook is not two memories, while a card
    reviewed again with other services is a new event (the newest one wins)."""
    if event not in EVENTS:
        raise ValueError(f"unknown history event {event!r}")
    from memsy import EventPayload

    key = card_key.strip().upper()
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    metadata = {"type": TYPE, "event": event, "key": key, "v": 1, "at": stamp, **data}
    name = (
        event_id
        or hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    )
    store.client().ingest(
        [
            EventPayload(
                actor_id=actor_of(key),
                session_id=f"card:{key}:{event}:{name}",
                kind="app_event",
                content=sentence(key, event, data),
                team_id=store.project_of(key),
                metadata=_fit(metadata),
            )
        ]
    )


def _metas(result: Any) -> list[dict[str, Any]]:
    """The history metadata a search result came from."""
    sources = getattr(result, "source_metadata", None)
    if sources is None:
        sources = (getattr(result, "metadata", None) or {}).get("source_metadata") or []
    out = []
    for source in sources or []:
        meta = source.get("metadata") if isinstance(source, dict) else None
        if isinstance(meta, dict) and meta.get("type") == TYPE and meta.get("key"):
            out.append(meta)
    return out


def related_cards(results: list[tuple[float, dict]], card_key: str) -> list[dict[str, Any]]:
    """Search hits grouped by card, best first: each card's services, title and
    pull requests (the newest state of each PR wins). The card itself is left out."""
    card_key = card_key.upper()
    cards: dict[str, dict[str, Any]] = {}
    for score, meta in results:
        key = str(meta["key"]).upper()
        if key == card_key or score < MIN_SCORE:
            continue
        card = cards.setdefault(
            key,
            {"card": key, "score": 0.0, "title": "", "services": [], "prs": {}, "_at": ""},
        )
        card["score"] = max(card["score"], round(float(score), 3))
        card["title"] = card["title"] or str(meta.get("title") or "")
        at = str(meta.get("at") or "")
        if meta.get("event") in ("card", "design") and meta.get("services") and at >= card["_at"]:
            # The newest review or TDD says which services the card touches.
            card["services"], card["_at"] = [str(s) for s in meta["services"]], at
        if meta.get("event") in ("pr_opened", "pr_state") and meta.get("pr"):
            pr = card["prs"].setdefault(
                (str(meta.get("repo") or ""), int(meta["pr"])),
                {"pr": int(meta["pr"]), "repo": str(meta.get("repo") or ""), "_at": ""},
            )
            for field in ("branch", "url", "files", "service"):
                if meta.get(field) and not pr.get(field):
                    pr[field] = meta[field]
            if meta.get("event") == "pr_state" and at >= pr["_at"]:
                pr["state"], pr["_at"] = str(meta.get("state") or ""), at
            pr.setdefault("state", "open")
    ranked = sorted(cards.values(), key=lambda c: -c["score"])[:MAX_CARDS]
    for card in ranked:
        card.pop("_at")
        for pr in card["prs"].values():
            pr.pop("_at")
            if pr.get("service") and pr["service"] not in card["services"]:
                card["services"].append(str(pr["service"]))
        card["prs"] = sorted(card["prs"].values(), key=lambda p: p["pr"])
    return ranked


def find(card_key: str, queries: list[str]) -> list[dict[str, Any]]:
    """Earlier cards in the project about the same thing as ``queries`` (the
    card's text, then its title), searched by meaning in Memsy; each with all
    its events (services, branches, pull requests, files)."""
    memsy = store.client()
    actor = actor_of(card_key)
    hits: list[tuple[float, dict]] = []
    for query in [q.strip()[:MAX_QUERY] for q in queries if q and q.strip()]:
        try:
            found = memsy.search(query, actor_id=actor, limit=SEARCH_LIMIT)
        except Exception as error:  # noqa: BLE001 - one failed search, not all
            logger.warning("History search %r failed: %s", query[:60], error)
            continue
        for result in found.results or []:
            for meta in _metas(result):
                hits.append((float(result.score or 0.0), meta))
    best = {c["card"]: c["score"] for c in related_cards(hits, card_key)}
    if best:
        # A search finds a card by one of its events; its pull requests must come too.
        for meta in store.listed_metadata(memsy, actor):
            key = str(meta.get("key") or "").upper()
            if meta.get("type") == TYPE and key in best:
                hits.append((best[key], meta))
    return related_cards(hits, card_key)
