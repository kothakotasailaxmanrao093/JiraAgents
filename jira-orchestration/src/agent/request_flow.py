"""A request started on the orchestrator's own form (2026-10-08).

The person types a description, attaches files and presses Run. Nothing is
written to Jira — it is only read, by Task Creation, to see whether the work
already exists:

    Task Creation   description + files  ->  the breakdown PDF (kept in storage)
    Review          the PDF + the files  ->  a readiness score, and its open points
    gate            below the minimum (4/5 by default) stops here, with the points
    Planning        the request, the breakdown, the points + the code graph -> TDD
    Coding          the TDD -> a pull request; when it cannot, back to Planning
    a person        reviews and merges the PR; the merge refreshes Planning's graph
                    (the GitHub check, router_flow.run_github_poll)

The steps after the breakdown are the same ``Chain`` a Jira comment runs; only
what Review reads, what Planning is given and where the design goes differ.

SDK-free like ``router_flow``: the executors are passed in.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta
from typing import Any

from temporalio.common import RetryPolicy

from agent.pipeline_flow import Chain, Dispatch, Execute, KeepDesign, PlanningSource
from routing import pipeline as rules
from routing.catalog import by_intent
from routing.pipeline import PipelineConfig, Step
from shared.contract import AgentResult, Outcome

logger = logging.getLogger(__name__)

_TOOL_TIMEOUT = timedelta(minutes=2)
# Every step after the breakdown, when ORCH_BUILD_PIPELINE names none: a form
# request exists to run them.
ALL_STEPS = ("review", "plan", "code")
UPLOAD_FIELDS = ("supporting_files", "uploaded_files")
NEXT_STEP = (
    "Review the pull request in GitHub and merge it. The merge reaches Planning through "
    "the GitHub check, which refreshes the code graph for the next design."
)


def upload_keys(payload: dict[str, Any]) -> list[str]:
    """The storage keys of the files picked on the form, in order, once each."""
    keys: list[str] = []
    for name in UPLOAD_FIELDS:
        value = payload.get(name)
        items = value if isinstance(value, list) else str(value or "").split(",")
        keys += [str(i).strip() for i in items if str(i or "").strip()]
    return list(dict.fromkeys(keys))


def title_of(requirement: str) -> str:
    """The request's name: its first line, short."""
    first = next((line.strip() for line in requirement.splitlines() if line.strip()), "")
    return first if len(first) <= 80 else first[:79].rstrip() + "…"


async def run_request(payload: dict[str, Any], execute: Execute, dispatch: Dispatch) -> dict:
    """One form request, start to finish; the run's result is the whole story."""
    requirement = str(payload.get("requirement") or "").strip()
    files = upload_keys(payload)
    title = title_of(requirement)
    setup = await _safe(
        execute,
        "request_settings",
        str(payload.get("project_key") or ""),
        start_to_close_timeout=_TOOL_TIMEOUT,
    )
    run_id = str(setup.get("run_id") or "")
    build = Step("build")
    if not requirement:
        return _finish(run_id, title, [_fail(build, "Enter the description of the work.")])
    problem = str(setup.get("problem") or setup.get("error") or "")
    if problem:
        return _finish(run_id, title, [_fail(build, problem)])

    project = str(setup.get("project_key") or "")
    config = _config(setup, payload)
    overrides = setup.get("task_queue_overrides") or {}
    logger.info(f"request {run_id}: {title!r} in {project}, {len(files)} file(s)")

    result, breakdown = await _breakdown(
        build, dispatch, overrides, run_id, requirement, project, files
    )
    if not breakdown:
        return _finish(run_id, title, [build], build=result)

    review_spec = by_intent("REVIEW")
    if review_spec is not None:
        review_spec = review_spec.with_task_queue(overrides.get(review_spec.agent_name))

    async def progress(steps: list[Step], ran: list[str]) -> None:
        logger.info(
            f"request {run_id}: " + " · ".join(f"{s.label} {s.status}" for s in [build, *steps])
        )

    chain = Chain(
        config=config,
        review_spec=review_spec,
        execute=execute,
        dispatch=dispatch,
        progress=progress,
        issue_key="",
        run_id=run_id,
        review_input={"document_file_keys": [breakdown, *files], "document_title": title},
        planning_source=form_planning_source(requirement, files),
        keep_design=local_copy(execute, run_id),
    )
    builder = by_intent("BUILD")
    steps, ran = await chain.run(result, [builder.display_name if builder else "Work Breakdown"])
    return _finish(
        run_id,
        title,
        [build, *steps],
        build=result,
        breakdown=breakdown,
        designs=chain.designs,
        pull_requests=chain.pull_requests,
        ran=ran,
    )


def _config(setup: dict[str, Any], payload: dict[str, Any]) -> PipelineConfig:
    """The steps and the gate: the form's minimum score wins over the setting."""
    config = PipelineConfig.from_dict(setup.get("pipeline"))
    steps = config.steps or ALL_STEPS
    try:
        minimum = int(payload.get("min_review_score") or setup.get("min_readiness") or 0)
    except (TypeError, ValueError):
        minimum = int(setup.get("min_readiness") or 0)
    return dataclasses.replace(config, steps=tuple(steps), plan_min_readiness=max(0, minimum))


async def _breakdown(
    step: Step,
    dispatch: Dispatch,
    overrides: dict[str, str],
    run_id: str,
    requirement: str,
    project: str,
    files: list[str],
) -> tuple[AgentResult | None, str]:
    """Task Creation makes the breakdown PDF; (its result, the PDF's storage key or "")."""
    spec = by_intent("BUILD")
    if spec is None:
        _fail(step, "No Task Creation agent is configured.")
        return None, ""
    spec = spec.with_task_queue(overrides.get(spec.agent_name))
    payload = {
        "requirement": requirement,
        "project_key": project,
        "supporting_files": files,
        # A PDF, never tickets: nothing is written to Jira.
        "output": "pdf_by_email",
        "keep_document": True,
        "run_id": run_id,
        **spec.forced_payload,
    }
    options: dict[str, Any] = {
        "workflow_id": f"request-build-{run_id}",
        "execution_timeout": timedelta(minutes=spec.timeout_minutes),
        "retry_policy": RetryPolicy(maximum_attempts=1),
    }
    if spec.task_queue:
        options["task_queue"] = spec.task_queue
    step.status, step.detail = "running", "Task Creation is breaking the request down…"
    try:
        raw = await dispatch(spec.agent_name, payload, **options)
        result = AgentResult.from_dict(raw if isinstance(raw, dict) else (raw or [{}])[0])
    except Exception as exc:  # noqa: BLE001 — every failure is in the result
        _fail(step, f"Task Creation did not respond: {type(exc).__name__}: {str(exc)[:300]}")
        return None, ""

    if result.outcome is not Outcome.PLANNED:
        _fail(step, result.summary or result.headline)
        step.lines = [*result.questions, *result.errors]
        return result, ""
    key = next((d.get("file_key", "") for d in result.documents if d.get("file_key")), "")
    if not key:
        _fail(step, "the breakdown PDF was made but could not be kept for Review")
        return result, ""
    step.status = "done"
    # "Work breakdown emailed as a PDF — …" under the heading "Work breakdown".
    step.detail = (result.headline or "ready as a PDF").removeprefix("Work breakdown ").strip()
    step.lines = list(result.proposed)
    return result, key


def form_planning_source(requirement: str, files: list[str]) -> PlanningSource:
    """Planning's input from the form itself: no ticket is read."""
    names = [key.rsplit("/", 1)[-1] for key in files]

    async def source(build: AgentResult, review: AgentResult | None) -> tuple[str, str]:
        description = requirement
        if names:
            description += "\n\nFiles attached to the request: " + ", ".join(names)
        root = {"key": "Request", "summary": title_of(requirement), "description": description}
        text = rules.planning_text(
            root, [], list(build.proposed), review.to_dict() if review else None
        )
        return text, ""

    return source


def local_copy(execute: Execute, run_id: str) -> KeepDesign:
    """Where a design can be opened: a copy on the laptop, else its storage key."""

    async def keep(tdd: str) -> str:
        saved = await _safe(
            execute, "keep_design_copy", tdd, "tdd", run_id, start_to_close_timeout=_TOOL_TIMEOUT
        )
        if saved.get("saved"):
            return f"Saved on this laptop: {saved['path']}"
        return f"Kept in storage as {tdd} (download it from the run's results)"

    return keep


def _fail(step: Step, reason: str) -> Step:
    step.status, step.detail = "failed", reason
    return step


def _finish(
    run_id: str,
    title: str,
    steps: list[Step],
    *,
    build: AgentResult | None = None,
    breakdown: str = "",
    designs: list[str] | None = None,
    pull_requests: list[dict[str, Any]] | None = None,
    ran: list[str] | None = None,
) -> dict[str, Any]:
    """The run's result: each step, the documents to download and the PRs to open."""
    results: list[dict[str, Any]] = []
    if breakdown:
        results.append(_download("Work breakdown (PDF)", breakdown, "Download the breakdown"))
    for n, tdd in enumerate(designs or [], start=1):
        label = "Technical design (PDF)" + (f" — version {n}" if len(designs or []) > 1 else "")
        results.append(_download(label, tdd, "Download the TDD"))
    for pr in pull_requests or []:
        results.append(
            {
                "title": f"Pull request: {pr.get('service', '')}",
                "link": f"[{pr['pr_url']}]({pr['pr_url']})",
                "label": f"Open the PR for {pr.get('service', '')}",
                "extension": "url",
            }
        )
    status = _status(steps)
    outcome = {
        "status": status,
        "run_id": run_id,
        "request": title,
        "handled_by": "Jira Orchestration → " + ", then ".join(ran) if ran else "",
        "steps": [f"{rules.MARK.get(s.status, '◻')} {s.label} — {s.detail}" for s in steps],
        "details": rules.summary(steps),
        "results": results,
    }
    if build is not None and build.questions:
        outcome["questions"] = list(build.questions)
    if pull_requests:
        outcome["next"] = NEXT_STEP
    logger.info(
        f"request {run_id}: {status} — " + " · ".join(f"{s.label} {s.status}" for s in steps)
    )
    return outcome


def _status(steps: list[Step]) -> str:
    """done (a PR was raised), stopped (the gate held it), or failed."""
    last = steps[-1] if steps else None
    if last is not None and last.name == "code" and last.status == "done":
        return "done"
    if any(s.status == "failed" for s in steps):
        return "failed"
    if any(s.status == "skipped" and "paused" in s.detail for s in steps):
        return "stopped"
    return "done" if steps and all(s.status == "done" for s in steps) else "stopped"


def _download(title: str, file_key: str, label: str) -> dict[str, Any]:
    return {
        "type": "s3_download_link",
        "title": title,
        "file_key": file_key,
        "label": label,
        "extension": "pdf",
    }


async def _safe(execute: Execute, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result = await execute(name, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the result says what went wrong
        logger.error(f"{name} raised: {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}
    return result if isinstance(result, dict) else {}
