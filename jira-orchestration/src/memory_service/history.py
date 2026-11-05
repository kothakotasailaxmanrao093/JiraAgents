"""What every card in a project did, kept in Memsy by Orchestration
(2026-10-30): the services it touches, its TDD, its branch and pull request,
the files that changed.

    actor  work:<PROJECT>   one session per event: card:<KEY>:<event>:<id>

Before a card is reviewed, earlier cards about the same thing are found by
meaning (Memsy's search), with their exact branches, PRs and files from the
event's metadata, which Memsy returns unchanged; its readable text is only
what the search matches on (Memsy rewrites it). One session per event,
because Memsy merges similar events of one session into one memory.

Design decisions are not kept here: they live in Planning's TDD memory
(designs.py), one place only. A TDD event names its version and services.

Event metadata: {"type": "work_history", "event": card | design | pr_opened |
pr_state, "key", "v": 1, "at", ...}. No code and no secrets: names, paths,
numbers.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from memory_service import store

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


def _fit(metadata: dict[str, Any]) -> dict[str, Any]:
    """The metadata within Memsy's limit: a long file list is halved until it
    fits (``files_cut``), then the free text is shortened."""
    out = dict(metadata)
    while len(json.dumps(out, sort_keys=True)) > MAX_METADATA and out.get("files"):
        out = {**out, "files": list(out["files"])[: len(out["files"]) // 2], "files_cut": True}
    for field in ("summary", "title"):
        over = len(json.dumps(out, sort_keys=True)) - MAX_METADATA
        if over > 0 and isinstance(out.get(field), str):
            out[field] = out[field][: max(0, len(out[field]) - over)]
    return out


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


async def write(card_key: str, event: str, data: dict[str, Any], event_id: str = "") -> None:
    """One fact about a card, as its own Memsy event.

    ``event_id`` names the event (a PR number and its state, a TDD version);
    without it the event's own content names it. The same card, event and id
    is one session, so a repeated webhook is not two memories, while a card
    reviewed again with other services is a new event (the newest one wins)."""
    if event not in EVENTS:
        raise ValueError(f"unknown history event {event!r}")
    key = card_key.strip().upper()
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    metadata = {"type": TYPE, "event": event, "key": key, "v": 1, "at": stamp, **data}
    name = (
        event_id
        or hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    )
    await store.write(
        [
            store.event(
                actor_of(key),
                f"card:{key}:{event}:{name}",
                "app_event",
                sentence(key, event, data),
                _fit(metadata),
                store.project_of(key),
            )
        ]
    )


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


async def find(card_key: str, query: str) -> list[dict[str, Any]]:
    """Earlier cards in the project about the same thing as ``query`` (the
    card's title and text), found with ONE search by meaning; each with all its
    events (services, branches, pull requests, files)."""
    actor = actor_of(card_key)
    hits: list[tuple[float, dict]] = []
    for result in await store.search(query.strip()[:MAX_QUERY], actor, SEARCH_LIMIT, MIN_SCORE):
        for meta in result["metadata"]:
            if meta.get("type") == TYPE and meta.get("key"):
                hits.append((result["score"], meta))
    best = {c["card"]: c["score"] for c in related_cards(hits, card_key)}
    if best:
        # A search finds a card by one of its events; its pull requests must come too.
        for meta in await store.listed_metadata(actor):
            key = str(meta.get("key") or "").upper()
            if meta.get("type") == TYPE and key in best:
                hits.append((best[key], meta))
    return related_cards(hits, card_key)
