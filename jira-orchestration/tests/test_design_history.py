"""Earlier work is kept when a new feature is designed or a design is revised (2026-10-15).

* A new TDD is given the project's earlier designs — decisions already made —
  and links to them, so Feature 2's design extends Feature 1 instead of
  describing only itself.
* A revision sends Planning the WHOLE current design: it used to be cut at
  20,000 characters, and BTS-8's revision lost BTS-4's same-account rule.
* The webhook URLs come from settings; until they are known, the health check
  says which checks wait for them.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fake_tools import Tools
from test_commands import PAGE, SETTINGS, Agents, command, tools

from agent.command_flow import run_command
from agent.router_flow import run_health_check
from routing import commands, settings
from tools import confluence_tools

EARLIER = [
    {
        "title": "BTS-4 - TDD v1.0",
        "url": "https://site/wiki/spaces/BTS/pages/4",
        "excerpt": "Requirements: transfer() throws 'Cannot transfer to the same account'.",
    },
    {
        "title": "BTS-1 - TDD v1.0",
        "url": "https://site/wiki/spaces/BTS/pages/1",
        "excerpt": "Requirements: getHistoryFor returns an unmodifiable list.",
    },
]


async def test_a_new_design_is_given_the_earlier_designs_and_links_them() -> None:
    t, agents = tools(earlier_designs={"ok": True, "designs": EARLIER}), Agents()
    await run_command(command("TDD", "@Aetherion create TDD", t, agents))

    issue_text = agents.payload("planning_agent")["issue_text"]
    assert issue_text.startswith("Requirement BGV-105: Daily transfer limit")
    assert "Earlier designs in this project — decisions already made. Keep them" in issue_text
    assert "Cannot transfer to the same account" in issue_text
    assert "getHistoryFor returns an unmodifiable list" in issue_text
    assert t.args("publish_tdd")[7] == [
        {"title": "BTS-4 - TDD v1.0", "url": "https://site/wiki/spaces/BTS/pages/4"},
        {"title": "BTS-1 - TDD v1.0", "url": "https://site/wiki/spaces/BTS/pages/1"},
    ]


async def test_the_first_design_in_a_project_reads_as_before() -> None:
    t, agents = tools(earlier_designs={"ok": True, "designs": []}), Agents()
    await run_command(command("TDD", "@Aetherion create TDD", t, agents))
    assert agents.payload("planning_agent")["issue_text"] == (
        "Requirement BGV-105: Daily transfer limit"
    )
    assert t.args("publish_tdd")[7] == []


async def test_a_revision_gets_the_whole_current_design_and_keeps_it() -> None:
    """A 35,000-character TDD lost its end at the old 20,000 cut."""
    long_design = "3. Requirements\n" + ("x" * 40_000) + "\nLAST LINE: tests and risks"
    t = tools(
        read_tdd={**PAGE, "text": long_design},
        earlier_designs={"ok": True, "designs": EARLIER[:1]},
    )
    agents = Agents()
    await run_command(
        command("UPDATE_TDD", "@Aetherion update the TDD: log the count", t, agents, page_id="9001")
    )

    issue_text = agents.payload("planning_agent")["issue_text"]
    assert "LAST LINE: tests and risks" in issue_text, "the end of the design is not cut"
    assert "This is a REVISION of the technical design below, not a new one" in issue_text
    assert "Keep every requirement, decision, file change, test and item of existing" in issue_text
    assert "Cannot transfer to the same account" in issue_text
    assert t.args("publish_tdd")[7] == [
        {"title": "BTS-4 - TDD v1.0", "url": "https://site/wiki/spaces/BTS/pages/4"}
    ]


def test_the_page_header_links_the_earlier_designs() -> None:
    header = commands.tdd_page_header(
        "BTS-12", "https://site/browse/BTS-12", related=[{"title": "BTS-4 <v1>", "url": "u&4"}]
    )
    assert "Builds on the earlier designs in this project" in header
    assert '<a href="u&amp;4">BTS-4 &lt;v1&gt;</a>' in header
    assert "Builds on" not in commands.tdd_page_header("BTS-12", "https://site/browse/BTS-12")


# --- the earlier_designs tool, against a fake Confluence ----------------------------------


def _page(key: str, version: str = "v1.0", body: str = "") -> dict[str, Any]:
    return {
        "title": f"{key} - TDD {version}",
        "_links": {"webui": f"/spaces/BTS/pages/{key}"},
        "body": {"storage": {"value": body or f"<p>3. Requirements</p><p>{key} rule</p>"}},
    }


@pytest.fixture
def wiki(monkeypatch):
    seen: dict[str, Any] = {}
    pages = [_page(f"BTS-{n}") for n in (12, 8, 4, 1, 20, 21, 22)]

    def handle(request: httpx.Request) -> httpx.Response:
        seen["cql"] = request.url.params.get("cql")
        return httpx.Response(200, json={"results": pages, "_links": {"base": "https://w"}})

    monkeypatch.setattr(
        confluence_tools,
        "_wiki",
        lambda: httpx.AsyncClient(base_url="https://w", transport=httpx.MockTransport(handle)),
    )
    monkeypatch.delenv("ORCH_CONFLUENCE_SPACES", raising=False)
    monkeypatch.delenv("ORCH_TDD_LABEL", raising=False)
    return seen, pages


async def test_earlier_designs_are_the_projects_other_tdds_newest_first(wiki) -> None:
    seen, _ = wiki
    out = await confluence_tools.earlier_designs("bts-12")
    assert out["ok"]
    assert [d["title"] for d in out["designs"]] == [
        "BTS-8 - TDD v1.0",
        "BTS-4 - TDD v1.0",
        "BTS-1 - TDD v1.0",
        "BTS-20 - TDD v1.0",
        "BTS-21 - TDD v1.0",
    ], "the story's own page is left out, and at most five"
    assert out["designs"][0]["url"] == "https://w/spaces/BTS/pages/BTS-8"
    assert "BTS-8 rule" in out["designs"][0]["excerpt"]
    assert 'space = "BTS"' in seen["cql"] and "order by lastmodified desc" in seen["cql"]


def test_the_excerpt_is_what_the_design_promised() -> None:
    text = "Intro\n3. Requirements\nMust reject a blank id.\n4. Current System / Architecture\nX"
    assert confluence_tools._requirements_excerpt(text) == "Requirements Must reject a blank id."


# --- the webhook URLs ----------------------------------------------------------------------


def test_webhook_urls_wait_for_the_platforms_address(monkeypatch) -> None:
    for name in ("ORCH_WEBHOOK_BASE_URL", "ORCH_WEBHOOK_TENANT_ID", "ORCH_WEBHOOK_TENANT"):
        monkeypatch.delenv(name, raising=False)
    status = settings.webhook_status()
    assert status["configured"] is False
    assert "ORCH_WEBHOOK_BASE_URL" in status["missing"]
    assert any(w.startswith("Confluence Automation rule") for w in status["waiting"])
    assert any(w.startswith("GitHub webhook") for w in status["waiting"])


def test_webhook_urls_once_configured(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_WEBHOOK_BASE_URL", "https://wh.example/webhooks/v2/")
    monkeypatch.setenv("ORCH_WEBHOOK_TENANT_ID", "t-1")
    monkeypatch.setenv("ORCH_WEBHOOK_TENANT", "test")
    assert settings.webhook_urls() == {
        "jira": "https://wh.example/webhooks/v2/jira?tenant_id=t-1&tenant=test",
        "confluence": "https://wh.example/webhooks/v2/confluence?tenant_id=t-1&tenant=test",
        "github": "https://wh.example/webhooks/v2/github?tenant_id=t-1&tenant=test",
    }


async def test_the_health_check_reports_the_webhooks() -> None:
    waiting = {"configured": False, "missing": "ORCH_WEBHOOK_BASE_URL", "waiting": ["x"]}
    t = Tools(
        request_settings={"pipeline": SETTINGS, "webhooks": waiting},
        smtp_login_check={"ok": True},
    )

    async def dispatch(agent, payload, **options):
        return {}

    out = await run_health_check(dispatch, t.execute)
    assert out["webhooks"] == waiting


# --- agent identity (2026-10-15) ---------------------------------------------------------


async def test_an_agent_is_only_called_on_its_own_queue(monkeypatch) -> None:
    from agent import router_agent

    calls = []

    async def execute(name, payload, **options):
        calls.append((name, options.get("task_queue")))
        return {}

    monkeypatch.setattr(router_agent.agentExecutor, "execute", execute)
    with pytest.raises(ValueError, match="No task queue is configured for planning_agent"):
        await router_agent._dispatch("planning_agent", {}, workflow_id="w")
    assert calls == [], "never sent by name alone"
    await router_agent._dispatch("planning_agent", {}, task_queue="9322ec14-task-queue")
    assert calls == [("planning_agent", "9322ec14-task-queue")]


async def test_a_missing_queue_shows_in_the_health_check() -> None:
    from agent import router_agent

    t = Tools(request_settings={"pipeline": SETTINGS}, smtp_login_check={"ok": True})
    out = await run_health_check(router_agent._dispatch, t.execute)
    review = out["agents"]["JiraRequirementReview"]
    assert review["reachable"] is False
    assert "No task queue is configured for JiraRequirementReview" in review["error"]
