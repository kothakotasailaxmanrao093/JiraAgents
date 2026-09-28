"""Which agent answers a comment, and why.

The rule the whole module serves: **never let a model guess intent when guessing
wrong creates Jira issues.** So these tests care most about the cases where the
router should refuse to decide.
"""

from __future__ import annotations

import pytest

from routing.catalog import CATALOG, by_intent
from routing.classify import classify, classify_by_verb, strip_mention

# --- Layer 1: an explicit verb wins, with no model call ----------------------


@pytest.mark.parametrize(
    "body, expected",
    [
        ("@Aetherion build a tyre pressure check", "BUILD"),
        ("@Aetherion break down this requirement", "BUILD"),
        ("@Aetherion breakdown please", "BUILD"),
        ("@Aetherion decompose this", "BUILD"),
        ("@Aetherion review this and tell me if it is ready", "REVIEW"),
        ("@Aetherion analyse the gaps", "REVIEW"),
        ("@Aetherion analyze it", "REVIEW"),
        ("@Aetherion assess this ticket", "REVIEW"),
    ],
)
def test_an_explicit_verb_routes_with_no_model_call(body: str, expected: str) -> None:
    decision = classify(body, scores=None)  # scores=None proves no model was used
    assert decision.intent == expected
    assert decision.layer == 1
    assert decision.agent is by_intent(expected)


def test_the_reason_names_the_verb_and_the_layer() -> None:
    """The reason becomes the "Routed to" line verbatim. A router nobody can
    debug is worse than no router."""
    decision = classify("@Aetherion build a thing")
    assert 'verb "build"' in decision.reason
    assert "Layer 1" in decision.reason


def test_help_is_answered_by_the_router_itself() -> None:
    decision = classify("@Aetherion help")
    assert decision.intent == "HELP"
    assert decision.agent is None


@pytest.mark.parametrize("body", ["@Aetherion explain this", "@Aetherion what is this"])
def test_a_question_is_dispatched_to_work_breakdown(body: str) -> None:
    """Bug 5: the router used to answer a question generically itself. The
    work-breakdown agent already detects a question on its own trigger comment
    and answers it without creating anything, so the router dispatches there
    instead of repeating a worse, generic answer."""
    decision = classify(body)
    assert decision.intent == "QUESTION"
    assert decision.agent is by_intent("BUILD")
    assert decision.dispatches


def test_a_verb_buried_mid_sentence_does_not_route() -> None:
    """ "Please do not build this yet" contains "build" and is the opposite of a
    request to build. Matching anywhere in the comment would create issues."""
    assert classify_by_verb("@Aetherion please do not build this yet") is None


def test_a_longer_verb_is_not_shadowed_by_a_shorter_one() -> None:
    decision = classify("@Aetherion break down the requirement")
    assert decision.intent == "BUILD"
    assert "break down" in decision.reason


def test_the_mention_is_stripped_however_it_is_punctuated() -> None:
    for body in ("@Aetherion build x", "Aetherion: build x", "@Aetherion, build x"):
        assert strip_mention(body).lower().startswith("build")


# --- Layer 2: the classifier, with signals beyond the text -------------------


def test_a_confident_classification_routes() -> None:
    decision = classify("this needs breaking up", scores={"BUILD": 0.91, "REVIEW": 0.05})
    assert decision.intent == "BUILD"
    assert decision.layer == 2
    assert "0.91" in decision.reason


def test_an_issue_that_already_has_children_leans_towards_review() -> None:
    """A ticket with sub-tasks has almost certainly been broken down already, so
    a vague comment on it is likelier to be asking for a review.

    Both cases land on REVIEW here (Bug 5's BUILD/REVIEW tie-break applies
    even without the nudge, since 0.55/0.52 together clear the combined-mass
    floor) — the nudge's effect is visible in the *reason*, not just the
    outcome: only the nudged run can point to the sub-tasks as why.
    """
    scores = {"BUILD": 0.55, "REVIEW": 0.52}

    without = classify("what do you think of this?", scores=scores)
    with_children = classify("what do you think of this?", scores=scores, has_children=True)

    assert without.intent == "REVIEW" and without.layer == 2
    assert "already has sub-tasks" not in without.reason
    assert with_children.intent == "REVIEW"
    assert "already has sub-tasks" in with_children.reason


def test_the_nudge_cannot_manufacture_confidence_the_model_did_not_have() -> None:
    """It tips a close call. It must not rescue a hopeless one."""
    decision = classify("hmm", scores={"BUILD": 0.20, "REVIEW": 0.21}, has_children=True)
    assert decision.layer == 3


def test_chatter_is_refused_without_dispatching() -> None:
    decision = classify("who is the PM of India?", scores={"CHATTER": 0.96})
    assert decision.intent == "CHATTER"
    assert decision.agent is None
    assert "not a work request" in decision.reason


# --- the asymmetry, which is the point --------------------------------------


def test_build_needs_more_confidence_than_review() -> None:
    """Being wrong is expensive for one and cheap for the other."""
    build = by_intent("BUILD")
    review = by_intent("REVIEW")
    assert build is not None and review is not None
    assert build.confidence > review.confidence
    assert build.writes_to_jira and not review.writes_to_jira


def test_a_score_that_would_route_review_does_not_route_build() -> None:
    """The same 0.65 means "go ahead" for a comment and "ask first" for issues."""
    assert classify("x", scores={"REVIEW": 0.65}).intent == "REVIEW"
    assert classify("x", scores={"BUILD": 0.65}).layer == 3


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.agent_name)
def test_every_agent_that_writes_needs_high_confidence(spec) -> None:
    if spec.writes_to_jira:
        assert (
            spec.confidence >= 0.75
        ), f"{spec.agent_name} writes to Jira on a guess of {spec.confidence}"


# --- Layer 3: ask rather than guess ------------------------------------------


def test_a_close_call_between_build_and_review_defaults_to_review() -> None:
    """Bug 5: this exact split (build 0.41, review 0.44) used to be the
    textbook "ask rather than guess" example — but asking is not the only
    safe answer when the two live options are BUILD and REVIEW specifically.
    Reviewing creates nothing, so the reversible action runs instead."""
    decision = classify("@Aetherion have a look at this", scores={"BUILD": 0.41, "REVIEW": 0.44})
    assert decision.layer == 2
    assert decision.intent == "REVIEW"
    assert decision.agent is by_intent("REVIEW")
    assert "build scored 0.41" in decision.reason
    assert "review scored 0.44" in decision.reason


def test_low_confidence_on_both_build_and_review_still_asks() -> None:
    """The BUILD/REVIEW tie-break needs a real floor (Bug 5): two low scores
    are "the model has no signal", not "a confident split between two
    options", so this must still ask rather than guess."""
    decision = classify("hmm, ok", scores={"BUILD": 0.30, "REVIEW": 0.25})
    assert decision.layer == 3
    assert decision.agent is None
    assert "nothing was run rather than guessing" in decision.reason


def test_the_reason_quotes_the_scores_so_the_decision_can_be_audited() -> None:
    decision = classify("have a look", scores={"BUILD": 0.30, "REVIEW": 0.25})
    assert "build 0.30" in decision.reason
    assert "review 0.25" in decision.reason


def test_an_unreachable_classifier_asks_rather_than_defaulting_to_anything() -> None:
    """Routing to BUILD on no information is exactly the failure this prevents."""
    decision = classify("some vague prose with no verb", scores=None)
    assert decision.layer == 3
    assert decision.agent is None
    assert "could not be reached" in decision.reason


def test_an_unknown_intent_from_the_model_is_refused() -> None:
    """The intent set is closed. A model that invents one must not dispatch."""
    decision = classify("x", scores={"SOMETHING_NEW": 0.99})
    assert decision.agent is None
    assert decision.layer == 3


def test_an_empty_score_set_asks() -> None:
    assert classify("x", scores={}).layer == 3


def test_nothing_dispatches_without_an_agent() -> None:
    """The property the dispatcher relies on."""
    for decision in (
        classify("@Aetherion help"),
        classify("x", scores={"CHATTER": 0.9}),
        classify("x", scores={"BUILD": 0.1}),
        classify("x", scores=None),
    ):
        assert not decision.dispatches
