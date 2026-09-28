"""The single reply, and the properties every one of them must have.

These are the tests Phase 4 names explicitly. Each protects something a reader
of a Jira comment depends on and cannot verify for themselves:

* every reply carries Handled by / Routed to / the signature;
* a router-only outcome names NO child;
* a BOTH route names both, in execution order, in ONE comment;
* the composer works for an agent it has never seen — proving it does not
  branch on ``produced_by``;
* the footer can never match the mention filter, so the system cannot answer
  itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from routing.compose import (
    AMBIGUOUS_OPTIONS,
    LAYOUT,
    ROUTER_DISPLAY_NAME,
    compose,
    handled_by,
    router_only,
)
from shared.contract import AgentResult, CreatedIssue, DuplicateRef, MissingSource, Outcome
from shared.keywords import AGENT_FOOTER, AGENT_SIGNATURE, mentions_trigger


def _result(**over: Any) -> AgentResult:
    base: dict[str, Any] = dict(
        produced_by="JiraTaskCreation",
        display_name="JiraTaskCreation",
        agent_version="4.3.0",
        outcome=Outcome.CREATED,
        headline="Work breakdown created",
        summary="Created 1 Story and 3 Sub-tasks.",
    )
    base.update(over)
    return AgentResult(**base)


def text_of(blocks: list[tuple[str, object]]) -> str:
    """Everything in the reply, flattened, for containment assertions."""
    out: list[str] = []
    for heading, body in blocks:
        out.append(str(heading))
        out.extend(str(x) for x in body) if isinstance(body, list) else out.append(str(body))
    return "\n".join(out)


def headings(blocks: list[tuple[str, object]]) -> list[str]:
    return [h for h, _ in blocks if h]


# --- the envelope, on every reply -------------------------------------------


def test_every_reply_carries_the_envelope() -> None:
    blocks = compose(_result(), routed_to="because.", ran=["JiraTaskCreation"])
    body = text_of(blocks)

    assert body.startswith(f"{AGENT_SIGNATURE} · Work breakdown created")
    assert "Handled by" in headings(blocks)
    assert "Routed to" in headings(blocks)
    assert body.rstrip().endswith(AGENT_FOOTER)


@pytest.mark.parametrize("outcome", list(Outcome))
def test_the_envelope_survives_every_outcome(outcome: Outcome) -> None:
    """Including outcomes this agent never produces — the composer is shared."""
    blocks = compose(_result(outcome=outcome, headline="h"), routed_to="why.", ran=["X"])
    assert "Handled by" in headings(blocks)
    assert "Routed to" in headings(blocks)
    assert text_of(blocks).rstrip().endswith(AGENT_FOOTER)


def test_the_layout_table_covers_every_outcome() -> None:
    """If an outcome is missing, replies for it silently lose their sections."""
    assert set(LAYOUT) == set(Outcome)


# --- attribution -------------------------------------------------------------


def test_a_router_only_outcome_names_no_child() -> None:
    """Nothing ran, so nothing may be named. Claiming a child ran is a lie the
    reader cannot check."""
    blocks = router_only("Which did you mean?", routed_to="unclear.", options=AMBIGUOUS_OPTIONS)
    line = dict(blocks)["Handled by"]

    assert line == ROUTER_DISPLAY_NAME
    assert "→" not in str(line)
    assert "JiraTaskCreation" not in text_of(blocks)


def test_a_single_child_is_named_after_the_arrow() -> None:
    assert handled_by(["Work Breakdown"]) == f"{ROUTER_DISPLAY_NAME} → Work Breakdown"


def test_a_both_route_names_both_in_execution_order_in_one_comment() -> None:
    blocks = compose(
        _result(headline="Reviewed, then broken down"),
        routed_to="both.",
        ran=["Requirement Review", "Work Breakdown"],
    )
    line = str(dict(blocks)["Handled by"])

    assert line == f"{ROUTER_DISPLAY_NAME} → Requirement Review, then Work Breakdown"
    # One comment, not two.
    assert text_of(blocks).count(AGENT_FOOTER) == 1


def test_a_child_that_did_not_run_is_not_named_even_though_it_was_chosen() -> None:
    """A timed-out child still has a name. It must not appear."""
    blocks = compose(
        _result(outcome=Outcome.FAILED, headline="Could not complete this request"),
        routed_to="work breakdown — but that agent could not be reached.",
        ran=[],  # nothing completed
    )
    assert str(dict(blocks)["Handled by"]) == ROUTER_DISPLAY_NAME


# --- substitutability: the composer must not know who produced the result ----


def test_the_composer_works_for_an_agent_it_has_never_seen() -> None:
    """Liskov, asserted directly. A fourth agent is a catalog entry, not a code
    change, and this is the test that proves the composer is not the obstacle."""
    unknown = AgentResult(
        produced_by="SomeFutureAgent",
        display_name="Future Thing",
        outcome=Outcome.REVIEWED,
        headline="Something new happened",
        summary="It did a new kind of work.",
        sources_read=["FL-1 description"],
        sources_missing=[MissingSource(what="FL-1 has no acceptance criteria")],
    )

    blocks = compose(unknown, routed_to="a new route.", ran=["Future Thing"])
    body = text_of(blocks)

    assert "Future Thing" in body
    assert "Something new happened" in body
    assert "FL-1 description" in body
    assert "FL-1 has no acceptance criteria" in body
    assert body.rstrip().endswith(AGENT_FOOTER)


def test_an_unknown_outcome_still_produces_a_usable_reply() -> None:
    """The router owns the comment, so it cannot give up on a shape it does not
    recognise."""
    result = _result(summary="Something happened.", sources_read=["a source"])
    object.__setattr__(result, "outcome", "SOMETHING_NEW")  # bypass the enum

    blocks = compose(result, routed_to="why.", ran=["X"])
    body = text_of(blocks)

    assert "Handled by" in headings(blocks)
    assert "Something happened." in body
    assert "a source" in body
    assert body.rstrip().endswith(AGENT_FOOTER)


# --- the self-trigger guard --------------------------------------------------


def test_the_footer_never_matches_the_mention_filter() -> None:
    """Without this the system answers its own reply, forever."""
    assert not mentions_trigger(AGENT_FOOTER)
    assert not mentions_trigger(AGENT_SIGNATURE)


def test_no_part_of_any_reply_can_retrigger_the_system() -> None:
    """Not just the footer — the whole comment goes back into Jira, and the
    webhook will see all of it."""
    replies = [
        compose(_result(), routed_to="verb.", ran=["JiraTaskCreation"]),
        router_only("Which did you mean?", routed_to="unclear.", options=AMBIGUOUS_OPTIONS),
    ]
    for blocks in replies:
        for heading, body in blocks:
            items = body if isinstance(body, list) else [body]
            for item in [heading, *items]:
                # The ambiguous reply quotes "@Aetherion build" as instructions to
                # the USER. If that re-triggered, the system would loop on its own
                # help text — which is why this is asserted, not assumed.
                if "@Aetherion" in str(item):
                    assert "—" in str(item), f"a bare mention leaked into a reply: {item!r}"


# --- sections ----------------------------------------------------------------


def test_created_issues_are_listed() -> None:
    blocks = compose(
        _result(created=[CreatedIssue(key="FL-121", issue_type="Story", summary="Record a check")]),
        routed_to="verb.",
        ran=["JiraTaskCreation"],
    )
    assert "Created in Jira" in headings(blocks)
    assert "Story FL-121 — Record a check" in text_of(blocks)


def test_duplicates_are_shown_for_the_already_exists_outcome() -> None:
    blocks = compose(
        _result(
            outcome=Outcome.ALREADY_EXISTS,
            headline="This work already exists",
            duplicates=[
                DuplicateRef(proposed_title="Record a check", existing_key="FL-9", score=0.81)
            ],
        ),
        routed_to="verb.",
        ran=["JiraTaskCreation"],
    )
    assert "Work that already exists" in headings(blocks)
    assert "FL-9" in text_of(blocks)


def test_an_empty_section_is_omitted_rather_than_shown_empty() -> None:
    blocks = compose(_result(created=[]), routed_to="verb.", ran=["JiraTaskCreation"])
    assert "Created in Jira" not in headings(blocks)


def test_a_degraded_run_warns_at_the_top_before_anything_else() -> None:
    """It has to be read before the content it is warning about."""
    blocks = compose(
        _result(degraded=True, degraded_reason="AI Gateway unreachable"),
        routed_to="verb.",
        ran=["JiraTaskCreation"],
    )
    rendered = text_of(blocks)
    assert "⚠ Please review the wording" in rendered
    assert "AI Gateway unreachable" in rendered
    assert rendered.index("Please review the wording") < rendered.index("Handled by")


def test_the_notification_line_appears_only_when_something_was_sent() -> None:
    with_email = compose(
        _result(), routed_to="v.", ran=["X"], notification="Emailed lead@example.com."
    )
    assert "Notification" in headings(with_email)

    without = compose(_result(), routed_to="v.", ran=["X"])
    assert "Notification" not in headings(without)


# --- Bug 6: what was read and what was missing must survive every outcome ---


@pytest.mark.parametrize("outcome", list(Outcome))
def test_what_was_read_and_missing_render_on_every_outcome(outcome: Outcome) -> None:
    """FAILED and NOT_A_REQUIREMENT used to drop these sections outright,
    whatever the child populated — a child that read three sources before
    crashing, or that already knew a linked spec was never supplied, had that
    silently dropped from the reply. Every outcome must be able to say it."""
    blocks = compose(
        _result(
            outcome=outcome,
            sources_read=["FL-120 description"],
            sources_missing=[
                MissingSource(
                    what="spec.docx could not be opened",
                    why="corrupt file",
                    what_to_do="please re-attach it",
                )
            ],
            errors=["GatewayError: 503"] if outcome is Outcome.FAILED else [],
        ),
        routed_to="verb.",
        ran=["JiraTaskCreation"],
    )
    rendered = text_of(blocks)
    assert "FL-120 description" in rendered, f"{outcome}: sources_read was dropped"
    assert "spec.docx" in rendered, f"{outcome}: sources_missing was dropped"


def test_layout_names_read_and_missing_for_every_known_outcome() -> None:
    """The table-level guarantee behind the test above: no outcome's layout can
    quietly omit these two sections again without this failing first."""
    for outcome, sections in LAYOUT.items():
        assert "read" in sections, f"{outcome} layout is missing 'read'"
        assert "missing" in sections, f"{outcome} layout is missing 'missing'"


def test_an_answer_renders_under_answer_not_readiness() -> None:
    """BGV-11's explain reply sat under "Readiness", headed "Nothing was created"."""
    result = AgentResult(
        produced_by="JiraTaskCreation",
        display_name="Work Breakdown",
        agent_version="t",
        run_id="r1",
        outcome=Outcome.ANSWERED,
        headline="About BGV-11",
        summary="It checks the candidate's last two employers.",
    )
    blocks = compose(result, routed_to="a question (Layer 1).", ran=["Work Breakdown"])
    headings = [h for h, _ in blocks]
    assert headings[0].endswith("About BGV-11")
    assert ("Answer", "It checks the candidate's last two employers.") in blocks
    assert "Readiness" not in headings


def test_a_partial_duplicate_shows_its_questions() -> None:
    """BGV-32 said "Still uncovered: …" and gave no way forward — the questions
    that said how to build the rest were dropped from this layout."""
    result = AgentResult(
        produced_by="JiraTaskCreation",
        display_name="Work Breakdown",
        agent_version="t",
        run_id="r1",
        outcome=Outcome.ALREADY_EXISTS,
        headline="This work already exists",
        summary="Part of this requirement already exists.",
        questions=["To create only the missing part, add a new comment …"],
    )
    blocks = compose(result, routed_to="build (Layer 1).", ran=["Work Breakdown"])
    assert (
        "What I need to know",
        ["To create only the missing part, add a new comment …"],
    ) in blocks


def test_existing_tickets_with_no_proposed_title_read_as_a_plain_list() -> None:
    result = AgentResult(
        produced_by="JiraTaskCreation",
        display_name="Work Breakdown",
        agent_version="t",
        run_id="r1",
        outcome=Outcome.ALREADY_EXISTS,
        headline="This work already exists",
        summary="Found and reused.",
        duplicates=[DuplicateRef(existing_key="BGV-34", existing_summary="Address History")],
    )
    blocks = compose(result, routed_to="build (Layer 1).", ran=["Work Breakdown"])
    assert ("Work that already exists", ["BGV-34 — Address History"]) in blocks


def _created(questions: list[str]) -> AgentResult:
    return AgentResult(
        produced_by="JiraTaskCreation",
        display_name="Work Breakdown",
        agent_version="t",
        run_id="r1",
        outcome=Outcome.CREATED,
        headline="Work breakdown created",
        summary="1 Story created.",
        questions=questions,
    )


def test_a_build_with_nothing_missing_says_so() -> None:
    """D1 Test A: a complete input — the reply states nothing was missing."""
    blocks = compose(_created([]), routed_to="build (Layer 1).", ran=["Work Breakdown"])
    assert (
        "Details I could not determine — please confirm",
        "Nothing was missing — every detail came from your description.",
    ) in blocks


def test_a_build_names_what_the_input_did_not_say() -> None:
    """D1 Test B: the missing detail is asked, never invented."""
    q = "Raise a request per employer: How many working days before an employer is flagged?"
    blocks = compose(_created([q]), routed_to="build (Layer 1).", ran=["Work Breakdown"])
    assert ("Details I could not determine — please confirm", [q]) in blocks
