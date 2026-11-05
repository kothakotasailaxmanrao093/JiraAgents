"""A local stand-in for Aetherion's Memsy, kept in a file (2026-11-03).

On the platform, Orchestration's memory (memory_service/store.py) goes through
common_lib.memsy, whose keys the platform injects into the pod; a laptop has
none. This behaves like Memsy where the store depends on it — every event's
metadata kept unchanged, one memory per session listed newest first, search by
(roughly) meaning, no update or delete — and keeps every event in
local-output/memsy/events.json, so a local run remembers between runs and never
touches the platform's Memsy.

Local runs only: scripts/local_run.py installs it. Nothing under src/ imports it.
"""

from __future__ import annotations

import itertools
import json
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "local-output" / "memsy" / "events.json"


class FileMemsy:
    is_configured = True

    def __init__(self, path: Path = FILE):
        self.path = path
        self.events = []
        if path.is_file():
            for row in json.loads(path.read_text()):
                self.events.append((row["id"], types.SimpleNamespace(**row["event"])))
        self._ids = itertools.count(len(self.events) + 1)
        self.lister = types.SimpleNamespace(memories=types.SimpleNamespace(list=self._list))

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"id": eid, "event": {k: getattr(e, k) for k in
                                  ("actor_id", "session_id", "content", "metadata")}}
            for eid, e in self.events
        ]
        self.path.write_text(json.dumps(rows, indent=1))

    async def ingest(self, events):
        ids = []
        for e in events:
            eid = f"local-{next(self._ids)}"
            self.events.append((eid, types.SimpleNamespace(
                actor_id=e.actor_id, session_id=e.session_id, content=e.content,
                metadata=e.metadata if isinstance(e.metadata, str) else json.dumps(e.metadata),
            )))
            ids.append(eid)
        self._write()
        return ids

    async def status(self, event_ids):
        return {"completed_ids": list(event_ids), "pending_ids": [], "failed_ids": []}

    def _sessions(self, actor_id):
        first = {}
        for eid, e in self.events:
            if e.actor_id == actor_id:
                first.setdefault(e.session_id, (eid, e))
        return list(first.values())

    async def _list(self, *, actor_id=None, limit=50, offset=0, **_):
        mine = list(reversed(self._sessions(actor_id)))
        items = [
            types.SimpleNamespace(
                text=e.content,
                payload={"source_metadata": [{"event_id": eid,
                                              "metadata": json.loads(e.metadata)}]},
            )
            for eid, e in mine[offset: offset + limit]
        ]
        return types.SimpleNamespace(items=items, total=len(mine))

    async def search_rich(self, query, actor_id, limit=5, threshold=0.0, **_):
        """The share of the query's words a memory has; one result per session."""
        words = {w for w in query.lower().split() if len(w) > 2}
        results = []
        for eid, e in self._sessions(actor_id):
            have = set(e.content.lower().replace(".", " ").replace(",", " ").split())
            score = len(words & have) / len(words) if words else 0.0
            if score > 0 and score >= threshold:
                results.append({
                    "id": eid, "content": e.content, "score": score, "strength": 1.0,
                    "metadata": {},
                    "source_metadata": [{"event_id": eid, "metadata": json.loads(e.metadata)}],
                })
        results.sort(key=lambda r: -r["score"])
        return results[:limit]


def install() -> FileMemsy:
    """Orchestration's store reads and writes this file instead of Memsy."""
    from memory_service import store

    memsy = FileMemsy()
    store.service = lambda: memsy
    store._lister = lambda: memsy.lister
    return memsy
