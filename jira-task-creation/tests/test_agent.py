"""End-to-end pipeline behaviour, driving the agent with a fake toolExecutor.

The real tool functions are called, so the ordering guarantee under test is
real: these assertions fail if anything reaches Jira before validation passes.
"""

from __future__ import annotations

import json

import pytest

from src.agent.agent import JiraTaskCreation
from src.classification import decompose as decomposer
from src.models.schemas import ResultStatus
from src.tools import tools as tool_module
from tests.test_decomposition import VALID_LLM_SMALL

SMALL = "Allow users to update their preferred language."
MEDIUM = "Send email and SMS notifications and allow users to manage notification preferences."
LARGE = (
    "Support mobile registration, OTP verification, login, token refresh, logout, "
    "password recovery, and active session management."
)

TOOLS = {
    "inspect_jira_context": tool_module.inspect_jira_context,
    "validate_requirement": tool_module.validate_requirement,
    "generate_work_breakdown": tool_module.generate_work_breakdown,
    "create_jira_issues": tool_module.create_jira_issues,
    # Webhook-era tools. These runs are manual-mode (no issue_key), so the
    # notifier is unconfigured and the write-back is a no-op, but the agent
    # still dispatches them by name.
    "read_jira_issue": tool_module.read_jira_issue,
    "notify_email": tool_module.notify_email,
    "report_to_issue": tool_module.report_to_issue,
    # Asked before creating: tickets, or a PDF (GENERATE_LOCAL_PDF, off here).
    "delivery_mode": tool_module.delivery_mode,
    "generate_breakdown_pdf": tool_module.generate_breakdown_pdf,
}

# Gate 1 always runs, so every successful pipeline starts with it.
INSPECT = "inspect_jira_context"


@pytest.fixture
def run_agent(monkeypatch):
    """Drive the agent, recording which tools it invoked and in what order."""
    calls: list[str] = []

    async def fake_execute(name: str, *args, **_kwargs):
        calls.append(name)
        return await TOOLS[name](*args)

    monkeypatch.setattr("src.agent.agent.toolExecutor.execute", fake_execute)

    async def _run(payload: dict) -> tuple[dict, list[str]]:
        result = await JiraTaskCreation.fn(payload)
        return result, calls

    return _run


@pytest.fixture(autouse=True)
def _no_gateway(monkeypatch):
    """No AI gateway in tests: the heuristic path is exercised by default."""

    async def _unavailable(_prompt: str) -> str:
        raise RuntimeError("no gateway in tests")

    monkeypatch.setattr(decomposer, "_chat", _unavailable)


# --- Example A: empty ------------------------------------------------------


@pytest.mark.parametrize("requirement", ["", "   ", None])
async def test_empty_input_creates_nothing(run_agent, fake_jira, jira_env, requirement):
    fake = fake_jira()
    result, calls = await run_agent({"requirement": requirement, "project_key": "ABC"})

    assert result["status"] == ResultStatus.VALIDATION_ERROR.value
    assert "provide a project, feature, or business requirement" in result["message"]
    assert calls == [INSPECT, "validate_requirement", "notify_email"]
    assert fake.writes == [], "validation failure must not write to Jira"
    assert fake.created == []
    assert result["jira_result"] is None


# --- Example B: random -----------------------------------------------------


@pytest.mark.parametrize("requirement", ["asdfgh hello xyz", "hello hello hello", "qwerty qwerty"])
async def test_random_input_creates_nothing(run_agent, fake_jira, jira_env, requirement):
    fake = fake_jira()
    result, calls = await run_agent({"requirement": requirement, "project_key": "ABC"})

    assert result["status"] == ResultStatus.VALIDATION_ERROR.value
    assert calls == [INSPECT, "validate_requirement", "notify_email"]
    assert fake.created == []


# --- Example C: unrelated --------------------------------------------------


async def test_unrelated_input_is_out_of_scope(run_agent, fake_jira, jira_env):
    fake = fake_jira()
    result, calls = await run_agent(
        {"requirement": "What is the weather today?", "project_key": "ABC"}
    )

    assert result["status"] == ResultStatus.OUT_OF_SCOPE.value
    assert calls == [INSPECT, "validate_requirement", "notify_email"]
    assert fake.created == []


async def test_out_of_scope_from_the_model_also_creates_nothing(
    run_agent, fake_jira, jira_env, monkeypatch
):
    async def _chat(_prompt: str) -> str:
        return json.dumps(
            {"verdict": "OUT_OF_SCOPE", "reason": "Not a product requirement.", "questions": []}
        )

    monkeypatch.setattr(decomposer, "_chat", _chat)
    fake = fake_jira()
    result, calls = await run_agent(
        {
            "requirement": "Please book me a table for dinner and send the invite.",
            "project_key": "ABC",
        }
    )

    assert result["status"] == ResultStatus.OUT_OF_SCOPE.value
    assert calls == [INSPECT, "validate_requirement", "notify_email"]
    assert fake.created == []


# --- Example D: ambiguous --------------------------------------------------


async def test_ambiguous_requirement_asks_questions_and_creates_nothing(
    run_agent, fake_jira, jira_env, monkeypatch
):
    async def _chat(_prompt: str) -> str:
        return json.dumps(
            {
                "verdict": "CLARIFICATION_REQUIRED",
                "reason": "The item under approval and the approving role are unknown.",
                "questions": [
                    "What type of item is being approved?",
                    "Which role can approve or reject it?",
                    "What should happen after approval or rejection?",
                ],
            }
        )

    monkeypatch.setattr(decomposer, "_chat", _chat)
    fake = fake_jira()
    result, calls = await run_agent(
        {"requirement": "Add approval functionality.", "project_key": "ABC"}
    )

    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert len(result["clarifying_questions"]) == 3
    assert calls == [INSPECT, "validate_requirement", "notify_email"]
    assert fake.created == []


# --- Example E: small ------------------------------------------------------


async def test_small_requirement_creates_a_story_and_subtasks(run_agent, fake_jira, jira_env):
    fake = fake_jira()
    result, calls = await run_agent({"requirement": SMALL, "project_key": "ABC"})

    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert result["classification"] == "Small"
    assert result["epic"] is None
    assert calls == [
        INSPECT,
        "validate_requirement",
        "generate_work_breakdown",
        "delivery_mode",
        "create_jira_issues",
        "notify_email",
    ]
    assert fake.summaries_by_type("Epic") == []
    assert len(result["jira_result"]["stories"]) == 1
    assert result["jira_result"]["subtasks"]


# --- Example F/G: medium and large ----------------------------------------


@pytest.mark.parametrize("requirement,expected", [(MEDIUM, "Medium"), (LARGE, "Large")])
async def test_medium_and_large_create_a_linked_hierarchy(
    run_agent, fake_jira, jira_env, requirement, expected
):
    fake = fake_jira()
    result, _ = await run_agent({"requirement": requirement, "project_key": "ABC"})

    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert result["classification"] == expected

    jira_result = result["jira_result"]
    epic_key = jira_result["epic"]["key"]
    assert epic_key
    assert len(jira_result["stories"]) >= 2
    assert all(story["parent_key"] == epic_key for story in jira_result["stories"])

    story_keys = {s["key"] for s in jira_result["stories"]}
    assert jira_result["subtasks"]
    assert all(sub["parent_key"] in story_keys for sub in jira_result["subtasks"])

    assert fake.created[0]["fields"]["issuetype"]["name"] == "Epic"


async def test_keys_returned_are_the_keys_jira_actually_issued(run_agent, fake_jira, jira_env):
    fake = fake_jira()
    result, _ = await run_agent({"requirement": MEDIUM, "project_key": "ABC"})
    assert result["jira_result"]["created_keys"] == [c["key"] for c in fake.created]


# --- create_in_jira = false -----------------------------------------------


async def test_review_mode_returns_the_breakdown_without_touching_jira(
    run_agent, fake_jira, jira_env
):
    fake = fake_jira()
    result, calls = await run_agent(
        {"requirement": MEDIUM, "project_key": "ABC", "create_in_jira": False}
    )

    assert result["status"] == ResultStatus.READY_FOR_JIRA.value
    assert result["epic"] is not None
    # delivery_mode: with GENERATE_LOCAL_PDF on, the PDF is emailed instead of a preview.
    assert calls == [INSPECT, "validate_requirement", "generate_work_breakdown", "delivery_mode"]
    assert fake.writes == [], "review mode must not write to Jira"


async def test_a_blank_create_in_jira_field_still_defaults_to_creating(
    run_agent, fake_jira, jira_env
):
    fake_jira()
    result, calls = await run_agent(
        {"requirement": SMALL, "project_key": "ABC", "create_in_jira": ""}
    )
    assert "create_jira_issues" in calls
    assert result["status"] == ResultStatus.JIRA_CREATED.value


# --- failures surfaced ----------------------------------------------------


async def test_missing_jira_configuration_is_surfaced_not_swallowed(
    run_agent, fake_jira, monkeypatch
):
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ABC")
    fake = fake_jira()
    result, _ = await run_agent({"requirement": SMALL, "project_key": "ABC"})

    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    # Gate 1 now catches missing credentials before anything is generated,
    # so the run fails earlier than it used to — and still writes nothing.
    assert "JIRA_BASE_URL" in result["message"]
    assert result["classification"] is None
    assert fake.requests == []


async def test_partial_failure_is_reported_with_retry_guidance(run_agent, fake_jira, jira_env):
    fake_jira(fail_on_summary="Verify and hand over")
    result, _ = await run_agent({"requirement": MEDIUM, "project_key": "ABC"})

    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert result["jira_result"]["failed_issue"]
    assert result["jira_result"]["created_keys"]
    assert "rather than creating duplicates" in result["message"]


async def test_rerunning_the_same_requirement_does_not_duplicate(run_agent, fake_jira, jira_env):
    fake = fake_jira()
    first, _ = await run_agent({"requirement": MEDIUM, "project_key": "ABC"})
    after_first = len(fake.created)

    second, _ = await run_agent({"requirement": MEDIUM, "project_key": "ABC"})

    assert len(fake.created) == after_first
    jr = second["jira_result"]
    assert jr["reused_existing"] is True
    assert jr["created_keys"] == [], "reuse must not report creations"
    assert sorted(jr["reused_keys"]) == sorted(first["jira_result"]["created_keys"])
    assert second["overview"]["created_in_jira"] is False
    assert "(created)" not in second["summary_text"]


# --- content guarantees ----------------------------------------------------


async def test_generated_subtasks_never_recommend_source_files(run_agent, fake_jira, jira_env):
    fake_jira()
    result, _ = await run_agent(
        {"requirement": LARGE, "project_key": "ABC", "create_in_jira": False}
    )
    for story in result["stories"]:
        for subtask in story["subtasks"]:
            blob = json.dumps(subtask).lower()
            for suffix in (".py", ".java", ".ts", ".tsx", ".go", ".rb"):
                assert suffix not in blob


async def test_out_of_scope_items_are_not_invented(run_agent, fake_jira, jira_env):
    fake_jira()
    result, _ = await run_agent(
        {"requirement": MEDIUM, "project_key": "ABC", "create_in_jira": False}
    )
    assert result["epic"]["out_of_scope"] == ["Not specified"]


async def test_the_llm_path_is_used_when_the_gateway_answers(
    run_agent, fake_jira, jira_env, monkeypatch
):
    async def _chat(prompt: str) -> str:
        if "TEXT TO TRIAGE" in prompt:
            return json.dumps({"verdict": "VALID", "reason": "ok", "questions": []})
        return json.dumps(VALID_LLM_SMALL)

    monkeypatch.setattr(decomposer, "_chat", _chat)
    fake_jira()
    result, _ = await run_agent({"requirement": SMALL, "project_key": "ABC"})

    assert result["generator"] == "llm"
    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert result["stories"][0]["title"] == "Update preferred language"


async def test_a_model_response_failing_validation_never_reaches_jira(
    run_agent, fake_jira, jira_env, monkeypatch
):
    bad = json.loads(json.dumps(VALID_LLM_SMALL))
    bad["stories"][0]["subtasks"][0]["description"] = "Modify auth_service.py."

    async def _chat(prompt: str) -> str:
        if "TEXT TO TRIAGE" in prompt:
            return json.dumps({"verdict": "VALID", "reason": "ok", "questions": []})
        return json.dumps(bad)

    monkeypatch.setattr(decomposer, "_chat", _chat)
    monkeypatch.setenv("LTW_REQUIRE_LLM", "true")
    fake = fake_jira()
    result, calls = await run_agent({"requirement": SMALL, "project_key": "ABC"})

    # A system failure, not "not a work requirement" (see FLAWS.md F34).
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "create_jira_issues" not in calls
    assert fake.created == []


# ==========================================================================
# Existing epic, sprint placement, and overlap — end to end
# ==========================================================================


async def test_stories_attach_to_an_existing_epic_without_creating_one(
    run_agent, fake_jira, jira_env
):
    fake = fake_jira(
        epics={"ABC-50": {"issuetype": "Epic", "project": "ABC", "summary": "Notifications"}}
    )
    result, _ = await run_agent(
        {"requirement": MEDIUM, "project_key": "ABC", "existing_epic_key": "ABC-50"}
    )

    assert result["status"] == ResultStatus.JIRA_CREATED.value
    jr = result["jira_result"]
    assert jr["epic_reused"] is True
    assert jr["epic"]["key"] == "ABC-50"
    # No Epic was created; every story hangs off the supplied one.
    assert fake.summaries_by_type("Epic") == []
    assert all(s["parent_key"] == "ABC-50" for s in jr["stories"])
    assert "Attached to existing epic ABC-50" in jr["summary"]
    assert "Epic" not in jr["summary"].split(",")[0]


async def test_an_invalid_epic_stops_before_any_generation(run_agent, fake_jira, jira_env):
    fake = fake_jira(epics={})
    result, calls = await run_agent(
        {"requirement": MEDIUM, "project_key": "ABC", "existing_epic_key": "ABC-9999"}
    )
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "was not found" in result["message"]
    assert calls == ["inspect_jira_context", "notify_email"]
    assert fake.created == []


async def test_an_epic_from_another_project_stops_the_run(run_agent, fake_jira, jira_env):
    fake = fake_jira(epics={"OTHER-1": {"issuetype": "Epic", "project": "OTHER"}})
    result, calls = await run_agent(
        {"requirement": MEDIUM, "project_key": "ABC", "existing_epic_key": "OTHER-1"}
    )
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "belongs to project 'OTHER'" in result["message"]
    assert calls == ["inspect_jira_context", "notify_email"]
    assert fake.created == []


async def test_a_non_epic_key_stops_the_run(run_agent, fake_jira, jira_env):
    fake = fake_jira(epics={"ABC-60": {"issuetype": "Story", "project": "ABC"}})
    result, calls = await run_agent(
        {"requirement": MEDIUM, "project_key": "ABC", "existing_epic_key": "ABC-60"}
    )
    assert "is a Story, not an Epic" in result["message"]
    assert calls == ["inspect_jira_context", "notify_email"]
    assert fake.created == []


async def test_stories_are_added_to_the_active_sprint(run_agent, fake_jira, jira_env):
    fake = fake_jira(active_sprints=[{"id": 55, "name": "Sprint 7"}])
    result, _ = await run_agent(
        {"requirement": MEDIUM, "project_key": "ABC", "placement": "current_sprint"}
    )
    jr = result["jira_result"]
    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert jr["placement"] == "current_sprint"
    # Only stories are sent; subtasks follow their parent.
    assert sorted(fake.sprint_added) == sorted(s["key"] for s in jr["stories"])
    assert "active sprint" in jr["summary"]


async def test_no_active_sprint_creates_nothing_at_all(run_agent, fake_jira, jira_env):
    fake = fake_jira(active_sprints=[])
    result, calls = await run_agent(
        {"requirement": MEDIUM, "project_key": "ABC", "placement": "current_sprint"}
    )
    # A sprint Jira cannot identify is something the requester can answer, so it
    # is raised as a question rather than reported as a fault. The assertion that
    # matters is unchanged: nothing was created.
    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert "no active sprint" in result["message"]
    assert calls == ["inspect_jira_context", "notify_email"]
    assert fake.created == [], "sprint problems must be caught before creation"


async def test_backlog_is_the_default_placement(run_agent, fake_jira, jira_env):
    fake = fake_jira()
    result, _ = await run_agent({"requirement": SMALL, "project_key": "ABC"})
    assert result["jira_result"]["placement"] == "backlog"
    assert fake.sprint_added == []


async def test_a_failed_sprint_move_does_not_fail_the_created_issues(
    run_agent, fake_jira, jira_env
):
    fake_jira(active_sprints=[{"id": 55, "name": "Sprint 7"}], sprint_add_status=500)
    result, _ = await run_agent(
        {"requirement": SMALL, "project_key": "ABC", "placement": "current_sprint"}
    )
    jr = result["jira_result"]
    assert result["status"] == ResultStatus.JIRA_CREATED.value, "tickets are still valid"
    assert jr["sprint_error"]
    assert "remain in the backlog" in jr["sprint_error"]
    assert jr["created_keys"]


async def test_overlap_with_an_existing_ticket_asks_before_creating(run_agent, fake_jira, jira_env):
    fake = fake_jira()
    fake.store.append(
        {
            "key": "ABC-1",
            "id": "1",
            "labels": [],
            "fields": {
                "summary": "Allow users to update their preferred language",
                "labels": [],
                "issuetype": {"name": "Story", "subtask": False},
                "parent": {},
            },
        }
    )
    result, calls = await run_agent({"requirement": SMALL, "project_key": "ABC"})

    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert result["duplicate_matches"][0]["existing_key"] == "ABC-1"
    assert "ABC-1" in result["message"]
    assert "create_jira_issues" not in calls
    assert fake.created == []


async def test_duplicates_are_never_created_even_when_asked(run_agent, fake_jira, jira_env):
    """Duplicate creation is not switchable: the overlap gate always stops.

    ``allow_duplicates`` was removed deliberately — overlapping work is
    reported and emailed, never created — so passing it must change nothing.
    """
    fake = fake_jira()
    fake.store.append(
        {
            "key": "ABC-1",
            "id": "1",
            "labels": [],
            "fields": {
                "summary": "Allow users to update their preferred language",
                "labels": [],
                "issuetype": {"name": "Story", "subtask": False},
                "parent": {},
            },
        }
    )
    result, calls = await run_agent(
        {"requirement": SMALL, "project_key": "ABC", "allow_duplicates": True}
    )
    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert "create_jira_issues" not in calls
    assert "notify_email" in calls, "the overlap must be emailed instead"
    assert result["duplicate_matches"]
    assert fake.created == [], "nothing may be created when the work already exists"


async def test_a_retry_is_not_mistaken_for_a_duplicate(run_agent, fake_jira, jira_env):
    """The idempotency label must win over the fuzzy overlap check."""
    fake = fake_jira()
    first, _ = await run_agent({"requirement": SMALL, "project_key": "ABC"})
    assert first["status"] == ResultStatus.JIRA_CREATED.value
    made = len(fake.created)

    second, _ = await run_agent({"requirement": SMALL, "project_key": "ABC"})
    assert (
        second["status"] == ResultStatus.JIRA_CREATED.value
    ), "a retry must reuse, not stop for confirmation"
    assert second["jira_result"]["reused_existing"] is True
    assert len(fake.created) == made


async def test_the_project_description_from_jira_reaches_the_result(run_agent, fake_jira, jira_env):
    fake_jira(project_description="A grocery delivery platform.")
    result, _ = await run_agent(
        {"requirement": SMALL, "project_key": "ABC", "create_in_jira": False}
    )
    assert result["jira_context"]["project"]["description"] == "A grocery delivery platform."


# ==========================================================================
# The flat summary views
# ==========================================================================


async def test_summary_is_present_on_a_rejection(run_agent, fake_jira, jira_env):
    fake_jira()
    result, _ = await run_agent({"requirement": "", "project_key": "ABC"})
    assert result["overview"]["status"] == "VALIDATION_ERROR"
    assert result["overview"]["created_in_jira"] is False
    assert "Rejected" in result["overview"]["outcome"]
    assert "Created in Jira: no" in result["summary_text"]


async def test_summary_lists_the_created_keys(run_agent, fake_jira, jira_env):
    fake_jira()
    result, _ = await run_agent({"requirement": MEDIUM, "project_key": "ABC"})
    ov = result["overview"]
    assert ov["created_in_jira"] is True
    assert ov["epic_key"] == result["jira_result"]["epic"]["key"]
    # "Send email and SMS notifications" is one capability, not two: the
    # fragment after "and" starts with a noun, so it continues the phrase.
    # The old splitter produced a story literally titled "Send email".
    assert ov["story_count"] == 2
    assert ov["subtask_count"] == 6
    assert ov["story_keys"] == [s["key"] for s in result["jira_result"]["stories"]]
    assert "Epic           :" in result["summary_text"]
    assert "Stories (2, created)" in result["summary_text"]


async def test_summary_marks_a_reused_epic(run_agent, fake_jira, jira_env):
    fake_jira(epics={"ABC-50": {"issuetype": "Epic", "project": "ABC"}})
    result, _ = await run_agent(
        {"requirement": MEDIUM, "project_key": "ABC", "existing_epic_key": "ABC-50"}
    )
    assert result["overview"]["epic_reused"] is True
    assert "ABC-50 (reused)" in result["summary_text"]


async def test_summary_describes_a_preview_without_claiming_creation(
    run_agent, fake_jira, jira_env
):
    fake_jira()
    result, _ = await run_agent(
        {"requirement": MEDIUM, "project_key": "ABC", "create_in_jira": False}
    )
    ov = result["overview"]
    assert ov["created_in_jira"] is False
    assert ov["proposed_stories"] == 2
    assert ov["proposed_epic"] is True
    assert "Proposed       :" in result["summary_text"]


async def test_summary_carries_clarifying_questions(run_agent, fake_jira, jira_env):
    fake = fake_jira()
    fake.store.append(
        {
            "key": "ABC-1",
            "id": "1",
            "labels": [],
            "fields": {
                "summary": "Allow users to update their preferred language",
                "labels": [],
                "issuetype": {"name": "Story", "subtask": False},
                "parent": {},
            },
        }
    )
    result, _ = await run_agent({"requirement": SMALL, "project_key": "ABC"})
    assert "Question       :" in result["summary_text"]
    assert "Possible dupe  : ABC-1" in result["summary_text"]


async def test_summary_pluralises_a_single_story(run_agent, fake_jira, jira_env):
    fake_jira()
    result, _ = await run_agent(
        {"requirement": SMALL, "project_key": "ABC", "create_in_jira": False}
    )
    assert "1 Story, 3 Subtasks" in result["summary_text"]
