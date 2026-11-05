"""A stand-in for Aetherion's Memsy service (common_lib.memsy) that behaves
like the real one where it matters: it rewrites the text it stores, keeps each
event's metadata unchanged in ``source_metadata``, merges the events of one
session into one memory that keeps only the first event's metadata, lists an
actor's memories newest first, searches by (roughly) meaning, has no update or
delete, and never raises (a failed write returns no ids).

``FakeMemsy`` is the service (``store.service``); ``FakeMemsy.lister`` is the
client it wraps, which the store lists with (``store._lister``)."""

import itertools
import json
import types


class FakeMemsy:
    is_configured = True

    def __init__(self):
        self.events = []
        self._ids = itertools.count(1)
        self.lister = types.SimpleNamespace(memories=types.SimpleNamespace(list=self._list))

    async def ingest(self, events):
        ids = []
        for e in events:
            eid = f"e{next(self._ids)}"
            self.events.append((eid, e))
            ids.append(eid)
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
                text=f"Rewritten by Memsy: {e.content}",
                payload={
                    "source_metadata": [{"event_id": eid, "metadata": json.loads(e.metadata)}]
                },
            )
            for eid, e in mine[offset : offset + limit]
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
                results.append(
                    {
                        "id": eid,
                        "content": f"Rewritten by Memsy: {e.content}",
                        "score": score,
                        "strength": 1.0,
                        "metadata": {},
                        "source_metadata": [{"event_id": eid, "metadata": json.loads(e.metadata)}],
                    }
                )
        results.sort(key=lambda r: -r["score"])
        return results[:limit]

    def stored(self, actor_id):
        return [e for _, e in self.events if e.actor_id == actor_id]
