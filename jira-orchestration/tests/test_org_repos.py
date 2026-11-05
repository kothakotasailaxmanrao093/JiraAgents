"""Every repository of the organization is designed and coded against (2026-10-16).

A repository created in AetherionAgentDemo is part of the next design, with no
setting to change. Configured lines keep their names; archived and empty
repositories are left out; GitHub being down never stops a design.
"""

from __future__ import annotations

import httpx
import pytest

from routing import github_events as gh
from tools import org_repos

BANK = "bank: AetherionAgentDemo/bank-transfer-service"


def repo(name: str, *, size: int = 10, archived: bool = False) -> dict:
    return {
        "name": name,
        "full_name": f"AetherionAgentDemo/{name}",
        "size": size,
        "archived": archived,
    }


def test_the_organization_is_added_after_the_configured_services() -> None:
    text = gh.with_org_repos(
        BANK, [repo("bank-transfer-service"), repo("payments-service"), repo("FirstRepo")]
    )
    assert text.splitlines() == [
        BANK,  # the configured name — never a second name for the same code
        "FirstRepo: AetherionAgentDemo/FirstRepo",
        "payments-service: AetherionAgentDemo/payments-service",
    ]


def test_archived_and_empty_repositories_are_left_out() -> None:
    text = gh.with_org_repos("", [repo("old", archived=True), repo("blank", size=0), repo("live")])
    assert text == "live: AetherionAgentDemo/live"


def test_a_name_already_used_gets_the_full_slug() -> None:
    text = gh.with_org_repos("payments: other/payments", [repo("payments")])
    assert text.splitlines()[1] == "AetherionAgentDemo-payments: AetherionAgentDemo/payments"


@pytest.fixture
def github(monkeypatch):
    calls: list[str] = []
    listing = {"repos": [repo("bank-transfer-service"), repo("payments-service")]}

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if listing.get("fail"):
            return httpx.Response(503, json={})
        return httpx.Response(200, json=listing["repos"])

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr(org_repos.httpx, "AsyncClient", client)
    monkeypatch.setattr(org_repos, "_cache", {"at": 0.0, "org": "", "repos": []})
    monkeypatch.setenv("ORCH_PLANNING_ORG_REPOS", "true")
    monkeypatch.setenv("ORCH_GITHUB_ORG", "AetherionAgentDemo")
    monkeypatch.setenv("ORCH_PLANNING_REPOS", BANK)
    monkeypatch.delenv("ORCH_GITHUB_REPOS", raising=False)
    return calls, listing


async def test_a_new_repository_is_used_without_changing_settings(github) -> None:
    calls, _ = github
    text = await org_repos.planning_repos()
    assert "payments-service: AetherionAgentDemo/payments-service" in text
    assert calls == ["/orgs/AetherionAgentDemo/repos"]
    await org_repos.planning_repos()
    assert len(calls) == 1, "read once for the next few minutes"


async def test_github_down_falls_back_to_the_configured_list(github) -> None:
    _, listing = github
    listing["fail"] = True
    assert await org_repos.planning_repos() == BANK


async def test_switched_off_it_is_the_configured_list_alone(github, monkeypatch) -> None:
    calls, _ = github
    monkeypatch.setenv("ORCH_PLANNING_ORG_REPOS", "false")
    assert await org_repos.planning_repos() == BANK and calls == []


async def test_the_commands_are_given_the_whole_list(github) -> None:
    from routing import settings

    values = await org_repos.with_repos(settings.commands())
    assert values["repos"].splitlines() == [
        BANK,
        "payments-service: AetherionAgentDemo/payments-service",
    ]


def test_an_idle_or_archived_repository_is_left_out():
    """keystone/client-ui, idle since 2022, matched "Client UI" before keystone/ui."""
    from datetime import UTC, datetime

    from tools.org_repos import active

    now = datetime(2026, 11, 3, tzinfo=UTC)
    repos = [
        {"name": "ui", "pushed_at": "2026-11-02T11:18:02Z"},
        {"name": "client-ui", "pushed_at": "2022-02-09T10:00:00Z"},
        {"name": "old", "pushed_at": "2026-10-01T00:00:00Z", "archived": True},
        {"name": "new", "pushed_at": None},
    ]
    assert [r["name"] for r in active(repos, 365, now)] == ["ui", "new"]
    assert len(active(repos, 0, now)) == 4
