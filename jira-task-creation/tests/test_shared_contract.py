"""The result contract every agent returns to the router.

This is the seam the routed architecture rests on: the router composes one reply
from this structure and never branches on which agent produced it. These tests
pin the properties that makes that safe — it survives a round trip through JSON,
it tolerates a child built against a newer shape, and the fields that account
for what was read are always present.

The module is shared (mirrored into every agent), so it is tested once, here.
"""

from __future__ import annotations

import json

import pytest

from src.shared.contract import (
    CONTRACT_VERSION,
    AgentResult,
    CreatedIssue,
    DuplicateRef,
    EmailKind,
    MissingSource,
    Outcome,
)


def _result(**over: object) -> AgentResult:
    base = dict(
        produced_by="JiraTaskCreation",
        display_name="JiraTaskCreation",
        outcome=Outcome.CREATED,
        headline="Work breakdown created",
    )
    base.update(over)
    return AgentResult(**base)  # type: ignore[arg-type]


def test_a_result_carries_its_contract_version() -> None:
    """A router must be able to reject a payload shaped for an older build."""
    assert _result().contract_version == CONTRACT_VERSION


def test_the_accounting_lists_exist_even_when_nothing_was_read() -> None:
    """``sources_read``/``sources_missing`` may be empty but never absent.

    A thin answer with no account of what was read is indistinguishable from a
    badly written ticket, and users blamed the ticket.
    """
    data = _result().to_dict()
    assert data["sources_read"] == []
    assert data["sources_missing"] == []


def test_a_result_survives_a_round_trip_through_json() -> None:
    """It crosses an agent boundary as JSON, so this is the real journey."""
    original = _result(
        outcome=Outcome.CREATED,
        run_id="4a81f7c2",
        agent_version="4.1.0",
        created=[CreatedIssue(key="FL-121", issue_type="Story", summary="Record a check")],
        duplicates=[DuplicateRef(proposed_title="x", existing_key="FL-9", score=0.81)],
        sources_read=["FL-120 description", "3 comments"],
        sources_missing=[
            MissingSource(
                what="spec.xlsx",
                why="password-protected",
                what_to_do="please re-attach it unprotected",
            )
        ],
        email_recommended=True,
        email_kind=EmailKind.CREATED,
    )

    revived = AgentResult.from_dict(json.loads(json.dumps(original.to_dict())))

    assert revived.outcome is Outcome.CREATED
    assert revived.email_kind is EmailKind.CREATED
    assert revived.run_id == "4a81f7c2"
    assert revived.created[0].key == "FL-121"
    assert revived.duplicates[0].existing_key == "FL-9"
    assert revived.sources_read == ["FL-120 description", "3 comments"]
    assert revived.sources_missing[0].what == "spec.xlsx"
    assert revived.to_dict() == original.to_dict()


def test_a_field_from_a_newer_child_does_not_take_the_router_down() -> None:
    """Forward compatibility: a child on a newer build must not break the parent."""
    payload = _result().to_dict()
    payload["something_invented_later"] = {"nested": True}

    revived = AgentResult.from_dict(payload)

    assert revived.produced_by == "JiraTaskCreation"
    assert not hasattr(revived, "something_invented_later")


def test_an_unknown_outcome_is_rejected_rather_than_rendered_blindly() -> None:
    """The router's layout table must be exhaustive, so the set is closed."""
    payload = _result().to_dict()
    payload["outcome"] = "SOMETHING_NEW"
    with pytest.raises(ValueError):
        AgentResult.from_dict(payload)


def test_a_missing_source_reads_as_an_instruction_not_an_apology() -> None:
    """Every entry must tell the user what to do about it."""
    entry = MissingSource(
        what="spec_matrix.xlsx could not be opened",
        why="password-protected",
        what_to_do="please re-attach it unprotected",
    )
    assert entry.as_sentence() == (
        "spec_matrix.xlsx could not be opened (password-protected) — "
        "please re-attach it unprotected"
    )


def test_a_minimal_missing_source_still_reads_as_a_sentence() -> None:
    assert MissingSource(what="FL-130 has no acceptance criteria").as_sentence() == (
        "FL-130 has no acceptance criteria"
    )


def test_the_router_can_compose_from_an_agent_it_has_never_seen() -> None:
    """Liskov: any child is substitutable behind this one type.

    Nothing about reading a result depends on which agent produced it, which is
    what makes a fourth agent a config entry rather than a code change.
    """
    payload = {
        "produced_by": "SomeFutureAgent",
        "display_name": "Future Thing",
        "outcome": "REVIEWED",
        "headline": "Reviewed by something new",
        "sources_read": ["FL-1 description"],
        "sources_missing": [],
    }

    revived = AgentResult.from_dict(payload)

    assert revived.display_name == "Future Thing"
    assert revived.outcome is Outcome.REVIEWED
    assert revived.email_kind is EmailKind.NONE
    assert revived.degraded is False


def test_a_degraded_run_carries_its_reason() -> None:
    """The child reports it; the router renders the warning."""
    revived = AgentResult.from_dict(
        _result(degraded=True, degraded_reason="AI Gateway unreachable").to_dict()
    )
    assert revived.degraded is True
    assert revived.degraded_reason == "AI Gateway unreachable"
