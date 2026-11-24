"""A run started from the page: one step (review, plan, code) or the whole
flow, done by scripts/local_run.py on the local workers.

One run at a time. Every line printed or logged while it runs is kept with it,
and every agent workflow it starts is reported (local_run.RUN_OBSERVER) with
its Temporal link. Finished runs are kept in local-output/studio/runs.json.
"""

from __future__ import annotations

import asyncio
import contextvars
import io
import json
import logging
import re
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from . import config, documents  # config first: it puts scripts/ on the path

import local_run  # noqa: E402,I001

KINDS = ("review", "breakdown", "plan", "code", "pipeline")
_CURRENT: contextvars.ContextVar[Job | None] = contextvars.ContextVar("studio_job", default=None)
LOCK = asyncio.Lock()
LIVE: dict[str, Job] = {}


class Job:
    def __init__(self, kind: str, issue_key: str, options: dict[str, Any]) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.issue_key = issue_key
        self.options = options
        self.status = "running"  # running | done | needs_answers | failed
        self.started = time.time()
        self.ended: float | None = None
        self.lines: list[dict[str, Any]] = []
        self.steps: dict[str, dict[str, Any]] = {}
        self.workflows: list[dict[str, Any]] = []
        self.error = ""

    def log(self, text: str) -> None:
        self.lines.append({"t": round(time.time() - self.started, 1), "text": text})
        del self.lines[:-800]

    def step(self, name: str, **fields: Any) -> dict[str, Any]:
        entry = self.steps.setdefault(name, {"name": name, "status": "waiting"})
        entry.update(fields)
        return entry

    def on_workflow(
        self, event: str, workflow: str, agent_dir: str, workflow_id: str, queue: str, result: Any
    ) -> None:
        """local_run's report of an agent run starting, finishing or failing."""
        found = next((w for w in self.workflows if w["id"] == workflow_id), None)
        if found is None:
            found = {
                "id": workflow_id,
                "workflow": workflow,
                "agent": config.AGENTS.get(agent_dir, agent_dir),
                "queue": queue,
                "url": f"{config.TEMPORAL_UI}/{workflow_id}",
                "started": time.time(),
            }
            self.workflows.append(found)
        found["status"] = {"started": "running", "finished": "completed", "failed": "failed"}[event]
        if event != "started":
            found["ended"] = time.time()
        if event == "failed":
            found["error"] = str(result)[:500]
        for name, agent in config.STEP_AGENT.items():
            if agent == agent_dir and name in self.steps:
                ids = self.steps[name].setdefault("workflow_ids", [])
                if workflow_id not in ids:
                    ids.append(workflow_id)

    def view(self, full: bool = True) -> dict[str, Any]:
        out = {
            "id": self.id,
            "kind": self.kind,
            "issue_key": self.issue_key,
            "options": {k: v for k, v in self.options.items() if k != "answers"},
            "status": self.status,
            "started": self.started,
            "ended": self.ended,
            "steps": self.steps,
            "workflows": self.workflows,
            "error": self.error,
        }
        if full:
            out["lines"] = self.lines
        return out


# --- each run's own log ---------------------------------------------------------------


class Tee(io.TextIOBase):
    """stdout, with each line also given to the run that printed it."""

    def __init__(self, real) -> None:
        self.real = real
        self._partial: dict[str, str] = {}

    def write(self, text: str) -> int:
        self.real.write(text)
        job = _CURRENT.get()
        if job is not None:
            *lines, rest = (self._partial.get(job.id, "") + text).split("\n")
            self._partial[job.id] = rest
            for line in lines:
                if line.strip():
                    job.log(line.rstrip())
        return len(text)

    def flush(self) -> None:
        self.real.flush()


class LogHandler(logging.Handler):
    """Log records made during a run, added to that run's log."""

    _SKIP = ("httpx", "httpcore", "temporalio", "uvicorn")

    def emit(self, record: logging.LogRecord) -> None:
        job = _CURRENT.get()
        if job is None or record.name.startswith(self._SKIP):
            return
        try:
            job.log(f"{record.levelname.lower()}: {record.getMessage()}"[:600])
        except Exception:  # noqa: BLE001 - a log line must never break the run
            pass


# --- history --------------------------------------------------------------------------


def history() -> list[dict[str, Any]]:
    try:
        return json.loads(config.RUNS_FILE.read_text())
    except (OSError, ValueError):
        return []


def _save(job: Job) -> None:
    kept = [r for r in history() if r.get("id") != job.id]
    kept.insert(0, job.view(full=False))
    config.STATE.mkdir(parents=True, exist_ok=True)
    config.RUNS_FILE.write_text(json.dumps(kept[:200], indent=1, default=str))


def find(job_id: str) -> dict[str, Any] | None:
    job = LIVE.get(job_id)
    if job is not None:
        return job.view()
    found = next((r for r in history() if r.get("id") == job_id), None)
    return {**found, "lines": []} if found else None


def all_runs() -> list[dict[str, Any]]:
    live = [j.view(full=False) for j in LIVE.values() if j.status == "running"]
    ids = {r["id"] for r in live}
    return live + [r for r in history() if r.get("id") not in ids]


# --- the steps ------------------------------------------------------------------------


def _score(message: str) -> int | None:
    found = re.search(r"readiness (\d)/5", message)
    return int(found.group(1)) if found else None


async def _review(job: Job) -> dict[str, Any]:
    step = job.step("review", status="running", started=time.time())
    out = await local_run.review(
        job.issue_key, job.options.get("answers") or "", list(job.options.get("files") or [])
    )
    step.update(
        status="done" if out.get("ready") else "needs_answers",
        ended=time.time(),
        message=out.get("message"),
        ready=bool(out.get("ready")),
        open_questions=out.get("open_questions") or [],
        document=documents.doc(out.get("pdf")),
        note=out.get("note"),
        score=out.get("score", _score(out.get("message") or "")),
        review_score=out.get("review_score"),
    )
    return out


async def _plan(job: Job, review_pdf: str | None) -> dict[str, Any]:
    step = job.step("plan", status="running", started=time.time())
    files = list(job.options.get("plan_files") or [])
    if review_pdf and Path(review_pdf).is_file():
        files.insert(0, documents.as_upload(review_pdf))  # Planning reads the review too
    out = await local_run.plan(job.issue_key, files, job.options.get("services") or "")
    if out.get("status") != "success":
        step.update(status="failed", ended=time.time(), message=out.get("message"))
        return out
    step.update(
        status="done",
        ended=time.time(),
        message=out.get("message"),
        services=out.get("services") or [],
        document=documents.doc(out.get("pdf")),
    )
    return out


async def _code(job: Job, tdd_pdf: str) -> dict[str, Any]:
    step = job.step("code", status="running", started=time.time())
    out = await local_run.code(tdd_pdf, job.options.get("branch_name") or "", "")
    pushed = bool(out.get("pull_request_links") or out.get("branches"))
    step.update(
        status="done" if pushed else "failed",
        ended=time.time(),
        message=out.get("message"),
        pull_requests=out.get("pull_request_links") or [],
        branches=out.get("branches") or [],
        branch=out.get("branch"),
        services=out.get("services") or [],
    )
    return out


async def _breakdown(job: Job) -> dict[str, Any]:
    """Task Creation: the card split into Sub-tasks (a Story) or Stories (an Epic)."""
    create = bool(job.options.get("create_in_jira"))
    step = job.step("tasks", status="running", started=time.time(), create=create)
    out = await local_run.breakdown(job.issue_key, create)
    step.update(
        status="done" if out.get("status") == "success" else "failed",
        ended=time.time(),
        message=out.get("message"),
        outcome=out.get("outcome"),
        proposed=out.get("proposed") or [],
        created=out.get("created") or [],
        document=documents.doc(out.get("pdf")),
    )
    return out


async def _pipeline(job: Job) -> None:
    """Review, then plan, then code — stopping where a person is needed."""
    job.step("review"), job.step("plan"), job.step("code")
    reviewed = await _review(job)
    if not reviewed.get("ready"):
        job.status = "needs_answers"
        job.log("The review needs answers before planning: answer the open questions and run again.")
        return
    planned = await _plan(job, reviewed.get("pdf"))
    if planned.get("status") != "success":
        job.status = "failed"
        job.error = planned.get("message") or "Planning wrote no TDD."
        return
    await _code(job, planned["pdf"])
    job.status = "done" if job.steps["code"]["status"] == "done" else "failed"


async def _run(job: Job) -> None:
    _CURRENT.set(job)
    local_run.RUN_OBSERVER.set(job.on_workflow)
    try:
        if job.kind == "review":
            job.status = "done" if (await _review(job)).get("ready") else "needs_answers"
        elif job.kind == "breakdown":
            out = await _breakdown(job)
            job.status = "done" if out.get("status") == "success" else "failed"
        elif job.kind == "plan":
            out = await _plan(job, job.options.get("review_pdf"))
            job.status = "done" if out.get("status") == "success" else "failed"
        elif job.kind == "code":
            tdd = job.options.get("tdd_pdf") or ""
            if not tdd:
                raise ValueError("No TDD to code: run Planning first or upload a TDD PDF.")
            await _code(job, tdd)
            job.status = "done" if job.steps["code"]["status"] == "done" else "failed"
        else:
            await _pipeline(job)
    except BaseException as error:  # noqa: BLE001 - shown on the page, never hidden
        root = error
        while getattr(root, "cause", None) is not None:
            root = root.cause
        job.status = "failed"
        job.error = f"{type(root).__name__}: {root}"[:1500]
        job.log(job.error)
        for entry in job.steps.values():
            if entry.get("status") == "running":
                entry.update(status="failed", ended=time.time(), message=job.error)
        if not isinstance(error, Exception):
            raise
        logging.getLogger("studio").error("run %s failed\n%s", job.id, traceback.format_exc())
    finally:
        job.ended = time.time()
        _save(job)


def start(kind: str, issue_key: str, options: dict[str, Any]) -> Job:
    """Start a run in the background; the caller checks LOCK first."""
    job = Job(kind, issue_key, options)
    LIVE[job.id] = job

    async def guarded() -> None:
        async with LOCK:
            await _run(job)

    asyncio.get_running_loop().create_task(guarded(), context=contextvars.copy_context())
    return job
