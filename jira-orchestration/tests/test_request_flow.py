"""A request from the orchestrator's own form: nothing written to Jira (2026-10-08).

The person types a description and attaches files. Task Creation breaks it
down into a PDF (Jira is only read, for work that already exists), Requirement
Review scores that PDF, and at 4/5 or above Planning writes the design and
Coding raises the pull request — going back to Planning when it cannot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent import request_flow
from agent.request_flow import run_request, title_of, upload_keys
from shared.contract import AgentResult, Outcome
from tests.fake_tools import Tools
from tests.test_pipeline import FULL, NO_PR, coded, planned, reviewed
from tools import pipeline_tools

SPEC = "local-uploads/ab12/daily-limit-spec.docx"
BREAKDOWN = "breakdowns/20261008T101500Z_aetherion-breakdown-BGV-REQUEST-1.pdf"
TDD = "technical_designs/20261008_daily-limit.pdf"
REQUEST = {
    "requirement": "Add a daily transfer limit\nReject a transfer above 100000 a day.",
    "supporting_files": [SPEC],
    "project_key": "bgv",
}
# Only these may run: no tool here reads or writes Jira.
NO_JIRA = {"request_settings", "keep_design_copy"}


def broken_down(outcome: Outcome = Outcome.PLANNED, documents: bool = True) -> dict:
    return AgentResult(
        produced_by="JiraTaskCreation",
        display_name="Work Breakdown",
        outcome=outcome,
        headline="Work breakdown emailed as a PDF — no Jira changes made",
        summary="The breakdown proposes 1 Story and 3 Sub-tasks.",
        proposed=["S1 Daily transfer limit — Sub-tasks: S1.1 Track the day's total"],
        questions=["Is the limit per account or per customer?"],
        documents=(
            [{"name": "aetherion-breakdown-BGV-REQUEST-1.pdf", "file_key": BREAKDOWN}]
            if documents
            else []
        ),
    ).to_dict()


class Agents:
    def __init__(self, **answers: list[Any]) -> None:
        self.answers = {
            "JiraTaskCreation": [broken_down()],
            "JiraRequirementReview": [reviewed(score=4)],
            "planning_agent": [planned(TDD)],
            "coding-agent": [coded()],
            **answers,
        }
        self.calls: list[tuple[str, dict, dict]] = []

    async def dispatch(self, agent: str, payload: dict, **options: Any) -> Any:
        self.calls.append((agent, payload, options))
        queue = self.answers[agent]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return [answer] if agent == "JiraRequirementReview" else answer

    def names(self) -> list[str]:
        return [a for a, _, _ in self.calls]

    def payload(self, agent: str, n: int = 0) -> dict:
        return [p for a, p, _ in self.calls if a == agent][n]


def tools_for(**over: Any) -> Tools:
    settings = {
        "run_id": "r9",
        "project_key": "BGV",
        "problem": "",
        "pipeline": {**FULL, "steps": []},
        "task_queue_overrides": {},
        "min_readiness": 4,
        **over,
    }
    return Tools(
        request_settings=settings,
        keep_design_copy={
            "saved": True,
            "path": "/laptop/docs-export/pipeline/tdd-x.pdf",
        },
    )


async def run(tools: Tools, agents: Agents, **payload: Any) -> dict:
    return await run_request({**REQUEST, **payload}, tools.execute, agents.dispatch)


# --- the whole flow -----------------------------------------------------------------


async def test_a_request_goes_from_description_to_pull_request() -> None:
    tools, agents = tools_for(), Agents()
    out = await run(tools, agents)

    assert agents.names() == [
        "JiraTaskCreation",
        "JiraRequirementReview",
        "planning_agent",
        "coding-agent",
    ]
    assert out["status"] == "done"
    assert out["steps"][0] == ("✅ Work breakdown — emailed as a PDF — no Jira changes made")
    assert out["steps"][-1].startswith("✅ Pull request — raised")
    assert out["handled_by"] == (
        "Jira Orchestration → Work Breakdown, then Requirement Review, then Planning, then Coding"
    )
    assert "merge it" in out["next"]


async def test_nothing_is_written_to_jira() -> None:
    tools = tools_for()
    await run(tools, Agents())
    assert set(tools.names()) <= NO_JIRA, tools.names()


async def test_task_creation_makes_a_pdf_and_keeps_it_for_review() -> None:
    tools, agents = tools_for(), Agents()
    await run(tools, agents)
    sent = agents.payload("JiraTaskCreation")
    assert sent["requirement"] == REQUEST["requirement"]
    assert sent["project_key"] == "BGV" and sent["supporting_files"] == [SPEC]
    assert sent["output"] == "pdf_by_email" and sent["keep_document"] is True
    assert sent["mode"] == "delegated" and "issue_key" not in sent


async def test_review_reads_the_pdf_and_the_files_not_a_ticket() -> None:
    tools, agents = tools_for(), Agents()
    await run(tools, agents)
    sent = agents.payload("JiraRequirementReview")
    assert sent["document_file_keys"] == [BREAKDOWN, SPEC]
    assert sent["document_title"] == "Add a daily transfer limit"
    assert sent["post_to_jira"] is False and "issue_key" not in sent


async def test_planning_gets_the_request_the_breakdown_and_the_review() -> None:
    tools, agents = tools_for(), Agents()
    await run(tools, agents)
    text = agents.payload("planning_agent")["issue_text"]
    assert text.startswith("Requirement Request: Add a daily transfer limit")
    assert "Reject a transfer above 100000 a day." in text
    assert "Files attached to the request: daily-limit-spec.docx" in text
    assert "- S1 Daily transfer limit" in text
    assert "readiness 4/5" in text and "What is the most PDFs one print may hold?" in text
    assert agents.payload("planning_agent")["repos"] == FULL["repos"]
    assert agents.payload("coding-agent")["uploaded_files"] == [TDD]


async def test_the_result_holds_the_documents_and_the_pr() -> None:
    tools, agents = tools_for(), Agents()
    out = await run(tools, agents)
    titles = [r["title"] for r in out["results"]]
    assert titles == [
        "Work breakdown (PDF)",
        "Technical design (PDF)",
        "Pull request: orders",
    ]
    assert out["results"][0]["file_key"] == BREAKDOWN and out["results"][1]["file_key"] == TDD
    assert out["results"][2]["link"].startswith("[https://github.com/org/orders/pull/7]")
    plan = out["details"][2]
    assert plan["lines"] == ["Saved on this laptop: /laptop/docs-export/pipeline/tdd-x.pdf"]


# --- the gate: 4/5 or above ------------------------------------------------------------


async def test_a_review_below_the_minimum_stops_before_planning() -> None:
    tools = tools_for()
    agents = Agents(JiraRequirementReview=[reviewed(score=3)])
    out = await run(tools, agents)

    assert "planning_agent" not in agents.names()
    assert out["status"] == "stopped"
    assert any("scored the request 3/5, below the 4/5" in s for s in out["steps"])


async def test_five_out_of_five_goes_on() -> None:
    agents = Agents(JiraRequirementReview=[reviewed(score=5)])
    out = await run(tools_for(), agents)
    assert out["status"] == "done"


async def test_the_form_can_set_the_minimum() -> None:
    agents = Agents(JiraRequirementReview=[reviewed(score=3)])
    out = await run(tools_for(), agents, min_review_score=3)
    assert "planning_agent" in agents.names() and out["status"] == "done"


async def test_a_review_that_failed_does_not_pass_the_gate() -> None:
    agents = Agents(JiraRequirementReview=[RuntimeError("timed out")])
    out = await run(tools_for(), agents)
    assert "planning_agent" not in agents.names()
    assert any("the review gave no score" in s for s in out["steps"])


# --- Coding cannot implement it: back to Planning ---------------------------------------


async def test_a_design_coding_cannot_implement_goes_back_to_planning() -> None:
    agents = Agents(
        planning_agent=[planned(TDD), planned("technical_designs/revised.pdf")],
        **{"coding-agent": [NO_PR, coded()]},
    )
    out = await run(tools_for(), agents)
    assert agents.names()[2:] == [
        "planning_agent",
        "coding-agent",
        "planning_agent",
        "coding-agent",
    ]
    assert out["status"] == "done"
    assert [r["title"] for r in out["results"]][1:3] == [
        "Technical design (PDF) — version 1",
        "Technical design (PDF) — version 2",
    ]


# --- what stops at the start ------------------------------------------------------------


async def test_a_breakdown_that_needs_answers_stops_with_its_questions() -> None:
    agents = Agents(JiraTaskCreation=[broken_down(Outcome.NEEDS_INFO, documents=False)])
    out = await run(tools_for(), agents)
    assert agents.names() == ["JiraTaskCreation"]
    assert out["status"] == "failed"
    assert out["questions"] == ["Is the limit per account or per customer?"]


async def test_a_pdf_that_could_not_be_kept_cannot_be_reviewed() -> None:
    agents = Agents(JiraTaskCreation=[broken_down(documents=False)])
    out = await run(tools_for(), agents)
    assert agents.names() == ["JiraTaskCreation"]
    assert "could not be kept for Review" in out["steps"][0]


async def test_an_empty_description_starts_nothing() -> None:
    agents = Agents()
    out = await run(tools_for(), agents, requirement="  ")
    assert agents.calls == [] and out["status"] == "failed"
    assert "Enter the description of the work." in out["steps"][0]


async def test_a_project_this_agent_does_not_serve_starts_nothing() -> None:
    agents = Agents()
    out = await run(tools_for(problem="KS is not one of this agent's projects (BGV)."), agents)
    assert agents.calls == [] and "KS is not one of" in out["steps"][0]


async def test_task_creation_unreachable_is_said() -> None:
    agents = Agents(JiraTaskCreation=[RuntimeError("no worker")])
    out = await run(tools_for(), agents)
    assert "Task Creation did not respond: RuntimeError: no worker" in out["steps"][0]


# --- the pieces -----------------------------------------------------------------------


def test_files_from_either_field_once_each() -> None:
    assert upload_keys({"supporting_files": [SPEC], "uploaded_files": f"{SPEC}, b.pdf"}) == [
        SPEC,
        "b.pdf",
    ]


def test_the_request_is_named_by_its_first_line() -> None:
    assert title_of("\n  Daily limit  \nmore") == "Daily limit"
    assert title_of("x" * 100).endswith("…") and len(title_of("x" * 100)) == 80


async def test_the_form_routes_a_requirement_to_this_flow(monkeypatch) -> None:
    from agent import router_agent

    seen: list[dict] = []

    async def fake(payload, execute, dispatch):
        seen.append(payload)
        return {"status": "done"}

    monkeypatch.setattr(router_agent, "run_request", fake)
    out = await router_agent.JiraOrchestration.fn({"requirement": "Daily limit"})
    assert out == {"status": "done"} and seen == [{"requirement": "Daily limit"}]


def test_the_form_offers_the_request_fields() -> None:
    root = Path(__file__).resolve().parents[1]
    meta = json.loads((root / "src" / "agent" / "metadata.json").read_text())
    names = [t["name"] for t in meta["config"]["triggers"]]
    assert names[:4] == [
        "requirement",
        "supporting_files",
        "project_key",
        "min_review_score",
    ]


def test_the_form_offers_both_polls_off_by_default() -> None:
    """Each poll is its own schedule; a schedule sets one, the other stays off."""
    root = Path(__file__).resolve().parents[1]
    meta = json.loads((root / "src" / "agent" / "metadata.json").read_text())
    polls = {
        t["name"]: t
        for t in meta["config"]["triggers"]
        if t["name"] in ("github_poll", "confluence_poll")
    }
    assert set(polls) == {"github_poll", "confluence_poll"}
    for trigger in polls.values():
        assert trigger["type"] == "boolean" and trigger["default"] is False


@pytest.mark.parametrize(
    "project,allowed,expected_problem",
    [("", "BGV,FL", ""), ("fl", "BGV,FL", ""), ("KS", "BGV,FL", "KS is not one of")],
)
async def test_request_settings(monkeypatch, project, allowed, expected_problem) -> None:
    monkeypatch.setenv("ORCH_ALLOWED_PROJECT_KEYS", allowed)
    monkeypatch.delenv("ORCH_REQUEST_PROJECT", raising=False)
    monkeypatch.delenv("ORCH_REQUEST_MIN_READINESS", raising=False)
    out = await pipeline_tools.request_settings(project)
    assert out["project_key"] == (project.upper() or "BGV")
    assert out["min_readiness"] == 4
    assert (expected_problem in out["problem"]) if expected_problem else out["problem"] == ""


async def test_a_laptop_keeps_a_copy_of_the_design(monkeypatch, tmp_path) -> None:
    from common_lib.storage import storage_client

    monkeypatch.setenv("AETHERION_LOCAL_RUN", "1")
    monkeypatch.setenv("ORCH_DOCUMENT_DIR", str(tmp_path))
    monkeypatch.setenv("TENANT_ID", "planning-local")
    monkeypatch.setattr(storage_client.storage, "init_client", lambda: None)
    monkeypatch.setattr(storage_client.storage, "retrieve", lambda b, k, m: b"%PDF tdd")
    out = await pipeline_tools.keep_design_copy(TDD, "tdd", "r9")
    assert out["saved"] is True
    assert Path(out["path"]).read_bytes() == b"%PDF tdd"
    assert Path(out["path"]).name == "tdd-20261008_daily-limit.pdf"


async def test_on_the_platform_no_copy_is_written(monkeypatch) -> None:
    monkeypatch.delenv("AETHERION_LOCAL_RUN", raising=False)
    assert await pipeline_tools.keep_design_copy(TDD, "tdd", "r9") == {"saved": False}


def test_the_flow_imports_nothing_that_reads_the_environment() -> None:
    source = Path(request_flow.__file__).read_text()
    assert "os.environ" not in source and "import os" not in source


async def test_without_coding_there_is_no_pr_to_merge() -> None:
    tools = tools_for(pipeline={**FULL, "steps": ["review", "plan"]})
    out = await run(tools, Agents())
    assert out["status"] == "done" and "next" not in out
    assert out["handled_by"].startswith("Jira Orchestration → Work Breakdown, then")


# Aetherion's upload service accepts only these (publish refused .srt, 2026-10-13).
PLATFORM_EXTENSIONS = {
    ".bmp",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".odt",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tif",
    ".tiff",
    ".txt",
    ".vtt",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}


def test_every_file_field_accepts_only_what_the_platform_allows() -> None:
    root = Path(__file__).resolve().parents[1]
    meta = json.loads((root / "src" / "agent" / "metadata.json").read_text())
    for trigger in meta["config"]["triggers"]:
        for ext in filter(None, (trigger.get("accepts") or "").split(",")):
            assert ext.strip() in PLATFORM_EXTENSIONS, f"{trigger['name']}: {ext}"
