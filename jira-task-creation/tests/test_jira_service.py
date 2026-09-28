"""Jira creation: hierarchy, linking, safety refusals, partial failure, idempotency.

Every test here runs against the FakeJira mock transport in ``conftest.py``.
No test can reach a real Jira instance.
"""

from __future__ import annotations

import pytest

from src.jira import api as jira
from src.models.schemas import ExistingIssue, ResultStatus, WorkBreakdown
from src.tools.tools import create_jira_issues, jira_configuration_status
from tests.test_schemas import make_epic, make_story, make_subtask

REQUIREMENT = "Send email and SMS notifications and let users manage preferences."


def small_breakdown() -> dict:
    return WorkBreakdown(
        classification="Small",
        analysis="A single focused change to one account setting.",
        stories=[
            make_story(
                title="Update preferred language",
                subtasks=[
                    make_subtask(title="Agree the supported language list"),
                    make_subtask(title="Verify the language choice persists"),
                ],
            )
        ],
    ).model_dump(mode="json")


def medium_breakdown() -> dict:
    return WorkBreakdown(
        classification="Medium",
        analysis="Three related notification capabilities.",
        epic=make_epic(),
        stories=[
            make_story(title="Email notifications", subtasks=[make_subtask()]),
            make_story(title="SMS notifications", subtasks=[make_subtask()]),
            make_story(
                title="Notification preferences",
                subtasks=[make_subtask(), make_subtask()],
            ),
        ],
    ).model_dump(mode="json")


# --- safety refusals: nothing may be created ------------------------------


async def test_no_jira_call_without_a_breakdown(fake_jira, jira_env):
    fake = fake_jira()
    result = await create_jira_issues(breakdown=None, requirement=REQUIREMENT)
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert fake.requests == []


async def test_no_jira_call_when_the_breakdown_fails_schema_validation(fake_jira, jira_env):
    fake = fake_jira()
    broken = small_breakdown()
    broken["stories"][0]["priority"] = "Whenever"
    result = await create_jira_issues(breakdown=broken, requirement=REQUIREMENT)
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "schema validation" in result["failure_reason"]
    assert fake.requests == []


async def test_missing_credentials_refuse_before_any_request(fake_jira, monkeypatch):
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ABC")
    fake = fake_jira()
    result = await create_jira_issues(breakdown=small_breakdown(), requirement=REQUIREMENT)
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "JIRA_BASE_URL" in result["failure_reason"]
    assert fake.requests == []
    assert result["created_keys"] == []


async def test_missing_project_key_refuses(fake_jira, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "svc@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    fake = fake_jira()
    result = await create_jira_issues(breakdown=small_breakdown(), requirement=REQUIREMENT)
    assert "project key" in result["failure_reason"]
    assert fake.requests == []


async def test_invalid_project_key_is_reported_and_creates_nothing(fake_jira, jira_env):
    fake = fake_jira(createmeta_status=404)
    result = await create_jira_issues(
        breakdown=small_breakdown(), requirement=REQUIREMENT, project_key="NOPE"
    )
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "was not found" in result["failure_reason"]
    assert fake.created == []


async def test_authentication_failure_is_reported_and_creates_nothing(fake_jira, jira_env):
    fake = fake_jira(createmeta_status=401)
    result = await create_jira_issues(breakdown=small_breakdown(), requirement=REQUIREMENT)
    assert "authentication failed" in result["failure_reason"]
    assert fake.created == []


async def test_missing_issue_type_refuses_before_creating_anything(fake_jira, jira_env):
    fake = fake_jira(issue_types=["Story", "Task"])
    result = await create_jira_issues(breakdown=small_breakdown(), requirement=REQUIREMENT)
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "Sub-task" in result["failure_reason"]
    assert fake.created == []


async def test_a_failing_duplicate_check_stops_creation(fake_jira, jira_env):
    fake = fake_jira(search_status=500)
    result = await create_jira_issues(breakdown=small_breakdown(), requirement=REQUIREMENT)
    assert "Duplicate check failed" in result["failure_reason"]
    assert fake.created == []


# --- happy paths ----------------------------------------------------------


async def test_small_requirement_creates_a_story_and_its_subtasks_but_no_epic(fake_jira, jira_env):
    fake = fake_jira()
    result = await create_jira_issues(breakdown=small_breakdown(), requirement=REQUIREMENT)

    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert result["epic"] is None
    assert fake.summaries_by_type("Epic") == []
    assert len(result["stories"]) == 1
    assert len(result["subtasks"]) == 2
    assert all(s["parent_key"] == result["stories"][0]["key"] for s in result["subtasks"])


async def test_medium_requirement_creates_the_epic_first(fake_jira, jira_env):
    fake = fake_jira()
    result = await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)

    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert fake.created[0]["fields"]["issuetype"]["name"] == "Epic"
    assert result["epic"]["key"] == fake.created[0]["key"]


async def test_stories_are_linked_to_the_created_epic(fake_jira, jira_env):
    fake_jira()
    result = await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)
    epic_key = result["epic"]["key"]
    assert len(result["stories"]) == 3
    assert all(story["parent_key"] == epic_key for story in result["stories"])


async def test_subtasks_are_parented_to_their_own_story(fake_jira, jira_env):
    fake_jira()
    result = await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)
    story_keys = {s["key"] for s in result["stories"]}
    assert len(result["subtasks"]) == 4
    assert all(sub["parent_key"] in story_keys for sub in result["subtasks"])


async def test_created_keys_and_urls_come_from_jira(fake_jira, jira_env):
    fake = fake_jira()
    result = await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)
    real_keys = [c["key"] for c in fake.created]
    assert result["created_keys"] == real_keys
    for ref in [result["epic"], *result["stories"], *result["subtasks"]]:
        assert ref["url"] == f"https://example.atlassian.net/browse/{ref['key']}"
        assert ref["id"]


async def test_summary_counts_the_created_hierarchy(fake_jira, jira_env):
    fake_jira()
    result = await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)
    assert result["summary"] == "1 Epic, 3 Stories, and 4 Subtasks created successfully."


async def test_epic_description_carries_only_the_six_business_fields(fake_jira, jira_env):
    fake = fake_jira()
    await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)
    epic_doc = fake.created[0]["fields"]["description"]
    headings = [
        block["content"][0]["text"] for block in epic_doc["content"] if block["type"] == "heading"
    ]
    assert headings == [
        "Business objective",
        "Scope",
        "Out-of-scope items",
        "Priority",
        "Acceptance criteria",
        "Related user stories",
    ]


async def test_story_description_carries_the_nine_field_contract(fake_jira, jira_env):
    fake = fake_jira()
    await create_jira_issues(breakdown=small_breakdown(), requirement=REQUIREMENT)
    story = next(c for c in fake.created if c["fields"]["issuetype"]["name"] == "Story")
    headings = [
        block["content"][0]["text"]
        for block in story["fields"]["description"]["content"]
        if block["type"] == "heading"
    ]
    assert headings == [
        "User story",
        "Description",
        "Business value",
        "Priority",
        "Estimated complexity",
        "Dependencies",
        "Acceptance criteria",
        "Related subtasks",
    ]


# --- epic-link fallback ---------------------------------------------------


async def test_falls_back_to_the_epic_link_field_when_parent_is_rejected(fake_jira, jira_env):
    fake = fake_jira(reject_inline_parent=True)
    result = await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)

    assert result["status"] == ResultStatus.JIRA_CREATED.value
    epic_key = result["epic"]["key"]
    assert {link[1] for link in fake.links} == {epic_key}
    assert all(story["parent_key"] == epic_key for story in result["stories"])


async def test_a_pinned_epic_link_field_id_is_used(monkeypatch, fake_jira, jira_env):
    monkeypatch.setenv("JIRA_EPIC_LINK_FIELD_ID", "customfield_99999")
    fake = fake_jira(reject_inline_parent=True)
    await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)
    assert ("GET", "/rest/api/3/field") not in fake.requests


# --- partial failure ------------------------------------------------------


async def test_partial_failure_reports_what_was_created_and_what_failed(fake_jira, jira_env):
    fake = fake_jira(fail_on_summary="Notification preferences")
    result = await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)

    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert result["epic"]["key"]
    assert len(result["stories"]) == 2
    assert "Notification preferences" in result["failed_issue"]
    assert result["failure_reason"]
    assert result["retry_safe"] is True
    assert result["created_keys"] == [c["key"] for c in fake.created]


async def test_epic_failure_leaves_nothing_behind(fake_jira, jira_env):
    fake = fake_jira(fail_on_summary="Notification channels")
    result = await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert result["created_keys"] == []
    assert fake.created == []


async def test_subtask_failure_stops_the_run(fake_jira, jira_env):
    breakdown = small_breakdown()
    fake_jira(fail_on_summary="Verify the language choice persists")
    result = await create_jira_issues(breakdown=breakdown, requirement=REQUIREMENT)
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert len(result["stories"]) == 1
    assert len(result["subtasks"]) == 1


# --- idempotency ----------------------------------------------------------


def test_idempotency_key_is_stable_for_the_same_requirement():
    assert jira.idempotency_key("Add SSO login", "ABC") == jira.idempotency_key(
        "  add   sso   LOGIN  ", "ABC"
    )


def test_idempotency_key_differs_per_requirement_and_project():
    assert jira.idempotency_key("A requirement", "ABC") != jira.idempotency_key(
        "A different requirement", "ABC"
    )
    assert jira.idempotency_key("A requirement", "ABC") != jira.idempotency_key(
        "A requirement", "XYZ"
    )


def test_a_supplied_correlation_id_wins():
    assert jira.idempotency_key("one", "ABC", "corr-1") == jira.idempotency_key(
        "two", "XYZ", "corr-1"
    )


async def test_every_created_issue_carries_the_idempotency_label(fake_jira, jira_env):
    fake = fake_jira()
    result = await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)
    label = result["idempotency_key"]
    assert label.startswith("ltw-")
    assert all(label in c["fields"]["labels"] for c in fake.created)


async def test_a_retry_reuses_the_existing_issues_instead_of_duplicating(fake_jira, jira_env):
    fake = fake_jira()
    first = await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)
    created_after_first = len(fake.created)

    second = await create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)

    assert len(fake.created) == created_after_first, "a retry must not create more issues"
    assert second["status"] == ResultStatus.JIRA_CREATED.value
    assert second["reused_existing"] is True
    # BUG C: a run that created nothing must report no created keys.
    assert second["created_keys"] == [], "reuse created nothing, so created_keys must be empty"
    assert sorted(second["reused_keys"]) == sorted(first["created_keys"])
    assert second["epic_reused"] is True, "an epic that already existed was not created now"
    assert second["epic"]["key"] == first["epic"]["key"]
    assert len(second["stories"]) == 3
    assert len(second["subtasks"]) == 4


async def test_a_different_requirement_is_not_treated_as_a_duplicate(fake_jira, jira_env):
    fake_jira()
    await create_jira_issues(breakdown=small_breakdown(), requirement="Requirement one here.")
    result = await create_jira_issues(
        breakdown=small_breakdown(), requirement="A completely different requirement."
    )
    assert result["reused_existing"] is False
    assert result["status"] == ResultStatus.JIRA_CREATED.value


# --- configuration status -------------------------------------------------


async def test_configuration_status_reports_not_ready_without_credentials():
    status = await jira_configuration_status()
    assert status["ready"] is False
    assert status["token_set"] is False


async def test_configuration_status_reports_ready(jira_env):
    status = await jira_configuration_status()
    assert status["ready"] is True
    assert status["project_key"] == "ABC"
    assert status["issue_types"]["subtask"] == "Sub-task"


# --- attaching sub-tasks to a ticket, twice ---------------------------------
# Live defect on FL-53: "please break this down into stories" was asked three
# times in three comments and produced NINE sub-tasks, three of each. The
# idempotency label is keyed per comment, so each ask got its own label and
# found nothing to reuse; duplicate detection could not help either, because it
# drops Sub-tasks from comparison. Nothing compared against the parent's own
# children, which is the only thing that matters here.


async def test_asking_twice_does_not_put_the_same_subtasks_on_a_ticket_again(fake_jira, jira_env):
    fake = fake_jira()

    first = await create_jira_issues(
        breakdown=small_breakdown(),
        requirement=REQUIREMENT,
        attach_to_key="FL-53",
        correlation_id="FL-53#10293",
    )
    after_first = len(fake.created)
    assert after_first > 0, "the first ask must actually create the sub-tasks"

    # A second, separate comment asking for the same thing.
    second = await create_jira_issues(
        breakdown=small_breakdown(),
        requirement=REQUIREMENT,
        attach_to_key="FL-53",
        correlation_id="FL-53#10310",
    )

    assert len(fake.created) == after_first, "a second ask must not create more sub-tasks"
    assert second["created_keys"] == []
    assert second["reused_existing"] is True
    assert sorted(second["reused_keys"]) == sorted(first["created_keys"])
    assert "already carries" in second["summary"]


async def test_only_the_missing_subtasks_are_added_to_a_ticket(fake_jira, jira_env):
    """A partly-populated ticket gains only what it does not already have."""
    fake = fake_jira()
    await create_jira_issues(
        breakdown=small_breakdown(),
        requirement=REQUIREMENT,
        attach_to_key="FL-53",
        correlation_id="FL-53#1",
    )
    # Drop one child, as a person deleting a sub-task in Jira would. `store` is
    # what the fake's JQL searches, so the issue has to leave it.
    removed = fake.created.pop()
    fake.store = [i for i in fake.store if i.get("key") != removed["key"]]

    result = await create_jira_issues(
        breakdown=small_breakdown(),
        requirement=REQUIREMENT,
        attach_to_key="FL-53",
        correlation_id="FL-53#2",
    )
    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert len(result["created_keys"]) == 1, "only the deleted sub-task is recreated"
    assert "were already there" in result["summary"]


# --- closed work is not a duplicate -----------------------------------------
# AUDIT D2: nothing filtered by status at any layer, so a request matching a
# ticket closed as "Won't Do" was refused as already existing. A decision not
# to build something is not a reason to refuse building it later.


def closed_issue(summary: str, status: str, category: str) -> ExistingIssue:
    return ExistingIssue(
        key="FL-9",
        summary=summary,
        issue_type="Story",
        status=status,
        status_category=category,
        url="https://x/browse/FL-9",
    )


def test_work_closed_as_wont_do_does_not_block_a_new_request() -> None:
    issues = [closed_issue("Log a rest break", "Won't Do", "done")]
    assert jira.comparable_issues(issues) == []
    assert jira.find_overlaps(["Log a rest break"], issues) == []


def test_completed_work_does_not_block_either() -> None:
    issues = [closed_issue("Log a rest break", "Done", "done")]
    assert jira.find_overlaps(["Log a rest break"], issues) == []


@pytest.mark.parametrize("status,category", [("To Do", "new"), ("In Progress", "indeterminate")])
def test_open_work_still_blocks(status: str, category: str) -> None:
    """The counterweight: filtering by status must not disable duplicate checks."""
    issues = [closed_issue("Log a rest break", status, category)]
    matches = jira.find_overlaps(["Log a rest break"], issues)
    assert [m.existing_key for m in matches] == ["FL-9"]


def test_the_status_name_alone_is_not_trusted() -> None:
    """A team can name an open status "Done-ish"; only the category decides."""
    issues = [closed_issue("Log a rest break", "Done reviewing", "indeterminate")]
    assert len(jira.find_overlaps(["Log a rest break"], issues)) == 1


def test_closed_work_can_be_compared_again_on_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTW_MATCH_CLOSED_WORK", "true")
    issues = [closed_issue("Log a rest break", "Won't Do", "done")]
    assert len(jira.find_overlaps(["Log a rest break"], issues)) == 1


# --- duplicate detection that does not depend on wording --------------------
# Two runs of one requirement produced "Allow depot staff to record tyre
# pressure checks" (model) and "Staff record a tyre pressure check" (fallback).
# Neither title route crossed its threshold and FL-120 was created as a
# duplicate of FL-116. The requirement text is the same both times.


def _issue(key: str, summary: str, description: str = "", issue_type: str = "Story"):
    return ExistingIssue(
        key=key,
        summary=summary,
        description=description,
        issue_type=issue_type,
        status="To Do",
        status_category="new",
    )


def test_a_reworded_duplicate_is_caught_by_the_requirement_text() -> None:
    requirement = "Let depot staff record a tyre pressure check."
    existing = [
        _issue(
            "FL-116",
            "Allow depot staff to record tyre pressure checks",
            "As a depot staff member, I want to record tyre pressure checks, so "
            "that I can maintain accurate records of vehicle maintenance.",
        )
    ]
    titles = ["Staff record a tyre pressure check"]

    assert not jira.build_overlap_report(titles, existing).matches

    report = jira.build_overlap_report(titles, existing, requirement=requirement)
    assert [m.existing_key for m in report.matches] == ["FL-116"]
    assert report.matches[0].matched_on == "requirement"
    assert not report.remaining_titles


def test_an_unrelated_requirement_still_gets_built() -> None:
    """The requirement pass must not become a blanket refusal to create work."""
    existing = [_issue("FL-116", "Allow depot staff to record tyre pressure checks")]
    report = jira.build_overlap_report(
        ["Drivers photograph damage"],
        existing,
        requirement="Let drivers photograph damage at handover.",
    )
    assert not report.matches
    assert report.remaining_titles == ["Drivers photograph damage"]


def test_a_title_match_still_wins_when_there_is_one() -> None:
    """The requirement pass is a fallback, not a replacement."""
    existing = [_issue("FL-9", "Drivers photograph damage at handover")]
    report = jira.build_overlap_report(
        ["Drivers photograph damage at handover"],
        existing,
        requirement="something else entirely",
    )
    assert report.matches[0].matched_on == "summary"
