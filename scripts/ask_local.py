"""Send one request to the router running on this laptop (2026-10-06).

On Aetherion a Jira comment reaches the router through the webhook. A laptop
has no webhook, so this starts the local router the same way the webhook
would, on the router's own local queue (``scripts/run_local.sh`` must be
running).

    cd jira-orchestration
    .venv/bin/python ../scripts/ask_local.py --health
        reaches Task Creation and Review without touching Jira
    .venv/bin/python ../scripts/ask_local.py BGV-12 10234
        handles the @Aetherion comment 10234 on BGV-12, exactly as the webhook would
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_worker import local_queue  # noqa: E402

USAGE = "usage: ask_local.py --health | ISSUE-KEY COMMENT-ID"


def payload_for(args: list[str]) -> dict:
    """The router payload the webhook would send, or the health check."""
    if args == ["--health"]:
        return {"health_check": True}
    if len(args) == 2:
        issue_key, comment_id = args
        return {
            "issue_key": issue_key.strip().upper(),
            "comment_id": comment_id.strip(),
            "webhookEvent": "comment_created",
        }
    raise SystemExit(USAGE)


async def main(args: list[str]) -> None:
    from aetherion_sdk.runtime import AetherionRuntime

    payload = payload_for(args)
    client = await AetherionRuntime().connect()
    queue = local_queue("jira-orchestration")
    handle = await client.start_workflow(
        "JiraOrchestration",
        payload,
        id=f"ask-local-{int(time.time())}",
        task_queue=queue,
    )
    print(f"started {handle.id} on {queue}; waiting for the result…", flush=True)
    print(json.dumps(await handle.result(), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
