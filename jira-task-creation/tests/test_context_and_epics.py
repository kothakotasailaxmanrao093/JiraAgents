"""Gate 1: project context, existing-epic validation, sprint resolution, overlap."""

from __future__ import annotations

import pytest

from src.classification.validate import ProjectContext
from src.jira import api as jira
from src.tools.tools import inspect_jira_context

# --- project context read from Jira ---------------------------------------


async def test_project_description_comes_from_jira_not_the_form(fake_jira, jira_env):
    fake_jira(project_description="A grocery delivery platform for households.")
    ctx = await inspect_jira_context(project_key="ABC")
    assert ctx["ok"] is True
    assert ctx["project"]["description"] == "A grocery delivery platform for households."
    assert ctx["project"]["name"] == "Demo Project"


async def test_existing_tickets_are_read_for_context(fake_jira, jira_env):
    fake = fake_jira()
    fake.store.append(
        {
            "key": "ABC-1",
            "id": "1",
            "labels": [],
            "fields": {
                "summary": "Existing wishlist work",
                "labels": [],
                "issuetype": {"name": "Story", "subtask": False},
                "parent": {},
            },
        }
    )
    ctx = await inspect_jira_context(project_key="ABC")
    assert [i["summary"] for i in ctx["existing_issues"]] == ["Existing wishlist work"]


async def test_context_feeds_the_prompt_with_project_and_tickets(fake_jira, jira_env):
    fake = fake_jira(project_description="Online store.")
    fake.store.append(
        {
            "key": "ABC-1",
            "id": "1",
            "labels": [],
            "fields": {
                "summary": "Wishlist",
                "labels": [],
                "issuetype": {"name": "Story", "subtask": False},
                "parent": {},
            },
        }
    )
    ctx = await inspect_jira_context(project_key="ABC")
    context = ProjectContext.from_jira(ctx["project"], ctx["existing_issues"])
    assert "Online store." in context.as_prompt_text()
    # Existing tickets reach the breakdown as context, never triage (BGV-15).
    assert "Wishlist" in context.as_prompt_text(for_breakdown=True)
    assert "Wishlist" not in context.as_prompt_text()


# --- project validation ----------------------------------------------------


async def test_missing_project_key_is_refused(fake_jira, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "a@b.c")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    fake = fake_jira()
    ctx = await inspect_jira_context(project_key="")
    assert ctx["ok"] is False
    assert "project key" in ctx["error"]
    assert fake.requests == []


async def test_missing_credentials_are_refused(fake_jira):
    fake = fake_jira()
    ctx = await inspect_jira_context(project_key="ABC")
    assert ctx["ok"] is False
    assert "JIRA_BASE_URL" in ctx["error"]
    assert fake.requests == []


async def test_unknown_project_is_refused(fake_jira, jira_env):
    fake_jira(project_status=404)
    ctx = await inspect_jira_context(project_key="NOPE")
    assert ctx["ok"] is False
    assert "was not found" in ctx["error"]


@pytest.mark.parametrize("code", [401, 403])
async def test_access_denied_is_refused(fake_jira, jira_env, code):
    fake_jira(project_status=code)
    ctx = await inspect_jira_context(project_key="ABC")
    assert ctx["ok"] is False
    assert "authentication failed or access is denied" in ctx["error"]


# --- existing epic ---------------------------------------------------------


async def test_valid_existing_epic_is_accepted(fake_jira, jira_env):
    fake = fake_jira(
        epics={"ABC-50": {"issuetype": "Epic", "project": "ABC", "summary": "Notifications"}}
    )
    fake.store.append(
        {
            "key": "ABC-51",
            "id": "51",
            "labels": [],
            "fields": {
                "summary": "Email notifications",
                "labels": [],
                "issuetype": {"name": "Story", "subtask": False},
                "parent": {"key": "ABC-50"},
            },
        }
    )
    ctx = await inspect_jira_context(project_key="ABC", existing_epic_key="ABC-50")
    assert ctx["ok"] is True
    assert ctx["epic"]["key"] == "ABC-50"
    assert ctx["epic"]["summary"] == "Notifications"
    assert [c["key"] for c in ctx["epic"]["child_stories"]] == ["ABC-51"]


async def test_epic_key_is_uppercased(fake_jira, jira_env):
    fake_jira(epics={"ABC-50": {"issuetype": "Epic", "project": "ABC"}})
    ctx = await inspect_jira_context(project_key="ABC", existing_epic_key=" abc-50 ")
    assert ctx["ok"] is True and ctx["epic"]["key"] == "ABC-50"


async def test_nonexistent_epic_is_refused(fake_jira, jira_env):
    fake_jira(epics={})
    ctx = await inspect_jira_context(project_key="ABC", existing_epic_key="ABC-9999")
    assert ctx["ok"] is False
    assert "was not found" in ctx["error"]


async def test_issue_that_is_not_an_epic_is_refused(fake_jira, jira_env):
    fake_jira(epics={"ABC-60": {"issuetype": "Story", "project": "ABC"}})
    ctx = await inspect_jira_context(project_key="ABC", existing_epic_key="ABC-60")
    assert ctx["ok"] is False
    assert "is a Story, not an Epic" in ctx["error"]


async def test_epic_from_another_project_is_refused(fake_jira, jira_env):
    fake_jira(epics={"OTHER-1": {"issuetype": "Epic", "project": "OTHER"}})
    ctx = await inspect_jira_context(project_key="ABC", existing_epic_key="OTHER-1")
    assert ctx["ok"] is False
    assert "belongs to project 'OTHER', not 'ABC'" in ctx["error"]


# --- sprints ---------------------------------------------------------------


async def test_backlog_placement_never_touches_the_sprint_api(fake_jira, jira_env):
    fake = fake_jira()
    ctx = await inspect_jira_context(project_key="ABC", placement="backlog")
    assert ctx["ok"] is True and ctx["sprint"] is None
    assert not any("agile" in p for _, p in fake.requests)


async def test_current_sprint_resolves_the_active_sprint(fake_jira, jira_env):
    fake_jira(active_sprints=[{"id": 55, "name": "Sprint 7"}])
    ctx = await inspect_jira_context(project_key="ABC", placement="current_sprint")
    assert ctx["ok"] is True
    assert ctx["sprint"]["id"] == 55 and ctx["sprint"]["name"] == "Sprint 7"
    assert ctx["placement"] == "current_sprint"


async def test_no_active_sprint_is_refused_before_anything_is_created(fake_jira, jira_env):
    fake = fake_jira(active_sprints=[])
    ctx = await inspect_jira_context(project_key="ABC", placement="current_sprint")
    assert ctx["ok"] is False
    assert "no active sprint" in ctx["error"]
    assert fake.created == []


async def test_multiple_active_sprints_are_refused_rather_than_guessed(fake_jira, jira_env):
    fake_jira(active_sprints=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
    ctx = await inspect_jira_context(project_key="ABC", placement="current_sprint")
    assert ctx["ok"] is False
    assert "2 active sprints" in ctx["error"]


async def test_multiple_boards_are_refused_rather_than_guessed(fake_jira, jira_env):
    fake_jira(boards=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
    ctx = await inspect_jira_context(project_key="ABC", placement="current_sprint")
    assert ctx["ok"] is False
    assert "JIRA_BOARD_ID" in ctx["error"]


async def test_pinned_board_id_skips_board_discovery(monkeypatch, fake_jira, jira_env):
    monkeypatch.setenv("JIRA_BOARD_ID", "77")
    fake = fake_jira(boards=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
    ctx = await inspect_jira_context(project_key="ABC", placement="current_sprint")
    assert ctx["ok"] is True
    assert ctx["sprint"]["board_id"] == 77
    assert ("GET", "/rest/agile/1.0/board") not in fake.requests


async def test_project_without_a_board_is_refused(fake_jira, jira_env):
    fake_jira(boards=[])
    ctx = await inspect_jira_context(project_key="ABC", placement="current_sprint")
    assert ctx["ok"] is False
    assert "has no board" in ctx["error"]


async def test_kanban_board_without_sprints_is_refused(fake_jira, jira_env):
    fake_jira(sprint_supported=False)
    ctx = await inspect_jira_context(project_key="ABC", placement="current_sprint")
    assert ctx["ok"] is False
    assert "does not support sprints" in ctx["error"]


# --- overlap detection -----------------------------------------------------


def test_similarity_scores():
    assert jira.similarity("Wishlist for products", "Wishlist for products") == 1.0
    assert jira.similarity("Wishlist for products", "Payment gateway integration") == 0.0


def test_find_overlaps_flags_near_identical_titles():
    from src.models.schemas import ExistingIssue

    existing = [
        ExistingIssue(key="ABC-1", summary="Allow customers to save products to a wishlist")
    ]
    matches = jira.find_overlaps(["Allow customers to save products to a wishlist"], existing)
    assert len(matches) == 1
    assert matches[0].existing_key == "ABC-1"
    assert matches[0].score == 1.0


def test_find_overlaps_ignores_unrelated_work():
    from src.models.schemas import ExistingIssue

    existing = [ExistingIssue(key="ABC-1", summary="Payment gateway integration")]
    assert jira.find_overlaps(["Allow customers to save products to a wishlist"], existing) == []


def test_sibling_capabilities_are_not_flagged_as_duplicates():
    """'Send email' and 'Send SMS' are different work, not a reword of each other."""
    from src.models.schemas import ExistingIssue

    existing = [ExistingIssue(key="ABC-1", summary="Send email notifications to customers")]
    assert jira.find_overlaps(["Send SMS notifications to customers"], existing) == []


def test_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv("LTW_DUPLICATE_THRESHOLD", "0.1")
    assert jira.duplicate_threshold() == 0.1
    monkeypatch.setenv("LTW_DUPLICATE_THRESHOLD", "not-a-number")
    assert jira.duplicate_threshold() == 0.75
