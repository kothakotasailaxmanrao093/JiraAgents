"""After a build: Review -> Planning -> Coding (2026-10-08).

The team's flow: "@Aetherion build" creates the tickets, Requirement Review
looks for gaps, Planning writes a technical design from the tickets, the review
and the code graph, and Coding implements it as a pull request — which a person
merges. When Coding cannot implement the design, the reasons go back to
Planning, and Coding tries the revised design.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agent.router_flow import run_router
from routing import pipeline as rules
from routing import settings
from routing.pipeline import PipelineConfig
from shared.contract import AgentResult, CreatedIssue, Outcome
from tests.fake_tools import Tools
from tests.test_orchestration import Clock, gate
from tools import pipeline_tools

PAYLOAD = {"issue_key": "BGV-32", "comment_id": "10501"}
REPOS = "orders: org/orders"
TDD = "technical_designs/20261008_bulk-print.pdf"
PR = "https://github.com/org/orders/pull/7"

FULL = {
    "steps": ["review", "plan", "code"],
    "repos": REPOS,
    "planning_agent": "planning_agent",
    "planning_queue": "",
    "planning_timeout_minutes": 15,
    "coding_agent": "coding-agent",
    "coding_queue": "",
    "coding_timeout_minutes": 20,
    "coding_attempts": 2,
    "plan_min_readiness": 0,
}


def built(outcome: Outcome = Outcome.CREATED) -> dict[str, Any]:
    return AgentResult(
        produced_by="JiraTaskCreation",
        display_name="Work Breakdown",
        outcome=outcome,
        headline="Created 1 Story",
        summary="BGV-32 stays a Story; 2 Sub-tasks were created under it.",
        created=[
            CreatedIssue("BGV-33", "Sub-task", "Add the print button"),
            CreatedIssue("BGV-34", "Sub-task", "Merge the PDFs"),
        ],
    ).to_dict()


def reviewed(score: int = 4) -> dict[str, Any]:
    return AgentResult(
        produced_by="JiraRequirementReview",
        display_name="Requirement Review",
        outcome=Outcome.REVIEWED,
        headline="Ready with minor gaps",
        summary=f"Readiness {score}/5.",
        readiness_score=score,
        questions=["What is the most PDFs one print may hold?"],
    ).to_dict()


def planned(key: str = TDD) -> dict[str, Any]:
    return {
        "status": "success",
        "results": [{"type": "s3_download_link", "file_key": key}],
    }


def coded(pr: str = PR) -> dict[str, Any]:
    return {
        "status": "success",
        "pull_requests": [
            {"service": "orders", "pr_url": pr, "tests_ran": True, "tests_passed": True}
        ],
        "skipped": [],
    }


NO_PR = {
    "status": "no_pull_requests",
    "message": "No Pull Requests were created",
    "pull_requests": [],
    "skipped": [{"service": "orders", "file_path": "src/print.py", "reason": "not in the clone"}],
}


class Agents:
    """Every agent the router starts, each answering from its own queue of results."""

    def __init__(self, **answers: list[Any]) -> None:
        self.answers = {
            "JiraTaskCreation": [built()],
            "JiraRequirementReview": [reviewed()],
            "planning_agent": [planned()],
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
        return answer

    def names(self) -> list[str]:
        return [agent for agent, _, _ in self.calls]

    def payloads(self, agent: str) -> list[dict]:
        return [payload for name, payload, _ in self.calls if name == agent]


def tools_for(pipeline: dict | None = FULL, **answers: Any) -> Tools:
    return Tools(
        ingress_check=gate("@Aetherion build", pipeline=pipeline),
        planning_input={"issue_text": "Requirement BGV-32: Bulk print"},
        attach_tdd={"attached": True, "filename": "BGV-32-technical-design.pdf"},
        **answers,
    )


async def run(tools: Tools, agents: Agents) -> dict[str, Any]:
    return await run_router(PAYLOAD, tools.execute, agents.dispatch, Clock().sleep)


def statuses(out: dict[str, Any]) -> dict[str, str]:
    return {s["step"]: s["status"] for s in out["after_build"]}


# --- the whole flow -------------------------------------------------------------------


async def test_a_build_goes_on_to_review_planning_and_a_pull_request() -> None:
    tools, agents = tools_for(), Agents()
    out = await run(tools, agents)

    assert agents.names() == [
        "JiraTaskCreation",
        "JiraRequirementReview",
        "planning_agent",
        "coding-agent",
    ]
    assert statuses(out) == {"review": "done", "plan": "done", "code": "done"}
    reply = tools.reply_text()
    assert PR in reply and "nothing is merged automatically" in reply
    assert "Attached to BGV-32: BGV-32-technical-design.pdf" in reply
    assert (
        "Jira Orchestration → Work Breakdown, then Requirement Review, then Planning, then Coding"
        in reply
    )
    assert tools.count("post_reply") == 1, "still one reply per comment"


async def test_each_agent_gets_what_it_needs() -> None:
    tools, agents = tools_for(), Agents()
    await run(tools, agents)

    review = agents.payloads("JiraRequirementReview")[0]
    assert review["mode"] == "delegated" and review["post_to_jira"] is False
    assert review["issue_key"] == "BGV-32"

    issue_key, keys, proposed, review_dict = tools.args("planning_input")
    assert (issue_key, keys, proposed) == ("BGV-32", ["BGV-33", "BGV-34"], [])
    assert review_dict["readiness_score"] == 4

    plan = agents.payloads("planning_agent")[0]
    assert plan["issue_text"] == "Requirement BGV-32: Bulk print" and plan["repos"] == REPOS

    code = agents.payloads("coding-agent")[0]
    assert code["uploaded_files"] == [TDD] and code["mode"] == "delegated"
    assert code["repos"] == REPOS, "Coding writes only to the configured repositories"


async def test_the_ticket_shows_each_step_while_it_runs() -> None:
    tools, agents = tools_for(), Agents()
    await run(tools, agents)

    edits = [str(args[2]) for name, args in tools.calls if name == "update_reply"]
    assert any("⏳ Review" in e and "◻ Technical design" in e for e in edits)
    assert any("✅ Review" in e and "⏳ Technical design" in e for e in edits)
    assert any("✅ Technical design" in e and "⏳ Pull request" in e for e in edits)


async def test_the_ticket_is_held_for_the_whole_chain() -> None:
    tools = tools_for()
    await run(tools, Agents())
    minutes = tools.args("claim_ticket")[4]
    # build 7 + review 8 + plan 15 + 2 coding runs (40) + one re-plan (15)
    assert minutes == 85


# --- Coding cannot implement the design: back to Planning -----------------------------


async def test_a_design_coding_cannot_implement_goes_back_to_planning() -> None:
    tools = tools_for()
    agents = Agents(
        planning_agent=[planned(TDD), planned("technical_designs/revised.pdf")],
        **{"coding-agent": [NO_PR, coded()]},
    )
    out = await run(tools, agents)

    assert agents.names()[2:] == [
        "planning_agent",
        "coding-agent",
        "planning_agent",
        "coding-agent",
    ]
    second_plan = agents.payloads("planning_agent")[1]["issue_text"]
    assert "could not be implemented" in second_plan and "not in the clone" in second_plan
    assert agents.payloads("coding-agent")[1]["uploaded_files"] == ["technical_designs/revised.pdf"]
    assert statuses(out) == {"review": "done", "plan": "done", "code": "done"}
    assert "written (revised)" in tools.reply_text()


async def test_after_the_last_attempt_the_reasons_are_on_the_ticket() -> None:
    tools = tools_for()
    agents = Agents(**{"coding-agent": [NO_PR]})
    out = await run(tools, agents)

    assert agents.names().count("coding-agent") == 2
    assert statuses(out)["code"] == "failed"
    assert "orders (src/print.py): not in the clone" in tools.reply_text()


async def test_coding_unreachable_is_not_sent_back_to_planning() -> None:
    tools = tools_for()
    agents = Agents(**{"coding-agent": [RuntimeError("no worker")]})
    out = await run(tools, agents)

    assert agents.names().count("planning_agent") == 1
    assert statuses(out)["code"] == "failed"
    assert "Coding did not respond: RuntimeError: no worker" in tools.reply_text()


# --- a step that cannot run stops the chain, saying why --------------------------------


async def test_no_design_means_no_coding() -> None:
    tools = tools_for()
    agents = Agents(planning_agent=[{"status": "error", "message": "RepoListError: no services"}])
    out = await run(tools, agents)

    assert "coding-agent" not in agents.names()
    assert statuses(out) == {"review": "done", "plan": "failed", "code": "skipped"}
    assert "Planning wrote no design: RepoListError: no services" in tools.reply_text()


async def test_without_repositories_planning_is_not_started() -> None:
    tools = tools_for({**FULL, "repos": ""})
    agents = Agents()
    out = await run(tools, agents)

    assert agents.names() == ["JiraTaskCreation", "JiraRequirementReview"]
    assert statuses(out) == {"review": "done", "plan": "skipped", "code": "skipped"}
    assert "no repositories to design against" in tools.reply_text()


async def test_a_poor_review_pauses_planning_when_asked_to() -> None:
    tools = tools_for({**FULL, "plan_min_readiness": 3})
    agents = Agents(JiraRequirementReview=[reviewed(score=2)])
    out = await run(tools, agents)

    assert "planning_agent" not in agents.names()
    assert statuses(out) == {"review": "done", "plan": "skipped", "code": "skipped"}
    assert "scored the request 2/5, below the 3/5" in tools.reply_text()


async def test_a_failed_review_does_not_stop_planning() -> None:
    tools = tools_for()
    agents = Agents(JiraRequirementReview=[RuntimeError("timed out")])
    out = await run(tools, agents)

    assert statuses(out) == {"review": "failed", "plan": "done", "code": "done"}
    assert tools.args("planning_input")[3] is None


@pytest.mark.parametrize("outcome", [Outcome.NEEDS_INFO, Outcome.FAILED, Outcome.NOT_A_REQUIREMENT])
async def test_a_build_with_nothing_to_go_on_ends_there(outcome: Outcome) -> None:
    tools = tools_for()
    agents = Agents(JiraTaskCreation=[built(outcome)])
    out = await run(tools, agents)

    assert agents.names() == ["JiraTaskCreation"]
    assert out["after_build"] == []


async def test_with_no_steps_configured_a_build_is_exactly_as_before() -> None:
    tools = tools_for(pipeline=None)
    agents = Agents()
    out = await run(tools, agents)

    assert agents.names() == ["JiraTaskCreation"]
    assert out["after_build"] == []
    assert tools.args("claim_ticket")[4] == 0
    assert "Technical design" not in tools.reply_text()


async def test_a_review_request_never_starts_the_chain() -> None:
    tools = Tools(ingress_check=gate("@Aetherion review", pipeline=FULL))
    agents = Agents()
    await run(tools, agents)
    assert agents.names() == ["JiraRequirementReview"]


async def test_planning_and_coding_go_to_their_configured_queues() -> None:
    tools = tools_for({**FULL, "planning_queue": "q-plan", "coding_queue": "q-code"})
    agents = Agents()
    await run(tools, agents)
    queues = {agent: options.get("task_queue") for agent, _, options in agents.calls}
    assert queues["planning_agent"] == "q-plan" and queues["coding-agent"] == "q-code"


# --- the rules, alone ---------------------------------------------------------------


def test_planning_is_told_the_request_the_tickets_and_the_review() -> None:
    text = rules.planning_text(
        {
            "key": "BGV-32",
            "summary": "Bulk print",
            "description": "Print many PDFs at once.",
        },
        [
            {
                "key": "BGV-33",
                "type": "Sub-task",
                "summary": "Button",
                "description": "A button.",
            }
        ],
        [],
        reviewed(),
    )
    assert text.startswith("Requirement BGV-32: Bulk print")
    assert "- Sub-task BGV-33: Button" in text and "  A button." in text
    assert "readiness 4/5" in text and "- What is the most PDFs one print may hold?" in text


def test_a_pdf_only_build_hands_planning_its_proposal() -> None:
    text = rules.planning_text({"key": "BGV-32", "summary": "S"}, [], ["S1 Story"], None)
    assert "proposed for it (not created in Jira)" in text and "- S1 Story" in text


def test_tickets_include_the_ones_that_already_existed() -> None:
    build = AgentResult.from_dict(
        {
            **built(Outcome.ALREADY_EXISTS),
            "created": [],
            "duplicates": [{"existing_key": "BGV-20", "existing_summary": "Bulk print"}],
        }
    )
    assert rules.ticket_keys(build) == ["BGV-20"]


def test_the_config_survives_the_trip_from_the_activity() -> None:
    config = PipelineConfig.from_dict({**FULL, "unknown_new_field": 1})
    assert config.steps == ("review", "plan", "code") and config.coding_attempts == 2
    assert PipelineConfig.from_dict(None).steps == ()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ()),
        ("review,plan,code", ("review", "plan", "code")),
        ("code, plan", ("plan", "code")),
        ("review,code", ("review",)),  # code needs a design
        ("Review, nonsense", ("review",)),
    ],
)
def test_which_steps_follow_a_build(monkeypatch, raw, expected) -> None:
    monkeypatch.setenv("ORCH_BUILD_PIPELINE", raw)
    assert settings.build_pipeline() == expected


def test_repositories_may_share_one_line(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_PLANNING_REPOS", "orders: org/orders; user: org/user@develop")
    assert settings.planning_repos() == "orders: org/orders\nuser: org/user@develop"


# --- the tools ----------------------------------------------------------------------


def _jira(issues: dict[str, dict], posted: list | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        key = request.url.path.split("/")[5]
        if request.method == "POST":
            posted.append((key, request.headers.get("X-Atlassian-Token"), request.content))
            return httpx.Response(200, json=[{"id": "1"}])
        if key not in issues:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"key": key, "fields": issues[key]})

    return handle


def fields(summary: str, kind: str, description: str = "") -> dict:
    doc = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}],
    }
    return {"summary": summary, "issuetype": {"name": kind}, "description": doc}


async def test_planning_input_reads_every_ticket_fresh(monkeypatch) -> None:
    issues = {
        "BGV-32": fields("Bulk print", "Story", "Print many PDFs."),
        "BGV-33": fields("Button", "Sub-task", "A print button."),
    }
    monkeypatch.setattr(
        pipeline_tools,
        "_client",
        lambda: httpx.AsyncClient(
            base_url="https://j.test", transport=httpx.MockTransport(_jira(issues))
        ),
    )
    out = await pipeline_tools.planning_input("bgv-32", ["BGV-33", "BGV-99"], [], reviewed())

    assert "Print many PDFs." in out["issue_text"] and "A print button." in out["issue_text"]
    assert out["unread"] == ["BGV-99"] and "BGV-99" in out["issue_text"]


async def test_the_design_is_attached_to_the_ticket(monkeypatch) -> None:
    from common_lib.storage import storage_client

    monkeypatch.setenv("TENANT_ID", "team")
    monkeypatch.setenv("JIRA_BASE_URL", "https://j.test")
    monkeypatch.setenv("JIRA_EMAIL", "a@b.c")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    monkeypatch.setattr(storage_client.storage, "init_client", lambda: None)
    monkeypatch.setattr(storage_client.storage, "retrieve", lambda b, k, m: b"%PDF-1.4 tdd")
    posted: list = []
    real = httpx.AsyncClient

    def client(**kwargs):
        return real(**kwargs, transport=httpx.MockTransport(_jira({}, posted)))

    monkeypatch.setattr(pipeline_tools.httpx, "AsyncClient", client)
    out = await pipeline_tools.attach_tdd("BGV-32", TDD, "r1")

    assert out == {"attached": True, "filename": "BGV-32-technical-design-20261008.pdf"}
    key, token_header, body = posted[0]
    assert key == "BGV-32" and token_header == "no-check" and b"%PDF-1.4 tdd" in body


async def test_no_storage_is_said_not_raised(monkeypatch) -> None:
    monkeypatch.delenv("TENANT_ID", raising=False)
    monkeypatch.delenv("STORAGE_AGENTS_BUCKET", raising=False)
    out = await pipeline_tools.attach_tdd("BGV-32", TDD, "r1")
    assert out == {"attached": False, "error": "no storage configured (TENANT_ID)"}


def test_the_result_summary_is_plain_data() -> None:
    steps = [rules.Step("review", "done", "Ready", ["x"])]
    assert json.loads(json.dumps(rules.summary(steps)))[0]["status"] == "done"


# --- GitHub refusing the token is not a design problem (live run, BGV-104) -------------

REFUSED = (
    "RepoOpsError: could not create blob for src/main/java/com/odyssey/controller/"
    "MedicalController.java: Client error '403 Forbidden' for url "
    "'https://api.github.com/repos/me/MedicalManagementSystem/git/blobs'\n"
    "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403"
    ' — {"message":"Resource not accessible by personal access token","status":"403"}'
)
REFUSED_RUN = {
    "status": "no_pull_requests",
    "message": "No Pull Requests were created",
    "pull_requests": [],
    "skipped": [
        {"service": "medical", "file_path": path, "reason": REFUSED}
        for path in ("Controller.java", "ServiceImpl.java", "Repository.java")
    ],
}


async def test_a_refused_token_is_reported_once_and_not_sent_back_to_planning() -> None:
    tools = tools_for()
    agents = Agents(**{"coding-agent": [REFUSED_RUN]})
    out = await run(tools, agents)

    assert agents.names().count("planning_agent") == 1, "a new design cannot fix a token"
    assert agents.names().count("coding-agent") == 1
    code = out["after_build"][2]
    assert code["status"] == "failed" and "Contents and Pull requests" in code["detail"]
    assert code["lines"] == [
        "medical (3 files): RepoOpsError: could not create blob for src/main/java/com/odyssey/"
        "controller/MedicalController.java: Client error '403 Forbidden' — Resource not "
        "accessible by personal access token"
    ]


def test_a_design_problem_is_not_an_access_problem() -> None:
    assert not rules.is_access_problem(NO_PR)
    assert rules.is_access_problem(REFUSED_RUN)


async def test_the_review_line_is_the_score_and_the_points() -> None:
    tools, agents = tools_for(), Agents()
    out = await run(tools, agents)
    assert out["after_build"][0]["detail"] == "readiness 4/5, 1 open point(s)"


async def test_with_a_minimum_set_a_failed_review_does_not_pass_the_gate() -> None:
    tools = tools_for({**FULL, "plan_min_readiness": 4})
    agents = Agents(JiraRequirementReview=[RuntimeError("timed out")])
    out = await run(tools, agents)

    assert "planning_agent" not in agents.names()
    assert statuses(out) == {"review": "failed", "plan": "skipped", "code": "skipped"}
    assert "the review gave no score" in tools.reply_text()


def test_an_untested_pull_request_says_it_is_a_draft_to_test_first() -> None:
    coding = {
        "pull_requests": [
            {"service": "bank", "pr_url": "https://github.com/o/r/pull/3", "tests_ran": False}
        ]
    }
    (line,) = rules.pr_lines(coding)
    assert "tests not run" in line and "draft" in line and "before merging" in line


def test_a_ci_verdict_is_named_as_ci() -> None:
    def line(passed: bool) -> str:
        pr = {"service": "bank", "pr_url": "u", "tests_ran": True, "tests_passed": passed}
        return rules.pr_lines({"pull_requests": [{**pr, "verified_by": "ci"}]})[0]

    assert line(True) == "bank: u (CI passed)"
    assert line(False) == "bank: u (CI FAILED — see the PR)"
    assert rules.tests_failed(
        {
            "pull_requests": [
                {"pr_url": "u", "tests_ran": True, "tests_passed": False, "verified_by": "ci"}
            ]
        }
    ), "a CI failure goes back to Planning like a failed local run"
