"""Start the router's scheduled checks on this laptop every minute (2026-10-03).

The local stand-in for Aetherion's Schedule, on the router's local queue:
``{"github_poll": true}`` (merges → Planning refreshes its code graph; the story
hears of it) and, since 2026-10-08, ``{"confluence_poll": true}`` (comments on
the TDD pages). One check at a time — a check still running (Planning or Coding
can take minutes) is never overlapped. Started by ``scripts/run_local.sh --poll``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_worker import local_queue  # noqa: E402

INTERVAL_SECONDS = 60


async def main() -> None:
    from aetherion_sdk.runtime import AetherionRuntime

    client = await AetherionRuntime().connect()
    queue = local_queue("jira-orchestration")
    print(f"checking GitHub every {INTERVAL_SECONDS}s through {queue}", flush=True)
    while True:
        started = time.monotonic()
        stamp = time.strftime("%H:%M:%S")
        try:
            handle = await client.start_workflow(
                "JiraOrchestration", {"github_poll": True}, id="github-poll-local", task_queue=queue
            )
            result = await handle.result()
            summary = {k: result.get(k) for k in ("repos", "sent", "failed", "error") if k in result}
            print(f"{stamp} github {summary} {result.get('notes') or ''}", flush=True)
            for item in result.get("results") or []:
                print(f"         {'sent' if item['ok'] else 'FAILED'}: {item['event']} — {item['message']}", flush=True)
        except Exception as exc:  # noqa: BLE001 — keep checking; say what happened
            print(f"{stamp} github check did not run: {type(exc).__name__}: {exc}", flush=True)
        try:
            handle = await client.start_workflow(
                "JiraOrchestration", {"confluence_poll": True}, id="confluence-poll-local", task_queue=queue
            )
            result = await handle.result()
            answered = result.get("answered") or []
            print(f"{stamp} confluence pages={result.get('pages', 0)} answered={len(answered)} {result.get('error') or ''}", flush=True)
            for item in answered:
                print(f"         {item.get('intent')}: comment {item.get('comment')} {item.get('links') or ''}", flush=True)
        except Exception as exc:  # noqa: BLE001 — keep checking; say what happened
            print(f"{stamp} confluence check did not run: {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(max(0.0, INTERVAL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    asyncio.run(main())
