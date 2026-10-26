"""Planning's memory in the TDD commands, and the bug flow (2026-10-26).

* create TDD asks Planning whether the requirement changed (mode "facts"): a
  reworded story keeps its TDD; only a changed fact makes a new version, and
  every published version is recorded (mode "record").
* why / why not / update the TDD go through Planning's check (mode "discuss"
  with a kind): only a valid change is applied; "update the TDD anyway" is a
  person insisting.
* A bug: Review judges the report, Planning looks for the cause in the code,
  and the merged result is kept as the ticket's review.

Planning is faked to the contract; ORCH_MEMORY off keeps the old behaviour.
"""

from __future__ import annotations

from typing import Any

from agent.command_flow import run_command
from agent.router_flow import run_router
from routing.commands import DISCUSSION_AGREED, design_kind, parse_update
from routing.memory import change_lines, normalised_hash, story_text
from tests.fake_tools import Tools
from tests.test_bug_routing import _bug_gate
from tests.test_commands import (
    DOCUMENT,
    PAGE,
    PUBLISHED,
    SETTINGS,
    STORY,
    Agents,
    command,
    text,
    tools,
)
from tests.test_router_flow import PAYLOAD, reviewed_contract

ON = {**SETTINGS, "memory": True}
KNOWN = {
    "page_id": "9001",
    "url": PAGE["url"],
    "title": PAGE["title"],
    "tdd_version": "v1.0",
    "fingerprint": "old-fingerprint",
}
STORY_TEXT = {"ok": True, "text": "BGV-105: Daily transfer limit\nLimit 2,00,000 INR", "hash": "h1"}
FACTS = [
    {
        "slot": "daily_limit",
        "kind": "limit",
        "statement": "2,00,000 a day",
        "values": ["2,00,000 INR"],
    }
]
DECISION = {"id": "money-integer-paise", "title": "Money as paise", "choice": "long paise"}


def facts(verdict: str, **over: Any) -> dict[str, Any]:
    return {
        "status": "success",
        "mode": "facts",
        "verdict": verdict,
        "facts": FACTS,
        "facts_hash": "fh",
        "current": {"tdd_version": "v1.0", "page_id": "9001"},
        "changes": [],
        "reason": "Every fact means the same.",
        **over,
    }


DESIGNED = {"status": "success", "results": [], "document": {**DOCUMENT, "decisions": [DECISION]}}
RECORDED = {"status": "success", "mode": "record", "stored": {"facts": 1, "decisions": 1}}


def memory_tools(**over: Any) -> Tools:
    return tools(
        story_context=over.pop("story_context", {**STORY, "tdd": KNOWN}),
        story_text=over.pop("story_text", STORY_TEXT),
        publish_tdd=over.pop("publish_tdd", {**PUBLISHED, "tdd_version": "v1.1"}),
        **over,
    )


def planning_payloads(agents: Agents) -> list[dict]:
    return [p for a, p in agents.calls if a == "planning_agent"]


# --- pure rules ----------------------------------------------------------------------


def test_design_kind_tells_a_proposal_from_a_question() -> None:
    assert design_kind("@Aetherion why not integer paise?") == "proposal"
    assert design_kind("@Aetherion can we use BigDecimal?") == "proposal"
    assert design_kind("@Aetherion explain why a Clock") == "question"
    assert design_kind("@Aetherion why a Clock?") == "question"


def test_parse_update_reads_the_change_and_the_override() -> None:
    assert parse_update("@Aetherion update the TDD: log each rejection") == (
        "update",
        "log each rejection",
    )
    assert parse_update("@Aetherion update the TDD anyway: use double") == (
        "update_override",
        "use double",
    )
    assert parse_update("@Aetherion update the TDD") == ("update", DISCUSSION_AGREED)


def test_the_story_text_leaves_out_nothing_a_person_wrote() -> None:
    out = story_text(
        {"key": "BTS-25", "summary": "Daily limit", "description": "Limit 2,00,000 INR"},
        [{"key": "BTS-26", "summary": "Tests", "description": "Edge at midnight"}],
        ["Also reject reversed transfers"],
    )
    assert "BTS-25: Daily limit" in out and "Edge at midnight" in out
    assert "- Also reject reversed transfers" in out


def test_a_normalised_hash_ignores_case_spacing_and_punctuation() -> None:
    assert normalised_hash("Limit: 2,00,000  INR.") == normalised_hash("limit 2 00 000 inr")
    assert normalised_hash("limit 2,00,000") != normalised_hash("limit 2,50,000")


def test_changes_are_named_with_old_and_new() -> None:
    lines = change_lines(
        [{"slot": "daily_limit", "old": "2,00,000 INR", "new": "2,50,000 INR", "why": "number"}]
    )
    assert lines == ["daily limit: 2,00,000 INR → 2,50,000 INR (number)"]


# --- create TDD ---------------------------------------------------------------------


async def test_a_reworded_story_keeps_its_tdd_without_a_new_run() -> None:
    t = memory_tools()
    agents = Agents(planning_agent=[facts("reworded")])
    result = await run_command(
        command("TDD", "@Aetherion create TDD", t, agents, settings=ON, fingerprint="new-fp")
    )
    sent = planning_payloads(agents)
    assert len(sent) == 1 and sent[0]["mode"] == "facts"
    assert sent[0]["story_text"] == STORY_TEXT["text"] and sent[0]["issue_key"] == "BGV-105"
    assert "JiraRequirementReview" not in agents.names()  # no review, no design
    assert t.count("publish_tdd") == 0
    assert t.args("remember_fingerprint") == ("BGV-105", "new-fp")
    body = text(result.blocks)
    assert "unchanged" in body and "was reworded, but its requirement is the same" in body


async def test_unsure_keeps_the_tdd_and_says_how_to_change_it() -> None:
    t = memory_tools()
    agents = Agents(planning_agent=[facts("unsure", reason="Rule 2 may mean more.")])
    result = await run_command(
        command("TDD", "@Aetherion create TDD", t, agents, settings=ON, fingerprint="fp")
    )
    body = text(result.blocks)
    assert "unchanged" in body and "Rule 2 may mean more." in body
    assert '"@Aetherion update the TDD: <what changed>"' in body
    assert t.count("publish_tdd") == 0


async def test_a_changed_fact_revises_and_names_the_change_then_records() -> None:
    change = {"slot": "daily_limit", "old": "2,00,000 INR", "new": "2,50,000 INR", "why": "number"}
    t = memory_tools()
    agents = Agents(planning_agent=[facts("changed", changes=[change]), DESIGNED, RECORDED])
    result = await run_command(
        command("TDD", "@Aetherion create TDD", t, agents, settings=ON, fingerprint="fp")
    )
    sent = planning_payloads(agents)
    assert [p.get("mode") for p in sent] == ["facts", None, "record"]
    revision = sent[1]["issue_text"]
    assert "This is a REVISION" in revision
    assert "daily limit: 2,00,000 INR → 2,50,000 INR" in revision
    assert sent[1]["project_key"] == "BGV" and sent[1]["issue_key"] == "BGV-105"
    record = sent[2]
    assert record["tdd_version"] == "v1.1" and record["page_id"] == "9001"
    assert record["facts"] == FACTS and record["facts_hash"] == "fh"
    assert record["story_text_hash"] == "h1" and record["decisions"] == [DECISION]
    assert "daily limit: 2,00,000 INR → 2,50,000 INR" in text(result.blocks)


async def test_a_first_tdd_is_written_and_recorded() -> None:
    t = memory_tools(story_context=STORY, publish_tdd={**PUBLISHED, "tdd_version": "v1.0"})
    agents = Agents(planning_agent=[facts("new", current=None), DESIGNED, RECORDED])
    await run_command(
        command("TDD", "@Aetherion create TDD", t, agents, settings=ON, fingerprint="fp")
    )
    sent = planning_payloads(agents)
    assert [p.get("mode") for p in sent] == ["facts", None, "record"]
    assert "REVISION" not in sent[1]["issue_text"]
    assert sent[2]["tdd_version"] == "v1.0"


async def test_same_verdict_without_a_readable_page_writes_it_again() -> None:
    t = memory_tools(read_tdd={"ok": False, "error": "404"})
    agents = Agents(planning_agent=[facts("same"), DESIGNED, RECORDED])
    await run_command(
        command("TDD", "@Aetherion create TDD", t, agents, settings=ON, fingerprint="fp")
    )
    assert t.count("publish_tdd") == 1


async def test_a_failed_facts_check_never_stops_the_tdd() -> None:
    t = memory_tools()
    agents = Agents(planning_agent=[{"status": "error", "message": "boom"}, DESIGNED, RECORDED])
    await run_command(
        command("TDD", "@Aetherion create TDD", t, agents, settings=ON, fingerprint="fp")
    )
    assert t.count("publish_tdd") == 1


async def test_memory_off_asks_planning_nothing_new() -> None:
    t = memory_tools()
    agents = Agents(planning_agent=[DESIGNED])
    await run_command(command("TDD", "@Aetherion create TDD", t, agents, fingerprint="fp"))
    modes = [p.get("mode") for p in planning_payloads(agents)]
    assert "facts" not in modes and "record" not in modes
    assert t.count("story_text") == 0


# --- why / why not / update the TDD ---------------------------------------------------


def discussed(verdict: str, **over: Any) -> dict[str, Any]:
    return {
        "status": "success",
        "mode": "discuss",
        "answer": "Integer paise avoids rounding at the limit.",
        "verdict": verdict,
        "change_recommended": verdict in ("apply", "partial"),
        "apply": [],
        "refused": [],
        "supersedes": [],
        "new_decisions": [],
        **over,
    }


async def ask(body: str, *answers: dict, intent: str = "DESIGN", **settings: Any):
    t = memory_tools()
    agents = Agents(planning_agent=list(answers))
    result = await run_command(
        command(intent, body, t, agents, page_id="9001", settings={**ON, **settings})
    )
    return result, t, agents


async def test_a_question_is_answered_only() -> None:
    result, t, agents = await ask("@Aetherion why a Clock?", discussed("answer_only"))
    sent = planning_payloads(agents)[0]
    assert sent["mode"] == "discuss" and sent["kind"] == "question"
    assert t.count("publish_tdd") == 0
    assert "Kept as it is" not in text(result.blocks)


async def test_an_invalid_proposal_is_refused_with_its_reason() -> None:
    refused = [{"change": "use double", "reason": "breaks the agreed paise decision"}]
    result, t, _ = await ask("@Aetherion why not double?", discussed("refuse", refused=refused))
    body = text(result.blocks)
    assert "Not changed" in body and "use double — breaks the agreed paise decision" in body
    assert '"@Aetherion update the TDD anyway: <the change>"' in body
    assert t.count("publish_tdd") == 0


async def test_a_change_already_in_the_tdd_makes_no_version() -> None:
    result, t, _ = await ask("@Aetherion why not a lock?", discussed("already_present"))
    assert "Already in the TDD" in text(result.blocks)
    assert t.count("publish_tdd") == 0


async def test_a_valid_proposal_is_applied_and_recorded_with_its_decision() -> None:
    agreed = discussed(
        "apply",
        apply=["Sum money as long paise"],
        supersedes=["money-double"],
        new_decisions=[DECISION],
    )
    result, t, agents = await ask("@Aetherion why not integer paise?", agreed, DESIGNED, RECORDED)
    sent = planning_payloads(agents)
    assert [p.get("mode") for p in sent] == ["discuss", None, "record"]
    assert sent[0]["kind"] == "proposal"
    assert "agreed change: Sum money as long paise" in sent[1]["issue_text"]
    assert sent[2]["superseded"] == ["money-double"]
    assert DECISION in sent[2]["decisions"]
    assert "Design revised" in text(result.blocks)


async def test_a_partly_valid_change_applies_one_part_and_names_the_rest() -> None:
    partial = discussed(
        "partial",
        apply=["Log each rejection"],
        refused=[{"change": "drop the lock", "reason": "two transfers could pass the limit"}],
    )
    result, _, _ = await ask(
        "@Aetherion why not log and drop the lock?", partial, DESIGNED, RECORDED
    )
    body = text(result.blocks)
    assert "Design revised" in body and "Log each rejection" in body
    assert "drop the lock — two transfers could pass the limit" in body


async def test_update_the_tdd_is_checked_before_it_is_applied() -> None:
    result, t, agents = await ask(
        "@Aetherion update the TDD: use double for money",
        discussed("refuse", refused=[{"change": "use double", "reason": "less accurate"}]),
        intent="UPDATE_TDD",
    )
    sent = planning_payloads(agents)[0]
    assert sent["kind"] == "update" and sent["question"] == "use double for money"
    assert t.count("publish_tdd") == 0


async def test_update_the_tdd_anyway_is_sent_as_an_override() -> None:
    _, t, agents = await ask(
        "@Aetherion update the TDD anyway: use double",
        discussed("apply", apply=["use double"]),
        DESIGNED,
        RECORDED,
        intent="UPDATE_TDD",
    )
    assert planning_payloads(agents)[0]["kind"] == "update_override"
    assert t.count("publish_tdd") == 1


async def test_with_memory_off_update_the_tdd_is_applied_as_before() -> None:
    t = memory_tools()
    agents = Agents(planning_agent=[DESIGNED])
    await run_command(
        command("UPDATE_TDD", "@Aetherion update the TDD: X", t, agents, page_id="9001")
    )
    assert [p.get("mode") for p in planning_payloads(agents)] == [None]
    assert t.count("publish_tdd") == 1


async def test_coding_failures_revise_without_a_check() -> None:
    from agent.command_flow import update_tdd

    t = memory_tools()
    agents = Agents(planning_agent=[DESIGNED, RECORDED])
    cmd = command("CODE", "@Aetherion code", t, agents, page_id="9001", settings=ON)
    await update_tdd(cmd, ["tests failed"])
    assert [p.get("mode") for p in planning_payloads(agents)] == [None, "record"]


# --- a bug: Review judges the report, Planning finds the cause --------------------------


class BugChildren:
    """Review answers the report; Planning answers the code."""

    def __init__(self, planning: Any) -> None:
        self.planning = planning
        self.calls: list[tuple[str, dict, dict]] = []

    async def dispatch(self, agent_name: str, payload: dict, **options: Any) -> Any:
        self.calls.append((agent_name, payload, options))
        if agent_name == "planning_agent":
            if isinstance(self.planning, Exception):
                raise self.planning
            return self.planning
        return reviewed_contract(
            headline="Bug investigation — more information needed",
            readiness_score=5,
            investigation={"actual": "Submit does nothing", "expected": "An error is shown"},
        )


FOUND = {
    "status": "success",
    "mode": "investigate",
    "investigation": {
        "root_cause": "The phone regex runs on the untrimmed value.",
        "root_cause_confidence": "medium",
        "evidence": ["validators.ts applies internationalPhoneNumber"],
        "affected_files": [{"path": "ui/src/validators.ts", "why": "no trim"}],
        "proposed_fix": "Trim before validating; show the error.",
        "code_note": "Searched ui for internationalPhoneNumber.",
    },
}


async def test_a_bug_gets_the_cause_from_planning_and_keeps_reviews_part() -> None:
    tools_ = Tools(
        ingress_check=_bug_gate("@Aetherion investigate", commands={"repos": "ui: Org/ui"}),
        story_text={"ok": True, "text": "Submit silently fails", "hash": "h"},
    )
    children = BugChildren(FOUND)
    await run_router(PAYLOAD, tools_.execute, children.dispatch)

    planning = [c for c in children.calls if c[0] == "planning_agent"]
    assert planning and planning[0][1]["mode"] == "investigate"
    assert planning[0][1]["report_text"] == "Submit silently fails"
    assert planning[0][1]["repos"] == "ui: Org/ui"
    reply = tools_.reply_text()
    assert "Actual: Submit does nothing" in reply  # Review's part
    assert "The phone regex runs on the untrimmed value." in reply  # Planning's part
    assert "ui/src/validators.ts — no trim" in reply
    stored = [a for n, a in tools_.calls if n == "finish_ticket"]
    assert stored and stored[-1][1] == "REVIEW"
    assert stored[-1][3]["investigation"]["root_cause"].startswith("The phone regex")
    assert stored[-1][3]["headline"] == "Bug investigation — likely cause found"


async def test_planning_failing_still_replies_with_reviews_part() -> None:
    tools_ = Tools(ingress_check=_bug_gate("@Aetherion investigate"))
    children = BugChildren(RuntimeError("worker gone"))
    await run_router(PAYLOAD, tools_.execute, children.dispatch)

    reply = tools_.reply_text()
    assert "Actual: Submit does nothing" in reply
    assert "The code could not be investigated: RuntimeError: worker gone" in reply
