"""The steps after a build, in order — Review, Planning, Coding (2026-10-08).

SDK-free like ``router_flow``: the executors are passed in, so the sequence is
tested without Temporal. The rules (what continues, what Planning is told, how
the reply reads) are in ``routing/pipeline.py``.

Two ways in share this sequence (2026-10-08): a Jira comment (the request is a
ticket; Review reads it, Planning's input is read from Jira, the design is
attached to it) and the orchestrator's form (the request is the description
and files; Review reads Task Creation's PDF, nothing touches Jira). What differs
is passed in: ``review_input``, ``planning_source`` and ``keep_design``.

Each step is reported as it happens (``progress``): on a ticket the one reply
is edited after every step, so a person sees "✅ Review — ⏳ Technical design"
instead of an unexplained wait. A step that fails ends the chain with the reason on the
ticket; nothing after it runs. Coding is the exception: when it raises no pull
request, its reasons go back to Planning for a revised design, and it tries
again (``coding_attempts`` in all).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio.common import RetryPolicy

from routing import pipeline as rules
from routing.catalog import AgentSpec
from routing.pipeline import PipelineConfig, Step
from shared.contract import AgentResult, Outcome

logger = logging.getLogger(__name__)

Execute = Callable[..., Awaitable[dict]]
Dispatch = Callable[..., Awaitable[dict]]
# progress(steps, ran): show where the chain is, on the ticket.
Progress = Callable[[list[Step], list[str]], Awaitable[None]]
# planning_source(build, review) -> (Planning's issue_text, or "" and why)
PlanningSource = Callable[[AgentResult, "AgentResult | None"], Awaitable[tuple[str, str]]]
# keep_design(file_key) -> one line saying where the design can be opened
KeepDesign = Callable[[str], Awaitable[str]]

_TOOL_TIMEOUT = timedelta(minutes=2)
PLANNING_NAME = "Planning"
CODING_NAME = "Coding"


@dataclass
class Chain:
    """One request's run of the steps after its build."""

    config: PipelineConfig
    review_spec: AgentSpec | None
    execute: Execute
    dispatch: Dispatch
    progress: Progress
    issue_key: str  # "" for a request from the form
    run_id: str
    # What Review reads: {"issue_key", "comment_id"} or {"document_file_keys", …}
    review_input: dict[str, Any]
    planning_source: PlanningSource
    keep_design: KeepDesign
    # Filled as the chain runs: every design written, every PR raised.
    designs: list[str] = field(default_factory=list)
    pull_requests: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, build: AgentResult, ran: list[str]) -> tuple[list[Step], list[str]]:
        """Every configured step after ``build``; (the steps as reported, who ran)."""
        steps = [Step(name) for name in self.config.steps]
        by_name = {step.name: step for step in steps}
        ran = list(ran)

        async def show() -> None:
            await self.progress(steps, ran)

        review: AgentResult | None = None
        if "review" in by_name:
            review = await self._review(by_name["review"], show, ran)
            if self._paused(review, by_name):
                return steps, ran

        if "plan" not in by_name:
            return steps, ran
        plan = by_name["plan"]
        if not self.config.repos:
            _skip_from(steps, "plan", "not run — no repositories to design against")
            return steps, ran

        plan.status = "running"
        plan.detail = "Planning is reading the tickets and the code…"
        await show()
        issue_text, problem = await self._planning_text(build, review)
        if not issue_text:
            plan.status = "failed"
            plan.detail = f"the tickets could not be read for Planning: {problem}"
            _skip_from(steps, "code", "not run — there is no design")
            return steps, ran

        tdd = await self._plan(plan, issue_text, attempt=1)
        if not tdd:
            _skip_from(steps, "code", "not run — there is no design")
            return steps, ran
        ran.append(PLANNING_NAME)

        if "code" in by_name:
            await self._code(by_name["code"], plan, issue_text, tdd, show, ran)
        return steps, ran

    # --- review -------------------------------------------------------------------

    async def _review(self, step: Step, show: Callable, ran: list[str]) -> AgentResult | None:
        spec = self.review_spec
        step.status = "running"
        step.detail = "Requirement Review is looking for gaps in the request…"
        await show()
        if spec is None:
            step.status, step.detail = ("skipped", "not run — no review agent is configured")
            return None
        payload = {**self.review_input, "run_id": self.run_id, **spec.forced_payload}
        out, problem = await self._call(
            spec.agent_name,
            payload,
            workflow_id=f"pipeline-review-{self.run_id}",
            minutes=spec.timeout_minutes,
            queue=spec.task_queue or "",
        )
        if out is None:
            step.status, step.detail = ("failed", f"{spec.display_name} did not respond: {problem}")
            return None
        try:
            review = AgentResult.from_dict(out)
        except Exception as exc:  # noqa: BLE001 — reported, the chain goes on without it
            step.status, step.detail = "failed", f"unreadable review: {exc}"
            return None
        ran.append(spec.display_name)
        if review.outcome is not Outcome.REVIEWED:
            step.status = "failed"
            step.detail = review.summary or review.headline
            return review
        step.status = "done"
        points = rules.review_points(review)
        score = f"readiness {review.readiness_score}/5, " if review.readiness_score else ""
        step.detail = f"{score}{len(points)} open point(s)"
        step.lines = (
            ["Handed to Planning to design for:"] + points
            if points
            else ["No open points — nothing was missing."]
        )
        return review

    def _paused(self, review: AgentResult | None, by_name: dict[str, Step]) -> bool:
        """Planning waits when the review judged the request too unclear to design."""
        minimum = self.config.plan_min_readiness
        if not minimum or "plan" not in by_name:
            return False
        scored = review is not None and review.outcome is Outcome.REVIEWED
        score = review.readiness_score if scored else 0
        if scored and (score == 0 or score >= minimum):
            return False
        reason = (
            f"paused — the review scored the request {score}/5, below the {minimum}/5 "
            "needed to design it. Answer the review's points, then ask again."
            if scored
            # With a minimum set, an unscored request does not pass the gate.
            else f"paused — the review gave no score, and {minimum}/5 is needed to design it."
        )
        _skip_from(list(by_name.values()), "plan", reason)
        return True

    # --- planning -----------------------------------------------------------------

    async def _planning_text(
        self, build: AgentResult, review: AgentResult | None
    ) -> tuple[str, str]:
        """(Planning's input text, or "" and why)."""
        return await self.planning_source(build, review)

    async def _plan(self, step: Step, issue_text: str, attempt: int) -> str:
        """One Planning run; the stored design's key, or "" with the step marked failed."""
        payload = {
            "issue_text": issue_text,
            "repos": self.config.repos,
            # Not read by Planning today; there for it to use.
            "issue_key": self.issue_key,
            "run_id": self.run_id,
        }
        out, problem = await self._call(
            self.config.planning_agent,
            payload,
            workflow_id=f"pipeline-plan-{self.run_id}-{attempt}",
            minutes=self.config.planning_timeout_minutes,
            queue=self.config.planning_queue,
        )
        tdd = rules.tdd_key(out) if out and out.get("status") == "success" else ""
        if not tdd:
            step.status = "failed"
            step.detail = f"Planning wrote no design: {problem or rules.planning_problem(out)}"
            return ""
        step.status = "done"
        step.detail = "written" + (" (revised)" if attempt > 1 else "")
        step.lines = [await self.keep_design(tdd)]
        self.designs.append(tdd)
        return tdd

    # --- coding -------------------------------------------------------------------

    async def _code(
        self, step: Step, plan: Step, issue_text: str, tdd: str, show: Callable, ran: list[str]
    ) -> None:
        attempts = max(1, self.config.coding_attempts)
        for attempt in range(1, attempts + 1):
            step.status = "running"
            step.detail = (
                "Coding is implementing the design…"
                if attempt == 1
                else f"Coding is implementing the revised design (attempt {attempt} of {attempts})…"
            )
            await show()
            out, problem = await self._call(
                self.config.coding_agent,
                {
                    "uploaded_files": [tdd],
                    "mode": "delegated",
                    # The only repositories Coding may write to (2026-10-14).
                    "repos": self.config.repos,
                    # Not read by Coding today; there for it to use.
                    "issue_key": self.issue_key,
                    "run_id": self.run_id,
                },
                workflow_id=f"pipeline-code-{self.run_id}-{attempt}",
                minutes=self.config.coding_timeout_minutes,
                queue=self.config.coding_queue,
            )
            if out is None:
                # Not reached at all: a new design would not help.
                step.status, step.detail = ("failed", f"Coding did not respond: {problem}")
                return
            if CODING_NAME not in ran:
                ran.append(CODING_NAME)
            prs = rules.pr_lines(out)
            self.pull_requests = rules.pull_requests(out)
            if prs:
                step.status = "done"
                step.detail = (
                    "raised — review and merge it in GitHub; nothing is merged automatically"
                )
                step.lines = prs
                return
            reasons = rules.coding_problems(out)
            if rules.is_access_problem(out):
                # A new design cannot fix a refused token: say what to change.
                step.status = "failed"
                step.detail = "no pull request — " + rules.ACCESS_HINT
                step.lines = reasons
                return
            if not rules.is_design_problem(out):
                # A timeout or a crash is not the design's fault: report it.
                step.status = "failed"
                step.detail = "no pull request — Coding did not finish"
                step.lines = reasons
                return
            if attempt == attempts:
                step.status = "failed"
                step.detail = f"no pull request after {attempts} attempt(s)"
                step.lines = reasons
                return
            # Back to Planning with the reasons, then try again.
            plan.status = "running"
            plan.detail = "revising the design — Coding could not implement it: " + "; ".join(
                reasons[:3]
            )
            await show()
            issue_text = rules.replan_text(issue_text, out)
            tdd = await self._plan(plan, issue_text, attempt=attempt + 1)
            if not tdd:
                step.status, step.detail = "failed", "not run again — no revised design"
                step.lines = reasons
                return

    # --- one agent call -----------------------------------------------------------

    async def _call(
        self, agent: str, payload: dict[str, Any], *, workflow_id: str, minutes: int, queue: str
    ) -> tuple[dict[str, Any] | None, str]:
        """Start one agent and wait for it; (its result, or None and why)."""
        options: dict[str, Any] = {
            "workflow_id": workflow_id,
            "execution_timeout": timedelta(minutes=minutes),
            # A failed step is reported, never retried blind: Planning and Coding
            # write (a design, a pull request), and Coding has its own retry.
            "retry_policy": RetryPolicy(maximum_attempts=1),
        }
        if queue:
            options["task_queue"] = queue
        try:
            out = await self.dispatch(agent, payload, **options)
        except Exception as exc:  # noqa: BLE001 — every failure reaches the ticket
            detail = f"{type(exc).__name__}: {str(exc)[:300]}"
            logger.error(f"run {self.run_id}: {agent} failed: {detail}")
            return None, detail
        if isinstance(out, list):
            out = out[0] if out else {}
        return (out if isinstance(out, dict) else {}), ""


# --- what differs for a request that is a Jira ticket ---------------------------------


def jira_planning_source(execute: Execute, issue_key: str) -> PlanningSource:
    """Planning's input read from Jira: the ticket and every ticket the build made."""

    async def source(build: AgentResult, review: AgentResult | None) -> tuple[str, str]:
        answer = await _safe(
            execute,
            "planning_input",
            issue_key,
            rules.ticket_keys(build),
            list(build.proposed),
            review.to_dict() if review else None,
            start_to_close_timeout=_TOOL_TIMEOUT,
        )
        return str(answer.get("issue_text") or ""), str(answer.get("error") or "no text")

    return source


def attach_to_ticket(execute: Execute, issue_key: str, run_id: str) -> KeepDesign:
    """The design put on the ticket, where a person opens it."""

    async def keep(tdd: str) -> str:
        attached = await _safe(
            execute, "attach_tdd", issue_key, tdd, run_id, start_to_close_timeout=_TOOL_TIMEOUT
        )
        if attached.get("attached"):
            return f"Attached to {issue_key}: {attached['filename']}"
        problem = f" — not attached: {attached['error']}" if attached.get("error") else ""
        return f"Kept in storage as {tdd}{problem}"

    return keep


def _skip_from(steps: list[Step], first: str, reason: str) -> None:
    """Mark ``first`` and every step after it as not run, saying why."""
    names = [s.name for s in steps]
    if first not in names:
        return
    for step in steps[names.index(first) :]:
        if step.status in ("waiting", "running"):
            step.status, step.detail = "skipped", reason


async def _safe(execute: Execute, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """One tool; an exception becomes ``{"error": …}``, never a crash."""
    try:
        result = await execute(name, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the caller reports it
        logger.error(f"{name} raised: {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}
    return result if isinstance(result, dict) else {}
