"""Planning reads the whole conversation on a story (2026-10-26)."""

from routing.pipeline import (
    agent_gist,
    discussion_lines,
    planning_text,
    request_content,
    requirement_words,
)

ROOT = {"key": "BTS-33", "summary": "Per-transfer limits", "description": "At most 50,000 INR."}
AGENT_REPLY = (
    "AetherionAgent · Requirement review — nothing created\n"
    "Handled by\n"
    "Jira Orchestration → Requirement Review\n"
    "Readiness: Needs minor clarification (4/5).\n"
    "Is the limit per account or per customer?\n"
    "— AetherionAgent · automated reply"
)


def test_the_design_reads_the_agents_question_and_the_persons_answer():
    text = planning_text(
        ROOT,
        [],
        [],
        None,
        [
            {"who": "agent", "author": "Agent", "text": AGENT_REPLY},
            {"who": "person", "author": "Ravi", "text": "Per account."},
        ],
    )
    assert "Discussion on the story (oldest first)" in text
    assert "- Agent: Requirement review — nothing created" in text
    assert "per account or per customer?" in text
    assert "- Ravi: Per account." in text
    assert "automated reply" not in text and "Handled by" not in text


def test_a_long_agent_reply_is_cut_to_its_point():
    gist = agent_gist("AetherionAgent · Headline\n" + "word " * 400)
    assert gist.startswith("Headline") and len(gist) <= 500 and gist.endswith("…")


def test_the_newest_comments_are_kept_when_the_discussion_is_long():
    many = [{"who": "person", "author": "P", "text": f"comment {i}"} for i in range(60)]
    lines = discussion_lines(many)
    assert len(lines) == 40 and lines[-1] == "- P: comment 59"


def test_a_request_is_split_into_its_command_and_its_words():
    assert request_content("@Aetherion create TDD") == ("create tdd", "")
    assert request_content("@Aetherion update the TDD anyway: use double") == (
        "update the tdd anyway",
        "use double",
    )


def test_only_a_request_that_adds_to_the_requirement_counts_as_requirement():
    assert requirement_words("@Aetherion create TDD") == ""
    assert (
        requirement_words("@Aetherion create TDD — note the limit is per account")
        == "note the limit is per account"
    )
    # a change request goes through the change validator, not the story
    assert requirement_words("@Aetherion update the TDD: use double for all money") == ""
    assert requirement_words("@Aetherion why not use BigDecimal for money here?") == ""
