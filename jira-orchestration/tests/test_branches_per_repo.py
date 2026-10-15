"""Each repository on its own branches (2026-10-15): orders on main, payments on develop."""

from __future__ import annotations

from test_commands import PAGE, Agents, command, tools

from agent.command_flow import run_command
from routing import commands
from routing.commands import BranchPlan, parse_code_command
from tools import confluence_tools

REPOS = {"orders": "org/orders", "payments": "org/payments"}
DEFAULTS = {
    "repo": "org/orders",
    "repos": list(REPOS.values()),
    "repo_names": REPOS,
    "source": "main",
    "branch": "feature/SHOP-42-partial-refunds",
    "target": "main",
    "per_repo": {"payments": {"source": "develop", "target": "develop"}},
}


def test_a_plan_names_each_repositorys_own_branches():
    plan = BranchPlan.from_dict(DEFAULTS)
    assert (plan.source_for("orders"), plan.target_for("orders")) == ("main", "main")
    assert (plan.source_for("payments"), plan.target_for("payments")) == ("develop", "develop")
    assert BranchPlan.from_dict(plan.to_dict()) == plan
    assert "per_repo" not in BranchPlan("main", "f", "main").to_dict(), "one repo reads as before"


def test_one_repositorys_branches_can_be_named_in_the_command():
    asked, confirmed = parse_code_command(
        "@Aetherion code confirm payments.source=release/2 payments.target=release/2"
    )
    assert confirmed and (asked.source, asked.target) == ("", "")
    assert asked.per_repo == {"payments": {"source": "release/2", "target": "release/2"}}
    plan = asked.merged_over(BranchPlan.from_dict(DEFAULTS))
    assert plan.source_for("payments") == "release/2" and plan.source_for("orders") == "main"


def test_a_branch_named_for_every_repository_replaces_their_defaults():
    asked, _ = parse_code_command("@Aetherion code source=hotfix target=hotfix")
    plan = asked.merged_over(BranchPlan.from_dict(DEFAULTS))
    assert plan.source_for("payments") == "hotfix" and plan.target_for("orders") == "hotfix"


def test_the_question_shows_one_line_per_repository():
    blocks = commands.branch_question(
        BranchPlan.from_dict(DEFAULTS), "org/orders", "Aetherion", REPOS
    )
    proposed = dict(blocks)["Proposed"]
    assert "orders (org/orders): from main → pull request into main" in proposed
    assert "payments (org/payments): from develop → pull request into develop" in proposed
    assert any("payments.source=develop" in line for line in dict(blocks)["Reply with one of"])


def test_one_repository_asks_as_before():
    blocks = commands.branch_question(
        BranchPlan("main", "f", "main"), "org/bank", "Aetherion", {"bank": "org/bank"}
    )
    assert "Start from (source): main" in dict(blocks)["Proposed"]


async def test_the_confirmed_per_repository_branches_reach_coding():
    t = tools(read_tdd={**PAGE, "code_plan": {}})
    t.answers["branch_defaults"] = DEFAULTS
    agents = Agents()
    await run_command(
        command(
            "CODE",
            "@Aetherion code confirm payments.source=develop payments.target=release",
            t,
            agents,
            page_id="9001",
        )
    )
    branches = agents.payload("coding-agent")["branches"]
    assert branches["source"] == "main" and branches["target"] == "main"
    assert branches["per_repo"] == {"payments": {"source": "develop", "target": "release"}}


async def test_the_defaults_come_from_the_configuration_then_github(monkeypatch):
    monkeypatch.setenv("ORCH_PLANNING_REPOS", "orders: org/orders; payments: org/payments@develop")

    async def default_branch(repo):
        return {"org/orders": "main"}.get(repo, "trunk")

    monkeypatch.setattr(confluence_tools, "_default_branch", default_branch)
    out = await confluence_tools.branch_defaults("SHOP-42", "Partial refunds")
    assert (out["source"], out["target"]) == ("main", "main")
    assert out["per_repo"] == {"payments": {"source": "develop", "target": "develop"}}
    assert out["repo_names"] == REPOS
    assert out["branch"] == "feature/SHOP-42-partial-refunds"
