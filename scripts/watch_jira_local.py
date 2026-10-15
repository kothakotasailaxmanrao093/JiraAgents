"""Hand new "@<keyword>" Jira comments to the router on this laptop (2026-10-08).

On Aetherion a Jira comment reaches the published router through the webhook.
A laptop has no webhook, so this watches the allowed projects instead: every
20 seconds it looks for comments written since it started that mention the
keyword (and in the 5 minutes before it started), and starts the local router for each — exactly what the webhook would
send (issue key, comment id, comment_created). The router itself still decides
whether to answer (keyword, signature, already answered).

Started by ``scripts/run_local.sh --poll``; run it alone from jira-orchestration:

    .venv/bin/python ../scripts/watch_jira_local.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_worker import local_queue  # noqa: E402

INTERVAL_SECONDS = 20
LOOKBACK_MINUTES = 5
SIGNATURE = "AetherionAgent"


def texts(node) -> list[str]:
    """Every text run in an ADF document."""
    if isinstance(node, dict):
        out = [node["text"]] if node.get("type") == "text" and node.get("text") else []
        for child in node.get("content") or []:
            out += texts(child)
        return out
    if isinstance(node, list):
        return [t for item in node for t in texts(item)]
    return []


def asks(body, keyword: str) -> bool:
    """A person's comment that mentions the keyword (never the agent's own reply)."""
    text = " ".join(texts(body)) if not isinstance(body, str) else body
    if SIGNATURE in text:
        return False
    return re.search(rf"(?<![\w])@{re.escape(keyword)}(?![\w])", text, re.IGNORECASE) is not None


async def main() -> None:
    from aetherion_sdk.runtime import AetherionRuntime

    load_dotenv(Path.cwd() / ".env", override=False)
    base = os.environ["JIRA_BASE_URL"].rstrip("/")
    auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
    keyword = os.environ.get("ORCH_TRIGGER_KEYWORD") or "Aetherion"
    projects = [p.strip() for p in os.environ.get("ORCH_ALLOWED_PROJECT_KEYS", "").split(",") if p.strip()]
    # A comment written while the watcher (re)starts is not lost: the last few
    # minutes are read too. The router answers each comment once (its own
    # "answered" record), so nothing is answered twice.
    started = datetime.now(UTC) - timedelta(minutes=LOOKBACK_MINUTES)
    seen: set[str] = set()

    client = await AetherionRuntime().connect()
    queue = local_queue("jira-orchestration")
    scope = f"project in ({', '.join(projects)}) AND " if projects else ""
    print(f"watching Jira for @{keyword} in {', '.join(projects) or 'every project'} — {queue}", flush=True)
    async with httpx.AsyncClient(base_url=base, auth=auth, timeout=30) as jira:
        while True:
            try:
                resp = await jira.post(
                    "/rest/api/3/search/jql",
                    json={"jql": f"{scope}updated >= -3m ORDER BY updated DESC", "fields": ["key"], "maxResults": 25},
                )
                resp.raise_for_status()
                for issue in resp.json().get("issues") or []:
                    key = issue["key"]
                    comments = await jira.get(
                        f"/rest/api/3/issue/{key}/comment", params={"orderBy": "-created", "maxResults": 20}
                    )
                    comments.raise_for_status()
                    for c in comments.json().get("comments") or []:
                        cid = str(c["id"])
                        created = datetime.fromisoformat(c["created"].replace("Z", "+00:00"))
                        if cid in seen or created < started or not asks(c.get("body"), keyword):
                            continue
                        seen.add(cid)
                        handle = await client.start_workflow(
                            "JiraOrchestration",
                            {"issue_key": key, "comment_id": cid, "webhookEvent": "comment_created"},
                            id=f"watch-{key}-{cid}-{int(time.time())}",
                            task_queue=queue,
                        )
                        print(f"{time.strftime('%H:%M:%S')} {key} comment {cid} → router ({handle.id})", flush=True)
            except Exception as exc:  # noqa: BLE001 — keep watching; say what happened
                print(f"{time.strftime('%H:%M:%S')} watch failed: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
