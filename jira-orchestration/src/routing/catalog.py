"""Which agents exist, what they answer to, and how much certainty each needs.

**This is a table, not a chain of ``if``.** Adding a fourth agent is one entry
here plus one result-contract implementation on its side — no router code
changes. That is the Open/Closed principle doing actual work: the dispatcher,
the classifier and the composer all read this and none of them names an agent.

Two fields carry the weight:

``verbs``
    The words that route deterministically, with no model call at all. An
    explicit verb is free, instant and auditable, and it is checked first
    precisely so that the common case never depends on a guess.

``confidence``
    How sure the classifier must be before this agent runs *without* a verb.
    **The thresholds are deliberately asymmetric.** Work breakdown writes to
    Jira, so a wrong guess creates issues someone has to delete; it needs high
    confidence. Review's only write is a comment — and over the webhook path it
    is draft-only — so it can run on less. Being wrong is cheap for one and
    expensive for the other, and the numbers say so.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentSpec:
    """One agent the router can dispatch to."""

    # The name the platform registered. This is what agentExecutor.execute takes,
    # and it is NOT the folder name or the display name.
    agent_name: str

    # Stable, human-readable, and never the class name. A person reading a Jira
    # comment sees this, so it must stay recognisable across versions.
    display_name: str

    # The intent this agent serves, as the classifier reports it.
    intent: str

    # Words that route here with no model call (Layer 1).
    verbs: tuple[str, ...] = ()

    # Minimum classifier confidence to run without a verb (Layer 2).
    confidence: float = 0.75

    # Whether dispatching this agent can change Jira. Drives how cautious the
    # threshold is, and what the reply says about retrying being safe.
    writes_to_jira: bool = False

    # How long to wait before giving up on it.
    timeout_minutes: int = 10

    # Payload keys forced on every delegated call, whatever the caller asked.
    forced_payload: dict[str, object] = field(default_factory=dict)

    # The task queue THIS agent's own worker actually polls.
    #
    # Without an explicit queue, dispatching a child workflow makes Temporal
    # default to the CALLER's queue — the router's own — which only has a
    # worker that knows how to run JiraOrchestration. The child workflow then
    # fails immediately: "Workflow class <X> is not registered on this
    # worker". This happened for real on the first live dispatch to
    # JiraRequirementReview.
    #
    # Deliberately NOT read from the environment here: this module is
    # imported by the workflow, and CATALOG is built at import time — reading
    # os.environ at that point is the same class of bug as F4 (a workflow
    # reading configuration directly). The real value is resolved in the
    # ingress ACTIVITY (see tools.py / settings.py) and merged in by the
    # dispatcher as data, via ``AgentSpec.with_task_queue``.
    task_queue: str | None = None

    def with_task_queue(self, queue: str | None) -> AgentSpec:
        """A copy of this spec with the queue filled in from activity data.

        Pure — no I/O, no environment. dataclasses.replace would work too;
        this is named for what it is doing at the call site.
        """
        if not queue:
            return self
        return dataclasses.replace(self, task_queue=queue)


# The catalog. Order matters only for verb matching, which prefers the longest
# match — so "build" and "breakdown" cannot shadow each other by accident.
CATALOG: tuple[AgentSpec, ...] = (
    AgentSpec(
        agent_name="JiraTaskCreation",
        display_name="Work Breakdown",
        intent="BUILD",
        verbs=("build", "break down", "breakdown", "decompose", "create tickets"),
        # High: this one writes. A wrong guess creates issues a person must
        # find and delete, and the ticket history keeps the scar.
        confidence=0.80,
        writes_to_jira=True,
        # See the note on RetryPolicy in router_flow.py's dispatch options: a
        # child that never gets picked up by any worker (the F22 misconfigured-
        # queue case) sits "Running" for the FULL execution_timeout, not the
        # ~14s the bounded retry_policy budgets — that policy only bounds
        # retries of a child that starts and fails, not one that never starts
        # at all. Lowered from 15 so that failure mode surfaces in minutes,
        # not a quarter of an hour, while real processing still has headroom.
        # Raised 5 -> 7 for the D1 self-review gate: one more model call, two
        # when it regenerates (+10-80 s on the largest builds).
        timeout_minutes=7,
        forced_payload={"mode": "delegated"},
    ),
    AgentSpec(
        agent_name="JiraRequirementReview",
        display_name="Requirement Review",
        intent="REVIEW",
        verbs=("review", "analyse", "analyze", "gaps", "assess", "check", "is this ready"),
        # Lower: the only write is a comment the router posts anyway, and the
        # delegated path forces draft-only. Being wrong costs one comment.
        confidence=0.60,
        writes_to_jira=False,
        timeout_minutes=4,  # see the note on the BUILD entry above
        forced_payload={"mode": "delegated", "post_to_jira": False},
    ),
)

# Intents the router answers itself. They dispatch to no agent, so they are not
# in the catalog — but the classifier must still be able to return them, and the
# set has to be closed or the composer cannot be exhaustive.
ROUTER_INTENTS = ("QUESTION", "CHATTER")

INTENTS = tuple(spec.intent for spec in CATALOG) + ROUTER_INTENTS


def by_intent(intent: str) -> AgentSpec | None:
    """The agent that serves this intent, or None when the router answers."""
    for spec in CATALOG:
        if spec.intent == intent:
            return spec
    return None


def by_agent_name(name: str) -> AgentSpec | None:
    for spec in CATALOG:
        if spec.agent_name == name:
            return spec
    return None


def display_name_for(agent_name: str) -> str:
    """What a person should see for this agent.

    Falls back to the raw name rather than raising: a reply naming an unfamiliar
    agent is far better than no reply at all, and the composer must work for an
    agent it has never seen.
    """
    spec = by_agent_name(agent_name)
    return spec.display_name if spec else agent_name
