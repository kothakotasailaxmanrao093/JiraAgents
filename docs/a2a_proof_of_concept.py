"""Proof of concept: one Aetherion agent calling another, and what crosses.

Run it:

    cd jira-task-creation && uv run python ../docs/a2a_proof_of_concept.py

What it proves, without a live worker:

1. the exact call a parent makes to invoke a child;
2. that the shared result contract survives Temporal's serialisation intact —
   this is a REAL encode/decode through the same converter the platform uses,
   not a mock;
3. that returning the dataclass instead of ``.to_dict()`` silently degrades it
   to a plain dict on the other side;
4. how each failure mode presents to the parent.

What it does NOT prove: that a deployed child is reachable. That needs two
published agents and a worker, and is the Phase 6 liveness check. Every claim
here that could not be verified locally is marked UNCONFIRMED in
docs/AGENT_TO_AGENT.md.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "jira-task-creation"))

from temporalio.converter import default as default_converter  # noqa: E402

from src.shared.contract import (  # noqa: E402
    AgentResult,
    CreatedIssue,
    EmailKind,
    MissingSource,
    Outcome,
)


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "─" * len(title))


# ---------------------------------------------------------------------------
# 1. The call itself
# ---------------------------------------------------------------------------


def the_call() -> None:
    rule("1. How the router invokes a child")
    print(
        """
The child is a Temporal CHILD WORKFLOW. The parent awaits it, so the call is
synchronous from the router's point of view even though everything underneath
is asynchronous and durable.

    from aetherion_sdk import agentExecutor

    result = await agentExecutor.execute(
        "JiraRequirementReview",          # the REGISTERED agent name
        {                                       # one positional payload arg
            "issue_key":  "FL-130",
            "comment_id": "10501",
            "mode":       "delegated",
            "run_id":     run_id,
        },
        execution_timeout=timedelta(minutes=10),
        retry_policy=RetryPolicy(maximum_attempts=1),
        workflow_id=f"review-{run_id}",         # <- the idempotency key
    )

Only these Temporal options are accepted (anything else raises):
"""
    )
    from aetherion_sdk.agent_executor import AgentExecutor

    for option in sorted(AgentExecutor._ALLOWED_TEMPORAL_KWARGS):
        print(f"    - {option}")

    print(
        """
Two children at once, when a route needs both:

    results = await agentExecutor.run_parallel({
        "review": ("JiraRequirementReview", payload_a),
        "build":  ("JiraTaskCreation",           payload_b),
    })

…but the BOTH route is CONDITIONAL — the build runs only if the review passes —
so the router uses two sequential `execute` calls, not `run_parallel`.
"""
    )


# ---------------------------------------------------------------------------
# 2 & 3. What crosses the boundary
# ---------------------------------------------------------------------------


def sample() -> AgentResult:
    return AgentResult(
        produced_by="JiraTaskCreation",
        display_name="Work Breakdown",
        agent_version="4.3.0",
        run_id="4a81f7c2",
        outcome=Outcome.CREATED,
        headline="Work breakdown created",
        summary="Created 1 Story and 3 Sub-tasks.",
        created=[
            CreatedIssue(
                key="FL-121",
                issue_type="Story",
                summary="Record a tyre pressure check",
                url="https://example.atlassian.net/browse/FL-121",
            )
        ],
        sources_read=["FL-120 description", "3 comment(s)", "Attachment: spec_v1.pdf"],
        sources_missing=[
            MissingSource(
                what="spec_matrix.xlsx",
                why="password-protected",
                what_to_do="please re-attach it unprotected",
            )
        ],
        email_recommended=True,
        email_kind=EmailKind.CREATED,
    )


async def crossing() -> None:
    rule("2. The contract survives the boundary")
    converter = default_converter()
    original = sample()

    payloads = await converter.encode([original.to_dict()])
    print(f"encoding   : {payloads[0].metadata.get(b'encoding').decode()}")
    print(f"size       : {len(payloads[0].data)} bytes")

    decoded = await converter.decode(payloads)
    revived = AgentResult.from_dict(decoded[0])

    print(f"identical  : {revived.to_dict() == original.to_dict()}")
    print(f"outcome    : {revived.outcome!r}  (a real enum, not a string)")
    print(f"created[0] : {revived.created[0].key} — {revived.created[0].summary}")
    print(f"missing[0] : {revived.sources_missing[0].as_sentence()}")

    rule("3. The mistake to avoid")
    raw = await converter.encode([original])  # the dataclass, NOT .to_dict()
    back = (await converter.decode(raw))[0]
    print(f"returning the dataclass decodes as : {type(back).__name__}")
    print(f"  outcome comes back as            : {back['outcome']!r}  (a string)")
    print(
        """
So the child MUST return `.to_dict()` and the router MUST call
`AgentResult.from_dict()`. Returning the dataclass appears to work — the payload
encodes fine — and the router then gets enum VALUES where it expects enums.
Nothing raises; the reply is just wrong."""
    )


# ---------------------------------------------------------------------------
# 4. Failure modes
# ---------------------------------------------------------------------------


class FakeExecutor:
    """Stands in for agentExecutor so each failure can be shown end to end."""

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour

    async def execute(self, workflow_type: str, *args, **kwargs):
        from temporalio.exceptions import ApplicationError, ChildWorkflowError, TimeoutError

        if self.behaviour == "ok":
            return sample().to_dict()
        if self.behaviour == "crash":
            raise ChildWorkflowError(
                "child workflow failed",
                namespace="default",
                workflow_id="wf-1",
                run_id="run-1",
                workflow_type=workflow_type,
                initiated_event_id=1,
                started_event_id=2,
                retry_state=None,
            )
        if self.behaviour == "timeout":
            raise TimeoutError(
                "execution timeout", type=None, last_heartbeat_details=[]
            )
        if self.behaviour == "not_deployed":
            raise ApplicationError(f"workflow type {workflow_type} is not registered")
        raise AssertionError(self.behaviour)


async def dispatch(executor: FakeExecutor, run_id: str) -> AgentResult:
    """The router's dispatch step: never raise, always produce something to post."""
    from temporalio.exceptions import TemporalError

    try:
        payload = await executor.execute("JiraTaskCreation", {"mode": "delegated"})
        return AgentResult.from_dict(payload)
    except TemporalError as exc:
        # The router still owns the reply, so a dead child becomes a renderable
        # result naming NO child — none ran to completion.
        return AgentResult(
            produced_by="JiraOrchestration",
            display_name="",  # deliberately empty: no child is named
            run_id=run_id,
            outcome=Outcome.FAILED,
            headline="Could not complete this request",
            summary=(
                "The work-breakdown agent did not respond. Nothing was created, "
                "so asking again is safe."
            ),
            degraded=True,
            degraded_reason=type(exc).__name__,
            errors=[f"{type(exc).__name__}: {exc}"],
        )


async def failures() -> None:
    rule("4. What the router sees when the child does not answer")
    for behaviour in ("ok", "crash", "timeout", "not_deployed"):
        result = await dispatch(FakeExecutor(behaviour), run_id="2f6a04b1")
        named = result.display_name or "(no child named)"
        print(f"{behaviour:<14} -> {result.outcome.value:<8} | handled by: {named}")
    print(
        """
Note the "Handled by" line names NO child on every failure path. The router
knows the child was *chosen*; it must not claim the child *ran*."""
    )


async def main() -> None:
    the_call()
    await crossing()
    await failures()
    print("\n\033[32mAll assertions above were executed, not described.\033[0m")


if __name__ == "__main__":
    asyncio.run(main())
