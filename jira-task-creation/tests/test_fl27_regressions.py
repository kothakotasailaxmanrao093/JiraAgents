"""The FL-27 failures, pinned.

A comment on FL-27 produced twelve tickets that should never have existed. Three
separate mistakes, each with its own test here so none of them returns.
"""

from __future__ import annotations

import pytest

from src.classification.decompose import heuristic_breakdown, split_capabilities
from src.classification.validate import ProjectContext
from src.context.ingest import assemble_requirement, is_pointer_comment
from src.models.schemas import SourceIssue

CTX = ProjectContext(
    name="FleetLinkTest",
    description="A delivery fleet platform. Dispatchers assign jobs to drivers.",
)


# --- mistake 1: an instruction was built as if it were a requirement --------
# "@Aetherion add more descrption and create some more sub tasks" is a message
# about the ticket. It became Epic FL-28 and Story FL-29 "Add more descrption".


@pytest.mark.parametrize(
    "comment",
    [
        "add more descrption and create some more sub tasks",
        "add more descrption and create some more sub tasks reference FL-27",
        "break this down",
        "please break this ticket down into stories",
        "create some more subtasks",
    ],
)
def test_an_instruction_about_the_ticket_is_a_pointer(comment: str) -> None:
    assert is_pointer_comment(comment)


@pytest.mark.parametrize(
    "comment",
    [
        "Allow drivers to upload a photo of a delivered parcel.",
        "Create me dashboard with all the drivers and vehicle.",
        # Mentions tickets, but is plainly product work.
        "Let dispatchers assign a job, and create tickets for each step.",
        # Contains the words "add more description" inside a real requirement.
        "Allow users to add more description to their profile.",
        # An instruction AND a requirement — the requirement must win, or the
        # work being asked for is silently thrown away.
        "please break this down. Capture a photo of the drop-off and collect "
        "the recipient's signature on the mobile app.",
    ],
)
def test_a_real_requirement_is_never_mistaken_for_a_pointer(comment: str) -> None:
    assert not is_pointer_comment(comment)


def test_a_pointer_comment_decomposes_the_tickets_own_description() -> None:
    """Exactly FL-27: the ticket said what was wanted; the comment just asked."""
    source = SourceIssue(
        key="FL-27",
        summary="Create me a dashboard",
        description="Create me dashboard with all the drivers and vehicle, with all the tracker.",
        trigger_comment_id="1",
        trigger_comment_author="Laxman",
        trigger_comment_body="@Aetherion add more descrption and create some more sub tasks",
    )
    text, _ = assemble_requirement(source)
    body = text.split("\n", 1)[1]

    assert "dashboard with all the drivers" in body
    assert "add more descrption" not in body.lower()
    assert "sub tasks" not in body.lower()


# --- mistake 2: one idea split into three fragments -------------------------
# "…with all the drivers and vehicle, with all the tracker" became three
# Stories: a half sentence, an invented "Create vehicle", and "With all the
# tracker", which is not a sentence at all.


def test_a_modifier_phrase_is_not_a_separate_capability() -> None:
    caps = split_capabilities(
        "Create me dashboard with all the dirvers and vehicle, with all the tracker."
    )
    assert len(caps) == 1, caps


def test_no_story_is_built_out_of_a_fragment() -> None:
    breakdown = heuristic_breakdown(
        "Create me dashboard with all the dirvers and vehicle, with all the tracker.",
        CTX,
    )
    titles = [s.title.lower() for s in breakdown.stories]
    assert breakdown.classification.value == "Small"
    assert breakdown.epic is None
    assert not any(t.startswith("with ") for t in titles), titles
    assert "create vehicle" not in titles, titles


# --- mistake 3: fixing the above must not un-split real lists ---------------
# The first attempt at the fix merged "insurance renewal" into the clause
# before it, because "insurance".startswith("in") and "in" is a preposition.


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        (
            "Track vehicle servicing, tyre replacement, insurance renewal, fuel card "
            "usage, driver licence expiry, and accident reports.",
            6,
        ),
        (
            "Let dispatchers assign a job to a driver, allow drivers to accept or "
            "decline a job, and notify the dispatcher when a job is declined.",
            3,
        ),
        (
            "Allow customers to rate a completed delivery, and let managers see the "
            "average rating per driver.",
            2,
        ),
        # Words that merely begin with a preposition: insurance/offer/form/onboarding.
        ("Track insurance renewal, offer discounts, and form new contracts.", 3),
        ("Support onboarding, and track international shipments.", 2),
        ("Allow drivers to upload a photo of a delivered parcel.", 1),
    ],
)
def test_real_lists_still_split(requirement: str, expected: int) -> None:
    caps = split_capabilities(requirement)
    assert len(caps) == expected, caps


def test_a_deliberate_bullet_list_is_never_merged() -> None:
    """Each bullet is a capability by definition, whatever it looks like."""
    caps = split_capabilities("- fuel spend by depot\n- with all the trackers\n- insurance renewal")
    assert len(caps) == 3, caps


# --- mistake 4: a duplicate Story instead of the ticket that was pointed at --
# On FL-15 the agent created Story FL-16 saying the same thing as FL-15, and
# hung FL-17/18/19 off the copy. FL-15 — the ticket that was asked about —
# ended up with no sub-tasks at all.


def test_a_pointer_request_marks_itself_as_one() -> None:
    from src.context.ingest import is_pointer_comment

    assert is_pointer_comment("add more descrption and create some more sub tasks")


async def test_sub_tasks_attach_to_the_ticket_not_a_copy_of_it() -> None:
    from fakes import FakeClient, FakeResponse

    from src.jira import api as jira
    from src.models.schemas import WorkBreakdown

    breakdown = WorkBreakdown.model_validate(
        {
            "classification": "Small",
            "analysis": "One focused capability.",
            "stories": [
                {
                    "title": "Create a dashboard of drivers and vehicles",
                    "user_story_statement": (
                        "As a manager, I want to see a dashboard of drivers and "
                        "vehicles, so that I can track the fleet."
                    ),
                    "description": "A dashboard listing drivers, vehicles and trackers.",
                    "business_value": "One place to see fleet state.",
                    "priority": "Medium",
                    "estimated_complexity": "Medium",
                    "dependencies": "None",
                    "acceptance_criteria": ["The dashboard lists every vehicle."],
                    "subtasks": [
                        {
                            "title": "Define the rules",
                            "description": "Agree what the dashboard shows.",
                            "expected_outcome": "An agreed field list.",
                            "dependencies": "None",
                            "completion_criteria": "Signed off by the manager.",
                        }
                    ],
                }
            ],
        }
    )
    created: list[dict] = []

    def create(_url: str, kwargs: dict) -> FakeResponse:
        fields = kwargs["json"]["fields"]
        created.append(fields)
        return FakeResponse(201, {"key": f"FL-{100 + len(created)}", "id": "1"})

    # Attaching now reads the ticket's existing sub-tasks first, so the same
    # request twice cannot add a second identical set. FL-15 has none.
    def no_children(_url: str, kwargs: dict) -> FakeResponse:
        return FakeResponse(200, {"issues": []})

    client = FakeClient({"/rest/api/3/issue": create, "/rest/api/3/search/jql": no_children})
    result = await jira.create_hierarchy(
        client, "https://x.atlassian.net", "FL", breakdown, "ltw-abc", attach_to_key="FL-15"
    )

    # No Story was created — only the sub-task.
    types = [f["issuetype"]["name"] for f in created]
    assert "Story" not in types, types
    assert types == ["Sub-task"]
    # And it hangs off the ticket that was asked about.
    assert created[0]["parent"]["key"] == "FL-15"
    assert result.attached_to == "FL-15"
    assert "No new Story was created" in result.summary


async def test_several_capabilities_still_get_their_own_stories() -> None:
    """Attaching is only right for one capability; three need Stories."""
    from fakes import FakeClient, FakeResponse

    from src.jira import api as jira
    from src.models.schemas import WorkBreakdown

    story = {
        "title": "Assign a job to a driver",
        "user_story_statement": (
            "As a dispatcher, I want to assign a job to a driver, so that work "
            "reaches the right person."
        ),
        "description": "Dispatchers pick a driver for each job.",
        "business_value": "Work gets to the right driver.",
        "priority": "Medium",
        "estimated_complexity": "Medium",
        "dependencies": "None",
        "acceptance_criteria": ["A job can be assigned."],
        "subtasks": [
            {
                "title": "Define the rules",
                "description": "Agree the assignment rules.",
                "expected_outcome": "Agreed rules.",
                "dependencies": "None",
                "completion_criteria": "Signed off.",
            }
        ],
    }
    breakdown = WorkBreakdown.model_validate(
        {
            "classification": "Medium",
            "analysis": "Two related capabilities.",
            "epic": {
                "business_objective": "Give dispatchers control of job assignment.",
                "scope": ["assignment"],
                "out_of_scope": ["Not specified"],
                "priority": "Medium",
                "acceptance_criteria": ["Jobs can be assigned and declined."],
                "jira_summary": "Job assignment",
            },
            "stories": [story, {**story, "title": "Decline a job"}],
        }
    )
    created: list[dict] = []

    def create(_url: str, kwargs: dict) -> FakeResponse:
        created.append(kwargs["json"]["fields"])
        return FakeResponse(201, {"key": f"FL-{200 + len(created)}", "id": "1"})

    client = FakeClient({"/rest/api/3/issue": create})
    result = await jira.create_hierarchy(
        client, "https://x.atlassian.net", "FL", breakdown, "ltw-abc", attach_to_key="FL-15"
    )

    types = [f["issuetype"]["name"] for f in created]
    assert types.count("Story") == 2, types
    assert "Epic" in types
    assert result.attached_to == "", "attaching is only for a single capability"


# --- mistake 3: the joining words were eaten --------------------------------


def test_merging_keeps_the_words_that_joined_the_fragments() -> None:
    """ "…drivers vehicle with all the tracker" was not a sentence any more."""
    caps = split_capabilities(
        "Create me dashboard with all the dirvers and vehicle, with all the tracker."
    )
    assert len(caps) == 1
    assert "dirvers and vehicle" in caps[0], caps[0]
    assert "vehicle, with all the tracker" in caps[0], caps[0]


def test_the_story_and_its_subtasks_all_read_properly() -> None:
    breakdown = heuristic_breakdown(
        "Create me dashboard with all the dirvers and vehicle, with all the tracker.",
        CTX,
    )
    story = breakdown.stories[0]
    for text in [story.title, *(s.title for s in story.subtasks)]:
        assert "dirvers and vehicle" in text, text
