"""Show what the Planning agent was given by the router's GitHub check, and the
plan it wrote (2026-10-04).

Reads the most recent Planning runs from the workflow server: when each
started, its status, the input it received (issue_text), and its answer —
the technical design document, or why it could not write one. Read-only.

    jira-orchestration/.venv/bin/python scripts/show_planning_inputs.py [how_many] [--save]

``--save`` also writes each full technical design document (Planning's
section-numbered text version) to ``planning-tdds/<run id>.txt``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

TDD_DIR = Path(__file__).resolve().parents[1] / "planning-tdds"


async def main(limit: int, save: bool) -> None:
    from aetherion_sdk.runtime import AetherionRuntime

    client = await AetherionRuntime().connect()
    query = 'WorkflowType = "planning_agent" AND WorkflowId STARTS_WITH "planning-gh-"'
    shown = 0
    async for run in client.list_workflows(query):
        handle = client.get_workflow_handle(run.id, run_id=run.run_id)
        payload = {}
        async for event in handle.fetch_history_events():
            attrs = event.workflow_execution_started_event_attributes
            if attrs.input.payloads:
                payload = client.data_converter.payload_converter.from_payload(attrs.input.payloads[0])
                break
        print("=" * 72)
        print(f"{run.start_time:%Y-%m-%d %H:%M:%S} UTC  {run.status.name if run.status else '?'}  {run.id}")
        print(f"event: {payload.get('event')}  repo: {payload.get('repo')}  PR #{payload.get('pr_number')}")
        print("--- INPUT given to Planning " + "-" * 44)
        print(payload.get("issue_text", "(no issue_text)"))
        print("--- PLAN written by Planning " + "-" * 43)
        summary, full = await _plan(handle)
        print(summary)
        if save and full:
            TDD_DIR.mkdir(exist_ok=True)
            path = TDD_DIR / f"{run.id}.txt"
            path.write_text(full)
            print(f"--- full document saved: {path}")
        shown += 1
        if shown >= limit:
            break
    if not shown:
        print("Planning has not been given anything yet.")


async def _plan(handle) -> tuple[str, str]:
    """(the plan's summary, the full document) — or why there is none.

    Planning reports its own failures as data, so a completed run is checked.
    """
    try:
        result = await asyncio.wait_for(handle.result(), timeout=5)
    except TimeoutError:
        return "(still running)", ""
    except Exception as exc:  # noqa: BLE001 — the run itself failed
        return f"(the run failed: {exc})", ""
    if not isinstance(result, dict) or result.get("status") != "success":
        return f"(no plan: {(result or {}).get('message') or result})", ""
    return result.get("markdown") or "(empty plan)", result.get("text") or ""


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--save"]
    asyncio.run(main(int(args[0]) if args else 3, "--save" in sys.argv[1:]))
