"""What the memory holds for a card (2026-10-30) — read only, nothing is written.
Read through Aetherion's Memsy service (common_lib.memsy), as the agent does.

Everything Orchestration keeps: the card's review memory (questions with their
answers, ranked services), its TDD memory (facts, versions, decisions), the
project's agreed decisions, and the project's history (cards, TDDs, PRs,
merges). With --search, which earlier cards a review of that text would find.

Usage (from jira-orchestration/, with MEMSY_* and CODE_GRAPH_KEY in .env):
    PYTHONPATH=src uv run python scripts/show_memory.py BTS-60
    PYTHONPATH=src uv run python scripts/show_memory.py BTS-60 --search "IMPS remark"
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from memory_service import cards, designs, history, ranking, store  # noqa: E402


async def _review(card_key: str) -> None:
    seq, snapshot = await cards.load(card_key)
    print(f"== Review memory of {card_key} (snapshot {seq or 'none'})")
    for q in snapshot.get("questions") or []:
        answers = [a["text"] for a in q.get("answers") or []]
        print(f"  {q['id']} {q['question']}")
        print(f"       -> {' / '.join(answers) if answers else '(not answered yet)'}")
    for s in ranking.in_order(list(snapshot.get("services") or [])):
        prs = ", ".join(f"{p['card']} PR #{p['pr']}" for p in s.get("prs") or [])
        extra = f" [{prs}]" if prs else ""
        print(f"  service {s['rank']}. {s['service']}: {s['reason']}{extra}")


async def _tdd(card_key: str) -> None:
    seq, snapshot = await designs.load(card_key)
    print(f"\n== TDD memory of {card_key} (snapshot {seq or 'none'})")
    for v in snapshot.get("versions") or []:
        print(f"  version {v.get('version')} page {v.get('page_id', '')} {v.get('created', '')}")
    for f in snapshot.get("facts") or []:
        print(f"  fact [{f.get('kind', '')}] {f.get('statement', '')}")
    for d in await designs.decisions_in_force(card_key):
        print(f"  decision {d['id']} ({d['story_key']}): {d.get('choice')}")


async def _history(card_key: str) -> None:
    actor = history.actor_of(card_key)
    events = [m for m in await store.listed_metadata(actor) if m.get("type") == history.TYPE]
    print(f"\n== History of project {actor} ({len(events)} events, newest first)")
    for m in sorted(events, key=lambda m: str(m.get("at") or ""), reverse=True):
        mark = "*" if m["key"] == card_key.upper() else " "
        print(f" {mark} {m.get('at', '')}  {history.sentence(m['key'], m['event'], m)}")


async def _search(card_key: str, text: str) -> None:
    print(f"\n== Earlier cards a review of {text!r} finds")
    for card in await history.find(card_key, text):
        print(f"  {card['card']} (score {card['score']}) {card['title']}: {card['services']}")
        for p in card["prs"]:
            files = ", ".join(p.get("files") or [])
            print(f"     PR #{p['pr']} {p.get('branch', '')} {p.get('state', '')} {files}")


async def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    card_key = argv[0].strip().upper()
    await _review(card_key)
    await _tdd(card_key)
    await _history(card_key)
    if "--search" in argv[1:]:
        await _search(card_key, " ".join(argv[argv.index("--search") + 1 :]))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
