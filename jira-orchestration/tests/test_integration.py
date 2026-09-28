"""The router against what the children really return.

Every other router test uses a contract I wrote. These use contracts the
children actually produced, captured from their own translation code by
`integration/capture_contracts.py` — each child run in its own virtualenv,
because two of them define a top-level `agent` package and importing both into
one process shadows one with the other.

That makes these the tests that would catch a child changing its output. If one
does, `capture_contracts.py --check` fails and these tests are updated with it,
rather than passing forever against a shape nothing produces any more.

**The guarantees Phase 5 has to prove, each with a test below:**

1. exactly one reply per user comment, on every path;
2. the envelope and attribution correct on every path;
3. at most one email;
4. never email an invalid request — survives routing;
5. duplicate protection survives the extra hop;
6. labels and answered-comment tracking work across the chain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.router_flow import run_router
from shared.contract import AgentResult, Outcome
from shared.keywords import AGENT_FOOTER, AGENT_SIGNATURE, mentions_trigger

CONTRACTS = Path(__file__).resolve().parents[2] / "integration" / "contracts"


def load(name: str) -> dict[str, Any]:
    path = CONTRACTS / f"{name}.json"
    if not path.exists():
        pytest.skip(f"{path} missing — run: python integration/capture_contracts.py")
    return json.loads(path.read_text())


ALL_CONTRACTS = sorted(p.stem for p in CONTRACTS.glob("*.json")) if CONTRACTS.exists() else []


# --- the harness -------------------------------------------------------------


class Tools:
    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args))
        answer = self.answers.get(name)
        return answer if answer is not None else {}

    def count(self, name: str) -> int:
        return [n for n, _ in self.calls].count(name)

    def args(self, name: str) -> tuple:
        for called, args in self.calls:
            if called == name:
                return args
        raise AssertionError(f"{name} was never called")

    def reply_text(self) -> str:
        out: list[str] = []
        for heading, body in self.args("post_reply")[1]:
            out.append(str(heading))
            if isinstance(body, list):
                out.extend(str(x) for x in body)
            else:
                out.append(str(body))
        return "\n".join(out)


class Child:
    def __init__(self, contract: Any = None, raises: Exception | None = None) -> None:
        self.contract = contract
        self.raises = raises
        self.calls: list[tuple[str, dict, dict]] = []

    async def dispatch(self, name: str, payload: dict, **options: Any) -> Any:
        self.calls.append((name, payload, options))
        if self.raises:
            raise self.raises
        return self.contract


def gate(**over: Any) -> dict[str, Any]:
    base = {
        "outcome": "proceed",
        "run_id": "4a81f7c2",
        "issue_key": "FL-120",
        "comment_id": "10501",
        "comment_body": "@Aetherion build a tyre pressure check",
        "issue_summary": "Depot operations",
        "has_children": False,
        "idempotency_key": "comment_created:10501",
        "trigger_keyword": "Aetherion",
        "processed_label": "aetherion-processed",
        "existing_labels": [],
        "read_only": False,
    }
    base.update(over)
    return base


PAYLOAD = {"issue_key": "FL-120", "comment_id": "10501", "webhookEvent": "comment_created"}
VERB = {"build": "@Aetherion build a thing", "review": "@Aetherion review this"}


def route_for(name: str) -> str:
    return VERB["review"] if name.startswith("review") else VERB["build"]


async def run_with(contract_name: str) -> tuple[Tools, Child, dict]:
    tools = Tools(
        ingress_check=gate(comment_body=route_for(contract_name)),
        post_reply={
            "posted": True,
            "comment_id": "10502",
            "recorded": True,
            "labels": ["aetherion-processed"],
        },
    )
    child = Child(contract=load(contract_name))
    result = await run_router(PAYLOAD, tools.execute, child.dispatch)
    return tools, child, result


# --- 1. exactly one reply, for every real contract ---------------------------


@pytest.mark.parametrize("name", ALL_CONTRACTS)
async def test_every_real_contract_produces_exactly_one_reply(name: str) -> None:
    tools, _, _ = await run_with(name)
    assert tools.count("post_reply") == 1


@pytest.mark.parametrize("name", ALL_CONTRACTS)
async def test_every_real_contract_is_parseable_by_the_router(name: str) -> None:
    """If a child's output stops fitting the contract, the router degrades to a
    "returned something unreadable" reply. That must never be the normal case."""
    revived = AgentResult.from_dict(load(name))
    assert isinstance(revived.outcome, Outcome)
    assert revived.produced_by
    assert isinstance(revived.sources_read, list)
    assert isinstance(revived.sources_missing, list)


# --- 2. the envelope and attribution, on every path --------------------------


@pytest.mark.parametrize("name", ALL_CONTRACTS)
async def test_every_real_reply_carries_the_full_envelope(name: str) -> None:
    tools, _, _ = await run_with(name)
    text = tools.reply_text()

    assert text.startswith(AGENT_SIGNATURE)
    assert "Handled by" in text
    assert "Routed to" in text
    # Bug 7: the run_id must NEVER reach a rendered comment. It travels through
    # dispatch (asserted separately) and the log, never the reply text.
    assert "4a81f7c2" not in text
    assert text.rstrip().endswith(AGENT_FOOTER)


@pytest.mark.parametrize("name", ALL_CONTRACTS)
async def test_every_real_reply_names_the_child_that_ran(name: str) -> None:
    """The HUMAN display name, never the raw registered agent id (Bug 2)."""
    tools, _, _ = await run_with(name)
    expected = "Requirement Review" if name.startswith("review") else "Work Breakdown"
    text = tools.reply_text()
    assert f"Jira Orchestration → {expected}" in text
    # The raw id must be structurally incapable of reaching a comment.
    assert "JiraTaskCreation" not in text
    assert "JiraRequirementReview" not in text


@pytest.mark.parametrize("name", ALL_CONTRACTS)
async def test_no_real_reply_can_retrigger_the_system(name: str) -> None:
    """The reply goes back into Jira, and the webhook sees all of it."""
    tools, _, _ = await run_with(name)
    for line in tools.reply_text().splitlines():
        if mentions_trigger(line):
            # Only the instructional options may contain a mention, and they are
            # written so a reader sees them as a command to type, not as a call.
            assert "—" in line, f"a bare mention leaked into a reply: {line!r}"


async def test_a_real_degraded_contract_warns_before_the_content() -> None:
    tools, _, _ = await run_with("build_degraded")
    text = tools.reply_text()
    assert "Please review the wording" in text
    assert text.index("Please review the wording") < text.index("Handled by")


# --- 3 & 4. email: at most one, and never for an invalid request -------------


async def test_a_real_invalid_request_never_mentions_an_email() -> None:
    """The child recorded `invalid_request` as the email it would have sent.
    The router must refuse it — and this is the REAL child's output saying so."""
    contract = load("build_not_a_requirement")
    assert contract["outcome"] == "NOT_A_REQUIREMENT"

    tools, _, _ = await run_with("build_not_a_requirement")
    assert "Notification" not in tools.reply_text()


def test_the_child_itself_refuses_to_recommend_an_email_for_an_invalid_request() -> None:
    """Belt and braces, asserted against the real captured output: the rule is
    enforced in the child AND in the router, so one forgetting cannot break it."""
    contract = load("build_not_a_requirement")
    assert contract["email_recommended"] is False
    assert contract["email_kind"] == "none"


# The claims a reply could make about email. D1a: none may appear unless an
# email was actually sent — and on the routed path none is sent yet.
_EMAIL_CLAIMS = ("An email was recommended", "Emailed", "email was sent", "Notification")


@pytest.mark.parametrize("name", ALL_CONTRACTS)
async def test_no_real_reply_claims_an_email_that_was_not_sent(name: str) -> None:
    """Asserted on the rendered comment, for every real child outcome. "An email
    was recommended for this outcome" appeared on every created run while
    nothing in the router could send one."""
    tools, _, _ = await run_with(name)  # the fake send_outcome_email reports nothing sent
    text = tools.reply_text()
    for claim in _EMAIL_CLAIMS:
        assert claim not in text, f"{name}: reply claims {claim!r} but no email was sent"


@pytest.mark.parametrize("name", ALL_CONTRACTS)
async def test_only_actionable_real_outcomes_try_to_email(name: str) -> None:
    """Against every real captured child contract: created, needs-info,
    already-exists and failed email once; reviews and invalid requests never."""
    outcome = load(name)["outcome"]
    tools, _, _ = await run_with(name)
    expected = 1 if outcome in ("CREATED", "NEEDS_INFO", "ALREADY_EXISTS", "FAILED") else 0
    assert tools.count("send_outcome_email") == expected, f"{name} ({outcome})"


# --- 5. duplicate protection survives the extra hop -------------------------


async def test_the_already_exists_outcome_names_what_it_matched() -> None:
    """The child's duplicate detection runs before any write, and the router
    renders it — so the hop cannot lose it."""
    tools, _, _ = await run_with("build_already_exists")
    text = tools.reply_text()
    assert "Work that already exists" in text
    assert "FL-9" in text
    assert "0.81" in text


async def test_a_redelivery_never_reaches_the_child_at_all() -> None:
    """Layer 1 of three. The child's own idempotency label is the third."""
    tools = Tools(
        ingress_check={"outcome": "ignored", "ignored": "DUPLICATE", "run_id": "4a81f7c2"}
    )
    child = Child(contract=load("build_created"))

    result = await run_router(PAYLOAD, tools.execute, child.dispatch)

    assert child.calls == []
    assert tools.count("post_reply") == 0
    assert result["run_id"] == "4a81f7c2"


async def test_the_child_workflow_id_prevents_a_replay_running_it_twice() -> None:
    """Layer 2 of three: Temporal refuses a duplicate workflow id."""
    _, child, _ = await run_with("build_created")
    _, _, options = child.calls[0]
    assert options["workflow_id"] == "build-4a81f7c2"


# --- 6. labels and answered-comment tracking across the chain ---------------


@pytest.mark.parametrize("name", ALL_CONTRACTS)
async def test_the_label_and_answered_record_are_passed_on_every_path(name: str) -> None:
    """Both belong to the router now. Every answered path must carry them, or a
    redelivery would be answered a second time."""
    tools, _, _ = await run_with(name)
    args = tools.args("post_reply")
    issue_key, _blocks, run_id, comment_id, idem_key, label, _thread_id = args

    assert issue_key == "FL-120"
    assert run_id == "4a81f7c2"
    assert comment_id == "10501"
    assert idem_key == "comment_created:10501"
    assert label == "aetherion-processed"


async def test_the_children_never_stamp_a_label_themselves() -> None:
    """Delegated mode forces this. Asserted on the payload the router sends."""
    _, child, _ = await run_with("build_created")
    _, payload, _ = child.calls[0]
    assert payload["mode"] == "delegated"


# --- the seam itself ---------------------------------------------------------


@pytest.mark.parametrize("name", ALL_CONTRACTS)
def test_every_real_contract_survives_a_json_round_trip(name: str) -> None:
    """It crosses the agent boundary as JSON, so this is the real journey."""
    original = load(name)
    revived = AgentResult.from_dict(json.loads(json.dumps(original)))
    assert revived.to_dict() == original


def test_both_children_are_represented() -> None:
    """A guard on the fixtures themselves: if one child stopped being captured,
    these tests would quietly only cover the other."""
    produced_by = {load(n)["produced_by"] for n in ALL_CONTRACTS}
    assert produced_by == {"JiraTaskCreation", "JiraRequirementReview"}


def test_the_captured_contracts_cover_every_outcome_the_children_can_produce() -> None:
    outcomes = {load(n)["outcome"] for n in ALL_CONTRACTS}
    assert outcomes == {
        "CREATED",
        "ALREADY_EXISTS",
        "NEEDS_INFO",
        "NOT_A_REQUIREMENT",
        "REVIEWED",
        "FAILED",
    }
