"""Agent — local_flow: Review, Planning and Coding by hand on this laptop (2026-11-03).

Local only, never published: it lives outside every agent's folder. From the
local test form, one step at a time — review the card, plan it, code it — with
what Orchestration does around each step done here (the card read from Jira,
its memory recalled and kept, its services ranked). The work is the tool's
(local_flow_tools); this only routes the form's fields to it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from aetherion_sdk import agent, toolExecutor
from temporalio.common import RetryPolicy

# Each step starts the real agent and waits for it: Planning and Coding take minutes.
_STEP = {"start_to_close_timeout": timedelta(minutes=60),
         "retry_policy": RetryPolicy(maximum_attempts=1)}
_STEPS = ("review", "plan", "code", "memory")


@agent(name="local_flow")
async def local_flow(payload: dict[str, Any]) -> dict[str, Any]:
    step = str(payload.get("step") or "").strip().lower()
    if step not in _STEPS:
        return {"status": "error", "message": f"step must be one of: {', '.join(_STEPS)}"}
    uploaded = payload.get("uploaded_files") or []
    file_keys = [str(k) for k in ([uploaded] if isinstance(uploaded, str) else uploaded) if k]
    try:
        return await toolExecutor.execute(
            "local_step",
            step,
            str(payload.get("issue_key") or "").strip(),
            file_keys,
            str(payload.get("branch_name") or "").strip(),
            str(payload.get("target_branch") or "").strip(),
            str(payload.get("answers") or ""),
            str(payload.get("services") or ""),
            **_STEP,
        )
    except Exception as error:  # noqa: BLE001 - shown on the form, never hidden
        while getattr(error, "cause", None) is not None:
            error = error.cause
        return {"status": "error", "message": f"{type(error).__name__}: {error}"[:2000]}
