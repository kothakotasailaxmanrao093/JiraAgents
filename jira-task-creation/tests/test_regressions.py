"""Regression tests for bugs found in the manual test run against project TT.

Every test here drives the agent through ``JiraTaskCreation.fn(payload)`` with
a real payload dict, so the payload wiring is exercised — not just the tools in
isolation. That layer is what the original suite missed.
"""

from __future__ import annotations

import pytest

from src.agent.agent import JiraTaskCreation
from src.classification import decompose as decomposer
from src.jira import api as jira
from src.models.schemas import ResultStatus
from src.tools import tools as tool_module

# One map of tool names, shared with test_agent.py so a new tool is added once.
from tests.test_agent import TOOLS  # noqa: E402


@pytest.fixture
def run_agent(monkeypatch):
    """Drive the real agent with a real payload dict."""
    calls: list[str] = []

    async def fake_execute(name: str, *args, **_kwargs):
        calls.append(name)
        return await TOOLS[name](*args)

    monkeypatch.setattr("src.agent.agent.toolExecutor.execute", fake_execute)

    async def _run(payload: dict):
        return await JiraTaskCreation.fn(payload), calls

    return _run


@pytest.fixture(autouse=True)
def _no_gateway(monkeypatch):
    """Gateway off, matching the conditions of the manual run."""

    async def _unavailable(_prompt: str) -> str:
        raise RuntimeError("no gateway")

    monkeypatch.setattr(decomposer, "_chat", _unavailable)


def seed_story(fake, key: str, summary: str, issue_type: str = "Story") -> None:
    fake.store.append(
        {
            "key": key,
            "id": key.split("-")[1],
            "labels": [],
            "fields": {
                "summary": summary,
                "labels": [],
                "issuetype": {"name": issue_type, "subtask": issue_type != "Story"},
                "parent": {},
            },
        }
    )


# ==========================================================================
# BUG B — duplicate detection never fired
# ==========================================================================


def test_bug_b_pronoun_reword_now_scores_above_threshold():
    """'them' vs 'tenants' was the only differing token, and it sank the score."""
    score = jira.similarity(
        "Let tenants view their payment history",
        "Let them view their payment history",
    )
    assert (
        score >= jira.duplicate_threshold()
    ), f"pronoun-only reword scored {score}, below {jira.duplicate_threshold()}"


def test_bug_b_sibling_capabilities_still_do_not_match():
    """The fix must not start blocking genuinely different work."""
    score = jira.similarity(
        "Send email notifications to tenants", "Send SMS notifications to tenants"
    )
    assert (
        score < jira.duplicate_threshold()
    ), f"sibling capabilities scored {score} and would be wrongly blocked"


def test_bug_b_heavy_reword_is_still_missed_by_design():
    """Documented word-overlap limitation. Preserved deliberately."""
    score = jira.similarity(
        "Let renters see a record of what they have paid",
        "Let them view their payment history",
    )
    assert score < jira.duplicate_threshold()


async def test_bug_b_agent_blocks_the_reworded_duplicate(run_agent, fake_jira, jira_env):
    """The live reproduction, through the agent's payload wiring."""
    fake = fake_jira()
    seed_story(fake, "ABC-14", "Let them view their payment history")

    result, calls = await run_agent(
        {
            "requirement": "Let tenants view their payment history.",
            "project_key": "ABC",
            "create_in_jira": True,
            "allow_duplicates": False,
        }
    )

    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert result["duplicate_matches"], "the duplicate must be reported"
    assert result["duplicate_matches"][0]["existing_key"] == "ABC-14"
    assert "create_jira_issues" not in calls
    assert fake.created == [], "nothing may be created when a duplicate is found"


async def test_bug_b_the_overlap_gate_cannot_be_switched_off(run_agent, fake_jira, jira_env):
    """An overlap always blocks creation and is emailed instead.

    The old ``allow_duplicates`` escape hatch was removed on purpose: a
    duplicate must be notified, never created. Passing the old key has no
    effect, which is what this asserts.
    """
    fake = fake_jira()
    seed_story(fake, "ABC-14", "Let them view their payment history")

    result, calls = await run_agent(
        {
            "requirement": "Let tenants view their payment history.",
            "project_key": "ABC",
            "create_in_jira": True,
            "allow_duplicates": True,
        }
    )

    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert "create_jira_issues" not in calls
    assert "notify_email" in calls
    assert result["duplicate_matches"], "the overlap is reported"
    assert fake.created == [], "and nothing is written"


async def test_bug_b_best_match_reported_even_below_threshold(run_agent, fake_jira, jira_env):
    """An empty duplicate_matches must not hide a near miss."""
    fake = fake_jira()
    seed_story(fake, "ABC-20", "Send email notifications to tenants")

    result, _ = await run_agent(
        {
            "requirement": "Send SMS notifications to tenants.",
            "project_key": "ABC",
            "create_in_jira": False,
        }
    )

    assert result["duplicate_matches"] == [], "sibling must not be blocked"
    assert result["best_match"] is not None, "the near miss must still be visible"
    assert result["best_match"]["existing_key"] == "ABC-20"
    assert 0 < result["best_match"]["score"] < jira.duplicate_threshold()


async def test_bug_b_subtasks_are_excluded_from_scoring(run_agent, fake_jira, jira_env):
    """Sub-task summaries carry long prefixes and must not be scored against."""
    fake = fake_jira()
    seed_story(
        fake,
        "ABC-99",
        "Define the rules for: Let them view their payment history",
        issue_type="Sub-task",
    )

    result, _ = await run_agent(
        {
            "requirement": "Let tenants view their payment history.",
            "project_key": "ABC",
            "create_in_jira": False,
        }
    )

    keys = [m["existing_key"] for m in result["duplicate_matches"]]
    assert "ABC-99" not in keys
    if result["best_match"]:
        assert result["best_match"]["existing_key"] != "ABC-99"


# ==========================================================================
# BUG C — the reuse path reported work it did not do
# ==========================================================================


async def test_bug_c_reuse_reports_no_creations(run_agent, fake_jira, jira_env):
    """A reuse run created nothing, so it must claim nothing."""
    fake = fake_jira()
    payload = {
        "requirement": (
            "Send rent reminder emails, allow tenants to raise a maintenance "
            "request, and let them view their payment history."
        ),
        "project_key": "ABC",
        "create_in_jira": True,
        "allow_duplicates": True,
    }

    first, _ = await run_agent(payload)
    assert first["status"] == ResultStatus.JIRA_CREATED.value
    made = len(fake.created)

    second, _ = await run_agent(payload)
    jr = second["jira_result"]

    assert len(fake.created) == made, "the retry must not create anything"
    assert jr["reused_existing"] is True
    # 1 — created_keys must be empty
    assert jr["created_keys"] == []
    assert sorted(jr["reused_keys"]) == sorted(first["jira_result"]["created_keys"])
    # 2 — summary_text must not say "(created)"
    assert "(created)" not in second["summary_text"]
    assert "(reused)" in second["summary_text"]
    # 3 — the two flags must agree
    assert jr["epic_reused"] is True
    assert second["overview"]["created_in_jira"] is False
    assert second["overview"]["reused_existing"] is True


async def test_bug_c_reuse_keys_come_back_in_ascending_order(run_agent, fake_jira, jira_env):
    """The reuse path rebuilt from a JQL response and never re-sorted."""
    fake = fake_jira()
    payload = {
        "requirement": (
            "Send rent reminder emails, allow tenants to raise a maintenance "
            "request, and let them view their payment history."
        ),
        "project_key": "ABC",
        "create_in_jira": True,
        "allow_duplicates": True,
    }
    await run_agent(payload)
    # Hand them back in the reverse order a "ORDER BY created DESC" would give.
    fake.store.reverse()

    second, _ = await run_agent(payload)
    story_keys = [s["key"] for s in second["jira_result"]["stories"]]
    subtask_keys = [s["key"] for s in second["jira_result"]["subtasks"]]

    def nums(keys):
        return [int(k.split("-")[1]) for k in keys]

    assert nums(story_keys) == sorted(nums(story_keys)), story_keys
    assert nums(subtask_keys) == sorted(nums(subtask_keys)), subtask_keys


async def test_bug_c_create_path_also_returns_sorted_keys(run_agent, fake_jira, jira_env):
    """Sorting must hold on both paths, not just the one that was broken."""
    fake_jira()
    result, _ = await run_agent(
        {
            "requirement": (
                "Send rent reminder emails, allow tenants to raise a "
                "maintenance request, and let them view their payment history."
            ),
            "project_key": "ABC",
            "create_in_jira": True,
        }
    )
    keys = [s["key"] for s in result["jira_result"]["stories"]]
    nums = [int(k.split("-")[1]) for k in keys]
    assert nums == sorted(nums), keys


# ==========================================================================
# BUGS D, E, F, G — heuristic output quality
# ==========================================================================

from src.classification.decompose import (  # noqa: E402
    MAX_SUMMARY_CHARS,
    classify,
    extract_actor,
    heuristic_breakdown,
    normalise_capability,
    split_capabilities,
    truncate,
)
from src.classification.validate import ProjectContext, has_action_intent  # noqa: E402
from src.models.schemas import Classification  # noqa: E402

CTX = ProjectContext(name="TenantSpace")

VERBOSE = (
    "We would like every tenant to be able to update their contact number from "
    "account settings, because today the only way is to phone the office, which "
    "is slow."
)
LIST_REQ = (
    "Support tenant onboarding, lease signing, rent collection, maintenance "
    "tracking, communication history, and move-out settlement."
)


# --- BUG D ---------------------------------------------------------------


def test_bug_d_statement_never_says_i_want_bare_verb():
    for req in [VERBOSE, LIST_REQ, "Send rent reminder emails and let them view invoices."]:
        for story in heuristic_breakdown(req, CTX).stories:
            stmt = story.user_story_statement.lower()
            after = stmt.split("i want", 1)[1].lstrip()
            assert after.startswith(("to ", "a ", "an ", "the ")), story.user_story_statement


def test_bug_d_so_that_clause_is_not_circular():
    for story in heuristic_breakdown(VERBOSE, CTX).stories:
        benefit = story.user_story_statement.lower().split("so that", 1)[1]
        assert "described in the requirement" not in benefit
        assert "is available to me" not in benefit


def test_bug_d_actor_is_extracted_from_the_text():
    actor, action = extract_actor("allow tenants to download a rent receipt")
    assert actor == "tenant", "plural actor should be singularised"
    assert action == "download a rent receipt"

    story = heuristic_breakdown("Allow tenants to download a rent receipt.", CTX).stories[0]
    assert story.user_story_statement.startswith("As a tenant,")


def test_bug_d_pronoun_is_not_used_as_an_actor():
    actor, action = extract_actor("let them view their payment history")
    assert actor == "user of this product", "'them' has no antecedent"
    assert action == "view their payment history"


def test_bug_d_a_bare_verb_statement_is_flagged_not_fatal():
    """Still caught — but as a quality check that asks once, never a schema
    error that fails the build (BGV-41, 2026-09-25)."""
    from src.models.schemas import statement_reads_badly
    from tests.test_schemas import make_story

    statement = "As a tenant, I want send rent reminder emails, so that it works."
    make_story(user_story_statement=statement)  # no longer raises
    assert statement_reads_badly(statement)


# --- BUG E ---------------------------------------------------------------


def test_bug_e_one_shared_limit_at_jiras_actual_255():
    assert MAX_SUMMARY_CHARS == 255


def test_bug_e_truncation_lands_on_a_word_boundary_with_an_ellipsis():
    text = "update their contact number from account settings and other things"
    out = truncate(text, 40)
    assert len(out) <= 40
    assert out.endswith("…")
    assert not out[:-1].rstrip().endswith(("accoun", "settin")), out
    assert text.startswith(out[:-1].rstrip())


def test_bug_e_short_text_is_left_alone():
    assert truncate("Short title", 255) == "Short title"


def test_bug_e_no_generated_summary_exceeds_the_limit():
    for req in [VERBOSE, LIST_REQ]:
        b = heuristic_breakdown(req, CTX)
        if b.epic:
            assert len(b.epic.jira_summary) <= MAX_SUMMARY_CHARS
        for story in b.stories:
            assert len(story.title) <= MAX_SUMMARY_CHARS
            for sub in story.subtasks:
                assert len(sub.title) <= MAX_SUMMARY_CHARS


def test_bug_e_a_long_capability_is_not_cut_at_the_old_80_chars():
    long_cap = "Allow tenants to " + "record every maintenance visit in detail " * 3
    title = heuristic_breakdown(long_cap, CTX).stories[0].title
    assert len(title) > 80, "the old 80-char cap should be gone"


# --- BUG F ---------------------------------------------------------------


def test_bug_f_requester_framing_is_stripped():
    assert normalise_capability("We would like to add a wishlist") == "add a wishlist"
    assert normalise_capability("Please can we export the tenant list") == (
        "export the tenant list"
    )


def test_bug_f_modal_padding_is_stripped():
    out = normalise_capability("every tenant to be able to update their number")
    assert "to be able to" not in out
    assert out == "every tenant update their number"


def test_bug_f_title_is_a_capability_not_the_raw_requirement():
    title = heuristic_breakdown(VERBOSE, CTX).stories[0].title
    assert not title.lower().startswith("we would like")
    assert "to be able to" not in title.lower()
    assert "because" not in title.lower()
    assert title == "Update their contact number from account settings"


# --- BUG G ---------------------------------------------------------------


def test_bug_g_shared_verb_is_distributed_across_list_items():
    caps = split_capabilities(LIST_REQ)
    assert len(caps) == 6
    assert all(c.lower().startswith("support") for c in caps), caps


def test_bug_g_no_story_is_a_bare_noun_phrase():
    for story in heuristic_breakdown(LIST_REQ, CTX).stories:
        after = story.user_story_statement.lower().split("i want", 1)[1].lstrip()
        assert after.startswith("to "), story.user_story_statement


def test_bug_g_a_list_without_a_shared_verb_is_left_alone():
    caps = split_capabilities(
        "Send rent reminders, and allow tenants to raise a maintenance request."
    )
    assert caps[0].lower().startswith("send")
    assert caps[1].lower().startswith("allow")


# ==========================================================================
# S15 — a 401 must not be reported as "project not found"
# ==========================================================================


async def test_s15_bad_credentials_report_authentication_not_missing_project(
    run_agent, fake_jira, jira_env
):
    """Jira answers 404 on /project/{key} for a bad token, so the 404 branch
    has to disambiguate against /myself or an expired token reads as a typo."""
    fake = fake_jira(project_status=404, authenticated=False)

    result, calls = await run_agent(
        {
            "requirement": "Allow landlords to upload a lease agreement PDF.",
            "project_key": "ABC",
            "create_in_jira": True,
        }
    )

    msg = result["message"].lower()
    assert "authentication failed" in msg, result["message"]
    assert "was not found" not in msg, "a 401 must not be reported as a missing project"
    assert "jira_email" in msg and "jira_api_token" in msg
    assert calls == ["inspect_jira_context", "notify_email"]
    assert fake.created == []


async def test_s15_a_genuinely_missing_project_still_says_not_found(run_agent, fake_jira, jira_env):
    """The fix must not turn every 404 into an auth error."""
    fake = fake_jira(project_status=404, authenticated=True)

    result, _ = await run_agent(
        {
            "requirement": "Allow landlords to upload a lease agreement PDF.",
            "project_key": "NOPE",
            "create_in_jira": True,
        }
    )

    msg = result["message"].lower()
    assert "was not found" in msg
    assert "authentication failed" not in msg
    assert fake.created == []


async def test_s15_token_never_appears_in_the_result(run_agent, fake_jira, jira_env):
    """Security: the credential must not reach the payload on any path."""
    import json as _json
    import os

    fake_jira(project_status=404, authenticated=False)
    token = os.environ["JIRA_API_TOKEN"]

    result, _ = await run_agent(
        {
            "requirement": "Allow landlords to upload a lease agreement PDF.",
            "project_key": "ABC",
            "create_in_jira": True,
        }
    )

    blob = _json.dumps(result)
    assert token not in blob
    assert token[:6] not in blob


# ==========================================================================
# S16 — the always-needed issue types are checked at gate 1
# ==========================================================================


async def test_s16_missing_subtask_type_is_caught_before_any_generation(
    run_agent, fake_jira, jira_env, monkeypatch
):
    """Story and Sub-task are needed whatever the size, so a mismatch must not
    cost a full decomposition (and, with the gateway on, a model call)."""
    # A project with no sub-task type at all. (A mere spelling difference —
    # "Subtask" vs "Sub-task" — is resolved since the team-managed BGV, 2026-09-27.)
    monkeypatch.setenv("JIRA_SUBTASK_ISSUE_TYPE", "Checklist item")
    fake = fake_jira(issue_types=["Epic", "Story", "Task", "Bug"])

    result, calls = await run_agent(
        {
            "requirement": "Allow tenants to add a secondary contact person.",
            "project_key": "ABC",
            "create_in_jira": True,
        }
    )

    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "Checklist item" in result["message"]
    assert "Task" in result["message"], "must list what the project does offer"
    # The whole point: stopped at gate 1, nothing generated.
    assert calls == ["inspect_jira_context", "notify_email"]
    assert result["classification"] is None
    assert result["stories"] == []
    assert fake.created == []


async def test_s16_missing_story_type_is_also_caught_at_gate_1(
    run_agent, fake_jira, jira_env, monkeypatch
):
    monkeypatch.setenv("JIRA_STORY_ISSUE_TYPE", "UserStory")
    fake = fake_jira(issue_types=["Epic", "Story", "Sub-task"])

    result, calls = await run_agent(
        {
            "requirement": "Allow tenants to add a secondary contact person.",
            "project_key": "ABC",
            "create_in_jira": True,
        }
    )

    assert "UserStory" in result["message"]
    assert calls == ["inspect_jira_context", "notify_email"]
    assert fake.created == []


async def test_s16_missing_epic_type_still_caught_before_writing(
    run_agent, fake_jira, jira_env, monkeypatch
):
    """Epic is only needed for Medium/Large, so it stays a gate-4 check —
    but it must still create nothing."""
    monkeypatch.setenv("JIRA_EPIC_ISSUE_TYPE", "Initiative")
    fake = fake_jira(issue_types=["Epic", "Story", "Sub-task"])

    result, calls = await run_agent(
        {
            "requirement": (
                "Send rent reminder emails, allow tenants to raise a maintenance "
                "request, and let them view their payment history."
            ),
            "project_key": "ABC",
            "create_in_jira": True,
        }
    )

    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "Initiative" in result["message"]
    assert "create_jira_issues" in calls, "epic check is deliberately later"
    assert fake.created == [], "but still nothing written"


async def test_s16_a_small_requirement_does_not_need_the_epic_type(
    run_agent, fake_jira, jira_env, monkeypatch
):
    """A project with no Epic type can still take Small requirements."""
    monkeypatch.setenv("JIRA_EPIC_ISSUE_TYPE", "Initiative")
    fake_jira(issue_types=["Story", "Sub-task"])

    result, _ = await run_agent(
        {
            "requirement": "Allow tenants to upload a profile photo.",
            "project_key": "ABC",
            "create_in_jira": True,
        }
    )

    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert result["jira_result"]["epic"] is None


# ==========================================================================
# Round 2 — three parser defects found in the TT2 live run
# ==========================================================================

R2_INDIRECT_OBJECT = (
    "Let dispatchers assign a job to a driver, allow drivers to accept or "
    "decline a job, and notify the dispatcher when a job is declined."
)
R2_EXPLANATION = (
    "It would be good if operations managers could see the total fuel cost for "
    "a vehicle over the last month, because right now they have to export the "
    "data and add it up in a spreadsheet, which nobody has time for."
)
R2_DROPPED_CAPABILITY = (
    "Let a driver report a vehicle fault from the app, and alert the depot "
    "manager when a fault is reported."
)


# --- defect 1: the actor regex grabbed the indirect object ---------------


def test_r2_actor_is_not_the_indirect_object():
    """'Let dispatchers assign a job to a driver' split on the WRONG 'to',
    giving actor='dispatchers assign a job' and title='A driver'."""
    actor, action = extract_actor("Let dispatchers assign a job to a driver")
    assert actor == "dispatcher"
    assert action == "assign a job to a driver"


def test_r2_actor_after_a_bare_article():
    """'let a driver report ...' has no 'to' at all."""
    actor, action = extract_actor("let a driver report a vehicle fault from the app")
    assert actor == "driver"
    assert action == "report a vehicle fault from the app"


def test_r2_a_genuine_to_split_still_works():
    """The fix must not break the case that already worked."""
    actor, action = extract_actor("allow tenants to download a rent receipt")
    assert actor == "tenant"
    assert action == "download a rent receipt"


def test_r2_modal_names_the_actor():
    """'operations managers could see X' names an actor as clearly as
    'allow operations managers to see X'."""
    actor, action = extract_actor("operations managers could see the total fuel cost for a vehicle")
    assert actor == "operations manager"
    assert action == "see the total fuel cost for a vehicle"


def test_r2_no_story_title_is_a_bare_object():
    """The visible symptom: a Story titled 'A driver'."""
    for req in (R2_INDIRECT_OBJECT, R2_DROPPED_CAPABILITY):
        for story in heuristic_breakdown(req, CTX).stories:
            assert story.title.lower() not in {"a driver", "a different driver"}
            assert len(story.title.split()) > 2, story.title


# --- defect 2: explanation leaked past the filter ------------------------


def test_r2_explanation_ends_the_capability_list():
    """'because ... and add it up in a spreadsheet' contributed a second
    capability, turning a Small requirement into a Medium."""
    caps = split_capabilities(R2_EXPLANATION)
    assert len(caps) == 1, caps
    assert not any("spreadsheet" in c.lower() for c in caps)
    assert classify(R2_EXPLANATION) is Classification.SMALL


def test_r2_explanation_case_produces_one_story_and_no_epic():
    breakdown = heuristic_breakdown(R2_EXPLANATION, CTX)
    assert breakdown.epic is None
    assert len(breakdown.stories) == 1
    title = breakdown.stories[0].title.lower()
    assert "spreadsheet" not in title
    assert "it would be good if" not in title
    assert "could" not in title


# --- defect 3: a real capability dropped by the length rule --------------


def test_r2_alert_is_recognised_as_an_action():
    """'alert' was missing from the verb list, so an eight-word capability
    with no recognised verb was discarded as commentary."""
    assert has_action_intent("alert the depot manager when a fault is reported")


def test_r2_the_dropped_capability_is_kept():
    caps = split_capabilities(R2_DROPPED_CAPABILITY)
    assert len(caps) == 2, caps
    assert any("alert" in c.lower() for c in caps)
    assert classify(R2_DROPPED_CAPABILITY) is Classification.MEDIUM


@pytest.mark.parametrize(
    "verb",
    [
        "accept",
        "approve",
        "assign",
        "cancel",
        "close",
        "decline",
        "download",
        "export",
        "filter",
        "mark",
        "reassign",
        "reject",
        "renew",
        "upload",
    ],
)
def test_r2_common_product_verbs_are_recognised(verb):
    assert has_action_intent(f"{verb} the thing for the user")


# --- the three actors must differ, end to end ----------------------------


def test_r2_multi_actor_requirement_extracts_distinct_actors():
    stories = heuristic_breakdown(R2_INDIRECT_OBJECT, CTX).stories
    assert len(stories) == 3
    actors = [
        s.user_story_statement.split(",")[0].replace("As a ", "").replace("As an ", "")
        for s in stories
    ]
    assert actors[0] == "dispatcher"
    assert actors[1] == "driver"
    assert len(set(actors)) >= 2, actors


# ==========================================================================
# Publishing safeguards
# ==========================================================================


async def test_allowlist_blocks_a_project_not_on_the_list(
    run_agent, fake_jira, jira_env, monkeypatch
):
    """With several people sharing one service account, a mistyped key must
    not be able to reach a project the deployment never authorised."""
    monkeypatch.setenv("LTW_ALLOWED_PROJECT_KEYS", "TT2,KS")
    fake = fake_jira()

    result, calls = await run_agent(
        {
            "requirement": "Allow drivers to upload a photo.",
            "project_key": "ABC",
            "create_in_jira": True,
        }
    )

    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "not on this deployment's allowed list" in result["message"]
    assert "TT2, KS" in result["message"]
    assert calls == ["inspect_jira_context", "notify_email"]
    assert fake.created == []


async def test_allowlist_permits_a_listed_project(run_agent, fake_jira, jira_env, monkeypatch):
    monkeypatch.setenv("LTW_ALLOWED_PROJECT_KEYS", "ABC,TT2")
    fake_jira()
    result, _ = await run_agent(
        {
            "requirement": "Allow drivers to upload a photo.",
            "project_key": "ABC",
            "create_in_jira": True,
        }
    )
    assert result["status"] == ResultStatus.JIRA_CREATED.value


async def test_empty_allowlist_keeps_the_old_behaviour(run_agent, fake_jira, jira_env):
    """Unset means unrestricted, so existing deployments are unaffected."""
    fake_jira()
    result, _ = await run_agent(
        {
            "requirement": "Allow drivers to upload a photo.",
            "project_key": "ABC",
            "create_in_jira": True,
        }
    )
    assert result["status"] == ResultStatus.JIRA_CREATED.value


async def test_read_only_mode_refuses_every_write(run_agent, fake_jira, jira_env, monkeypatch):
    monkeypatch.setenv("LTW_READ_ONLY", "true")
    fake = fake_jira()

    result, _ = await run_agent(
        {
            "requirement": "Allow drivers to upload a photo.",
            "project_key": "ABC",
            "create_in_jira": True,
        }
    )

    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "read-only mode" in result["message"]
    assert fake.created == [], "read-only must create nothing"


def test_benefit_clause_varies_with_the_verb():
    """One fixed 'so that' on every story was the round-1 complaint."""
    from src.classification.decompose import _benefit

    assert _benefit("assign a job to a driver") != _benefit("export the list as CSV")
    assert "hand-off" in _benefit("assign a job")
    assert _benefit("frobnicate the widget")  # unknown verb still yields something


def test_shouted_input_is_not_shouted_back():
    breakdown = heuristic_breakdown("ALLOW DRIVERS TO MARK A DELIVERY COMPLETE.", CTX)
    story = breakdown.stories[0]
    assert not story.title.isupper(), story.title
    assert "mARK" not in story.user_story_statement


def test_acronyms_survive_deshouting():
    breakdown = heuristic_breakdown("Export the vehicle list as CSV and PDF.", CTX)
    blob = " ".join(s.title for s in breakdown.stories)
    assert "CSV" in blob and "PDF" in blob


@pytest.mark.asyncio
async def test_a_failed_context_lookup_does_not_crash_the_breakdown() -> None:
    """When Jira cannot see the project, ``project`` comes back as null.

    ``.get("project", {})`` does not help there: the key exists and its value
    is ``None``, so the default never applies and the tool died with an
    AttributeError halfway through a run instead of failing cleanly.
    """
    from src.tools.tools import generate_work_breakdown

    for overrides in ({"project": None, "existing_issues": []}, {"project": {}}, None):
        result = await generate_work_breakdown("Allow drivers to log a break.", overrides)
        assert result.get("breakdown"), f"no breakdown for overrides={overrides!r}"


# --- near-miss adjudication -------------------------------------------------
# A reworded duplicate scores below the 0.75 line and used to be created in
# full: "Let drivers record a rest break" against an existing "Allow drivers to
# log a rest break" scores 0.60. Lowering the threshold is not the answer —
# "Send email notifications" and "Send SMS notifications" also score 0.60 and
# are different work. Only meaning separates them, so the band is adjudicated.


async def test_a_reworded_duplicate_is_caught_when_the_model_confirms_it(
    run_agent, fake_jira, jira_env, monkeypatch
):
    fake = fake_jira()
    seed_story(fake, "ABC-40", "Allow drivers to log a rest break")

    seen: list[list[tuple[str, str, str]]] = []

    async def _same_work(pairs):
        seen.append(pairs)
        return {0: True}

    monkeypatch.setattr(tool_module, "llm_same_work", _same_work)

    result, calls = await run_agent(
        {
            "requirement": "Let drivers record a rest break.",
            "project_key": "ABC",
            "create_in_jira": True,
        }
    )

    assert seen, "the near miss must be put to the model"
    assert seen[0][0][1] == "ABC-40"
    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert result["duplicate_matches"], "a confirmed reword blocks creation"
    assert result["duplicate_matches"][0]["existing_key"] == "ABC-40"
    assert result["duplicate_matches"][0]["matched_on"] == "semantic"
    assert "create_jira_issues" not in calls
    assert fake.created == [], "nothing may be created for a confirmed duplicate"


async def test_a_sibling_capability_survives_adjudication(
    run_agent, fake_jira, jira_env, monkeypatch
):
    """The model saying DIFFERENT must leave the work alone."""
    fake = fake_jira()
    seed_story(fake, "ABC-41", "Send email notifications to tenants")

    async def _same_work(pairs):
        return {}

    monkeypatch.setattr(tool_module, "llm_same_work", _same_work)

    result, _ = await run_agent(
        {
            "requirement": "Send SMS notifications to tenants.",
            "project_key": "ABC",
            "create_in_jira": False,
        }
    )

    assert result["duplicate_matches"] == [], "a sibling must not be blocked"


async def test_adjudication_failure_falls_back_to_the_word_scores(
    run_agent, fake_jira, jira_env, monkeypatch
):
    """No model, no confirmation — exactly the behaviour before this existed."""
    fake = fake_jira()
    seed_story(fake, "ABC-42", "Allow drivers to log a rest break")

    async def _unavailable(pairs):
        raise RuntimeError("no gateway")

    monkeypatch.setattr(tool_module, "llm_same_work", _unavailable)

    result, _ = await run_agent(
        {
            "requirement": "Let drivers record a rest break.",
            "project_key": "ABC",
            "create_in_jira": False,
        }
    )

    assert result["duplicate_matches"] == []
    assert result["best_match"]["existing_key"] == "ABC-42"


async def test_adjudication_can_be_switched_off(run_agent, fake_jira, jira_env, monkeypatch):
    fake = fake_jira()
    seed_story(fake, "ABC-43", "Allow drivers to log a rest break")
    monkeypatch.setenv("LTW_DUPLICATE_ADJUDICATE", "false")

    called = False

    async def _same_work(pairs):
        nonlocal called
        called = True
        return {0: True}

    monkeypatch.setattr(tool_module, "llm_same_work", _same_work)

    result, _ = await run_agent(
        {
            "requirement": "Let drivers record a rest break.",
            "project_key": "ABC",
            "create_in_jira": False,
        }
    )

    assert called is False, "the model must not be consulted when switched off"
    assert result["duplicate_matches"] == []


async def test_only_the_band_below_the_threshold_is_adjudicated(fake_jira, jira_env):
    """Above the line is already a match; far below is noise, not a near miss."""
    fake = fake_jira()
    seed_story(fake, "ABC-50", "Allow drivers to log a rest break")
    seed_story(fake, "ABC-51", "Redesign the invoice footer")

    from src.models.schemas import ExistingIssue

    existing = [
        ExistingIssue(
            key="ABC-50", summary="Allow drivers to log a rest break", issue_type="Story"
        ),
        ExistingIssue(key="ABC-51", summary="Redesign the invoice footer", issue_type="Story"),
    ]
    near = jira.find_near_misses(["Let drivers record a rest break"], existing)

    assert [m.existing_key for m in near] == ["ABC-50"], "only the plausible pair"
    assert jira.adjudication_floor() <= near[0].score < jira.duplicate_threshold()


async def test_degraded_generation_is_also_said_on_the_ticket(
    run_agent, fake_jira, jira_env, monkeypatch
):
    """The warning has to reach the person reading the ticket, not just the run.

    It went only into the result object, which nobody in Jira sees — so a
    gateway outage read as the agent simply writing badly.
    """
    seen: dict = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return {"type": "doc", "version": 1, "content": []}

    monkeypatch.setattr(tool_module.jira, "outcome_comment", _capture)

    await tool_module.report_to_issue(
        issue_key="ABC-1",
        headline="Work breakdown created.",
        situation="Created 1 Epic and 2 Stories.",
        quality_warning="written without the language model",
    )

    assert "language model" in seen.get("quality_warning", "")


def test_the_reply_comment_leads_with_the_quality_warning():
    body = jira.outcome_comment(
        headline="Work breakdown created.",
        situation="Created 1 Epic.",
        quality_warning="written without the language model",
    )
    rendered = str(body)
    assert "Please review the wording" in rendered
    # Before "What happened", so it cannot be scrolled past.
    assert rendered.index("Please review the wording") < rendered.index("What happened")
