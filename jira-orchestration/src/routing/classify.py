"""Deciding which agent answers a comment, in three layers.

The rule the whole module exists to serve: **never let a model guess intent when
guessing wrong creates Jira issues.**

    Layer 1  explicit verb      deterministic, free, auditable — no model call
                 │ no verb
                 ▼
    Layer 2  classifier         one cheap call, closed intent set, with signals
                 │              beyond the text
                 │ below the agent's threshold
                 ▼
    Layer 3  ask                one comment: "did you mean build or review?"

Layer 3 is not a failure mode, it is the design. One round-trip costs a comment;
a wrong BUILD costs ten stories somebody has to find and delete.

Everything here is **pure**. It runs inside the Temporal workflow, so it may not
read the environment, touch the network or look at the clock. The model call of
Layer 2 happens in an activity; this module only interprets its answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .catalog import CATALOG, AgentSpec, by_intent


@dataclass(frozen=True)
class Decision:
    """Where a comment routes, and — just as importantly — why.

    ``reason`` becomes the "Routed to" line in the reply verbatim. A router
    nobody can debug is worse than no router, and the person reading the comment
    is the first debugger.
    """

    intent: str
    agent: AgentSpec | None
    layer: int
    reason: str
    confidence: float = 1.0
    # Populated only when Layer 3 gives up, so the reply can quote the scores.
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def dispatches(self) -> bool:
        return self.agent is not None


# --------------------------------------------------------------------------
# Layer 1 — an explicit verb wins
# --------------------------------------------------------------------------

_HELP = ("help", "what can you do", "capabilities", "commands")
# 2026-10-15: what has happened on this ticket — answered from its history.
_STATUS = ("status", "progress", "where is it", "what is the status", "what's the status")
# Deliberately NOT bare interrogatives. "who", "why" and "when" match
# "who is the PM of India?", which is chatter about the world, not a question
# about this ticket — and Layer 1 has no way to tell the difference. Only
# phrases that are unambiguously *about the ticket* route deterministically;
# everything else goes to the classifier, which can see the whole sentence.
_QUESTION = (
    "explain",
    "what is this",
    "what does this mean",
    "what is this asking",
    "describe this",
)


# Commands the orchestrator runs as a workflow of its own, across agents and
# into Confluence and GitHub (2026-10-08). Matched by their opening words only —
# never guessed by the model: they write a design page or open a pull request.
WORKFLOW_VERBS: dict[str, tuple[str, ...]] = {
    "TDD": (
        "create tdd",
        "create the tdd",
        "create a tdd",
        "tdd",
        "create technical design",
        "create the technical design",
        "write the tdd",
    ),
    "DESIGN": (
        "explain why",
        "why",
        "why not",
        "why can't",
        "why cant",
        "why can we not",
        "why didn't",
        "what about",
        "could we use",
        "can we use",
        "should we use",
    ),
    "UPDATE_TDD": ("update the tdd", "update tdd", "revise the tdd", "update the design"),
    "CODE": ("code", "start coding", "implement this", "implement the tdd"),
}
WORKFLOW_INTENTS = tuple(WORKFLOW_VERBS)
_WORKFLOW_REASON = {
    "TDD": "a request for the Technical Design Document — checked for readiness, then "
    "written by the Planning agent in Confluence",
    "DESIGN": "a question about the design — answered by the Planning agent",
    "UPDATE_TDD": "a request to change the design — the Planning agent revises the TDD",
    "CODE": "a request to implement the TDD — the Coding agent, after the branches are confirmed",
}


def _workflow_match(text: str) -> tuple[str, str] | None:
    """(intent, the opening words matched) for a workflow command, longest first."""
    best: tuple[str, str] | None = None
    for intent, verbs in WORKFLOW_VERBS.items():
        verb = _leading_verb(text, verbs)
        if verb and (best is None or len(verb) > len(best[1])):
            best = (intent, verb)
    return best


def classify_workflow(body: str, keyword: str = "Aetherion") -> Decision | None:
    """A workflow command, by its opening words (Layer 1), or None."""
    match = _workflow_match(strip_mention(body, keyword))
    if match is None:
        return None
    intent, verb = match
    return Decision(
        intent=intent,
        agent=None,
        layer=1,
        reason=f'{_WORKFLOW_REASON[intent]} — the comment began with "{verb}" (Layer 1).',
    )


def strip_mention(body: str, keyword: str = "Aetherion") -> str:
    """The comment with the mention removed, so the verb is at the front."""
    pattern = re.compile(rf"@?\b{re.escape(keyword)}\b[\s,:\-–—]*", re.IGNORECASE)
    return pattern.sub("", body or "", count=1).strip()


def _leading_verb(text: str, verbs: tuple[str, ...]) -> str | None:
    """The verb this text opens with, if any.

    Anchored at the start on purpose. "Please do not build this yet" contains
    "build" but is not a request to build, and a substring match anywhere in the
    comment routes it straight into creating issues.
    """
    lowered = text.strip().lower()
    for verb in sorted(verbs, key=len, reverse=True):
        if lowered == verb or lowered.startswith(verb + " ") or lowered.startswith(verb + ":"):
            return verb
    return None


def classify_by_verb(body: str, keyword: str = "Aetherion") -> Decision | None:
    """Layer 1. Returns None when there is no explicit verb to act on."""
    text = strip_mention(body, keyword)
    if not text:
        return None

    lowered = text.lower()

    if _leading_verb(text, _HELP) or lowered in _HELP:
        return Decision(
            intent="HELP",
            agent=None,
            layer=1,
            reason="a request for help — listing what this system can do.",
        )

    if _leading_verb(text, _STATUS) or lowered.rstrip("?") in _STATUS:
        return Decision(
            intent="STATUS",
            agent=None,
            layer=1,
            reason="a request for the ticket's status — read from its history.",
        )

    # Longest match across every agent, so a two-word verb is not shadowed by a
    # one-word verb belonging to a different agent.
    # The longest opening wins: "explain why …" is a design question, not "explain".
    workflow = _workflow_match(text)
    if workflow is not None:
        catalog_verb = max(
            (len(v) for spec in CATALOG for v in spec.verbs if _leading_verb(text, (v,))),
            default=0,
        )
        question_verb = len(_leading_verb(text, _QUESTION) or "")
        if len(workflow[1]) >= max(catalog_verb, question_verb):
            return classify_workflow(body, keyword)

    best: tuple[int, AgentSpec, str] | None = None
    for spec in CATALOG:
        verb = _leading_verb(text, spec.verbs)
        if verb and (best is None or len(verb) > best[0]):
            best = (len(verb), spec, verb)

    if best:
        _, spec, verb = best
        return Decision(
            intent=spec.intent,
            agent=spec,
            layer=1,
            reason=f'the comment began with the verb "{verb}" (Layer 1).',
        )

    if _leading_verb(text, _QUESTION):
        # Dispatched, not answered by the router itself (Bug 5). The
        # work-breakdown agent already detects a question on its own trigger
        # comment (its own gate 0b, independent of this classifier) and
        # answers it without creating anything — the router repeating a
        # generic "the ticket's own description is the best source" was a
        # worse answer than the one the agent already knows how to give.
        # ``intent`` stays "QUESTION" for logging and the reply text; the
        # dispatched workflow id and forced payload come from the agent's own
        # BUILD spec, which routes to the same worker regardless.
        build = by_intent("BUILD")
        return Decision(
            intent="QUESTION",
            agent=build,
            layer=1,
            reason=(
                "a question about this ticket — answered without creating anything " "(Layer 1)."
            ),
        )

    return None


# --------------------------------------------------------------------------
# Layer 2 — the classifier's answer, interpreted
# --------------------------------------------------------------------------


def classify_by_model(
    scores: dict[str, float],
    *,
    has_children: bool = False,
) -> Decision:
    """Layer 2 and Layer 3, from the model's scores over the closed intent set.

    ``has_children`` is a signal beyond the text, and a strong one: an issue that
    already has sub-tasks has almost certainly been broken down already, so a
    vague comment on it is far likelier to be asking for a review than for a
    second breakdown.

    The adjustment is applied to the *scores*, not to the thresholds, so the
    reply can still quote what the model actually said.
    """
    adjusted = dict(scores)
    nudge = ""
    if has_children and "REVIEW" in adjusted:
        # Deliberately modest. It tips a close call; it must not manufacture
        # confidence the model did not have.
        adjusted["REVIEW"] = min(1.0, adjusted["REVIEW"] + 0.15)
        nudge = " and this ticket already has sub-tasks"

    if not adjusted:
        return _ambiguous({}, "the classifier returned nothing")

    intent = max(adjusted, key=lambda k: adjusted[k])
    confidence = adjusted[intent]

    if intent == "CHATTER":
        return Decision(
            intent="CHATTER",
            agent=None,
            layer=2,
            reason=(
                "not a work request — the comment asks about the world, not about "
                f"this ticket (Layer 2, chatter, confidence {confidence:.2f})."
            ),
            confidence=confidence,
            scores=adjusted,
        )

    if intent == "QUESTION":
        # Same dispatch as Layer 1's QUESTION branch, and for the same reason
        # (Bug 5): the work-breakdown agent already answers a question about
        # the ticket without creating anything, on its own trigger comment.
        return Decision(
            intent="QUESTION",
            agent=by_intent("BUILD"),
            layer=2,
            reason=(
                "a question about this ticket — answered without creating anything "
                f"(Layer 2, confidence {confidence:.2f})."
            ),
            confidence=confidence,
            scores=adjusted,
        )

    spec = by_intent(intent)
    if spec is None:
        return _ambiguous(adjusted, f"the classifier returned an unknown intent {intent!r}")

    if confidence < spec.confidence:
        # Bug 5: asking is not the only safe answer to uncertainty. When the
        # classifier is torn specifically between BUILD and REVIEW, reviewing
        # is the REVERSIBLE guess: it creates nothing, so a wrong guess costs
        # one comment instead of the round-trip asking costs, and far less
        # than a wrongly-created Epic.
        #
        # "Torn between BUILD and REVIEW" needs a real floor, not just
        # "whichever of these two is bigger" — two near-zero scores (the model
        # has no signal at all) must still ask, not be treated as a confident
        # split between two options. The floor used here is the combined mass
        # of BUILD + REVIEW: at least three-quarters of the classifier's own
        # probability mass landing on this pair, whichever of the two is
        # ahead, is what "uncertain between these two, and nothing else" means
        # — as opposed to "uncertain, full stop" (low combined mass) or
        # "chatter/question actually dominates" (mass sits elsewhere).
        review_spec = by_intent("REVIEW")
        build_score = adjusted.get("BUILD", 0.0)
        review_score = adjusted.get("REVIEW", 0.0)
        if intent in ("BUILD", "REVIEW") and build_score + review_score >= 0.75 and review_spec:
            return Decision(
                intent="REVIEW",
                agent=review_spec,
                layer=2,
                reason=(
                    f"build scored {adjusted.get('BUILD', 0.0):.2f} and review scored "
                    f"{review_score:.2f} — neither reached its own threshold, so the "
                    "reversible action ran rather than asking (Layer 2)."
                ),
                confidence=review_score,
                scores=adjusted,
            )
        return _ambiguous(
            adjusted,
            (
                f"{spec.display_name.lower()} scored {confidence:.2f}, below the "
                f"{spec.confidence:.2f} needed"
                + (" because it writes to Jira" if spec.writes_to_jira else "")
            ),
        )

    return Decision(
        intent=intent,
        agent=spec,
        layer=2,
        reason=(
            f"no explicit verb, but the comment reads as a {spec.display_name.lower()} "
            f"request{nudge} (Layer 2, confidence {confidence:.2f})."
        ),
        confidence=confidence,
        scores=adjusted,
    )


def _ambiguous(scores: dict[str, float], why: str) -> Decision:
    """Layer 3 — ask rather than guess. Dispatches to nobody."""
    quoted = ", ".join(f"{k.lower()} {v:.2f}" for k, v in sorted(scores.items()))
    detail = f"the classifier scored {quoted}" if quoted else why
    return Decision(
        intent="AMBIGUOUS",
        agent=None,
        layer=3,
        reason=(
            f"unclear — {detail}, so nothing was run rather than guessing (Layer 3)."
            if quoted
            else f"unclear — {why}, so nothing was run rather than guessing (Layer 3)."
        ),
        confidence=max(scores.values()) if scores else 0.0,
        scores=scores,
    )


def classify(
    body: str,
    *,
    scores: dict[str, float] | None = None,
    has_children: bool = False,
    keyword: str = "Aetherion",
) -> Decision:
    """The whole ladder. Layer 1 first, and it is free.

    ``scores`` is what the classifier activity returned, or None when it was not
    called (because a verb settled it) or could not be reached.
    """
    verdict = classify_by_verb(body, keyword)
    if verdict is not None:
        return verdict

    if scores is None:
        # The model was unreachable. Asking is the only safe answer: routing a
        # comment to BUILD on no information is exactly the failure this whole
        # module is arranged to prevent.
        return Decision(
            intent="AMBIGUOUS",
            agent=None,
            layer=3,
            reason=(
                "unclear — there was no explicit verb and the classifier could not "
                "be reached, so nothing was run rather than guessing (Layer 3)."
            ),
            confidence=0.0,
        )

    return classify_by_model(scores, has_children=has_children)
