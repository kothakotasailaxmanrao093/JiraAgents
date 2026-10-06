"""Start a GitHub check on the local router every minute (2026-10-03).

The local stand-in for Aetherion's Schedule: ``{"github_poll": true}`` on the
router's local queue, one check at a time — a check still running (Planning
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
            print(f"{stamp} {summary} {result.get('notes') or ''}", flush=True)
            for item in result.get("results") or []:
                print(f"         {'sent' if item['ok'] else 'FAILED'}: {item['event']} — {item['message']}", flush=True)
        except Exception as exc:  # noqa: BLE001 — keep checking; say what happened
            print(f"{stamp} check did not run: {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(max(0.0, INTERVAL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    asyncio.run(main())
