"""Parsing of raw Jira payloads and the write-back comment."""

from __future__ import annotations

import pytest
from builders import changelog, comment, issue_payload, link, worklog

from src.jira import api as jira
from src.models.schemas import DuplicateMatch, JiraIssueRef

# --- trigger keyword --------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["Aetherion please help", "aetherion", "Ask AETHERION to break this down", "(Aetherion)"],
)
def test_trigger_keyword_matches_whole_word(text: str) -> None:
    assert jira.mentions_trigger(text)


@pytest.mark.parametrize("text", ["aetherionx", "myaetherion", "aether", "", "Aether ion"])
def test_trigger_keyword_does_not_match_partial_words(text: str) -> None:
    assert not jira.mentions_trigger(text)


def test_trigger_keyword_searches_every_supplied_text() -> None:
    assert jira.mentions_trigger("nothing here", "", "a comment mentioning Aetherion")


def test_trigger_keyword_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTW_TRIGGER_KEYWORD", "Jarvis")
    assert jira.mentions_trigger("hey Jarvis")
    assert not jira.mentions_trigger("hey Aetherion")


# --- parsing ----------------------------------------------------------------


def test_comments_are_flattened_out_of_adf() -> None:
    payload = issue_payload(trigger=None, comments=[comment("Also needs opt-out.", author="Ann")])
    parsed = jira.parse_comments(payload["fields"])
    assert len(parsed) == 1
    assert parsed[0].body == "Also needs opt-out."
    assert parsed[0].author == "Ann"
    assert parsed[0].created == "2026-09-02T09:30:00"


def test_empty_comment_bodies_are_dropped() -> None:
    empty = {"id": "9", "author": {"displayName": "X"}, "created": "", "body": None}
    assert jira.parse_comments({"comment": {"comments": [empty]}}) == []


def test_history_becomes_one_entry_per_changed_field() -> None:
    entries = jira.parse_history(issue_payload(changelog=changelog()))
    assert len(entries) == 1
    assert entries[0].field == "status"
    assert "changed status from 'To Do' to 'In Progress'" in entries[0].as_line()


def test_worklogs_are_parsed_with_their_comment() -> None:
    entries = jira.parse_worklogs(issue_payload(worklogs=[worklog()])["fields"])
    assert entries[0].time_spent == "2h"
    assert entries[0].comment == "Investigated the SMS provider."


def test_links_parent_and_subtasks_all_become_linked_issues() -> None:
    payload = issue_payload(
        issuelinks=[link("KS-3")],
        parent={"key": "KS-1", "fields": {"summary": "Platform", "issuetype": {"name": "Epic"}}},
        subtasks=[
            {"key": "KS-20", "fields": {"summary": "Spike", "issuetype": {"name": "Sub-task"}}}
        ],
    )
    links = jira.parse_links(payload["fields"])
    by_key = {item.key: item for item in links}
    assert set(by_key) == {"KS-3", "KS-1", "KS-20"}
    assert by_key["KS-3"].relationship == "blocks"
    assert by_key["KS-1"].relationship == "parent of this issue"
    assert by_key["KS-20"].relationship == "sub-task of this issue"


def test_links_without_a_key_are_dropped() -> None:
    assert jira.parse_links({"issuelinks": [{"type": {}, "outwardIssue": {"fields": {}}}]}) == []


def test_missing_sections_parse_to_empty_lists() -> None:
    assert jira.parse_comments({}) == []
    assert jira.parse_history({}) == []
    assert jira.parse_worklogs({}) == []
    assert jira.parse_links({}) == []


# --- write-back comment -----------------------------------------------------


def test_outcome_comment_is_valid_adf() -> None:
    doc = jira.outcome_comment(headline="Work breakdown created.")
    assert doc["type"] == "doc"
    assert doc["version"] == 1
    assert doc["content"], "an ADF document must not be empty"


def test_outcome_comment_lists_created_issues() -> None:
    doc = jira.outcome_comment(
        headline="Done.",
        created=[JiraIssueRef(key="KS-30", issue_type="Story", summary="Email alerts")],
    )
    flat = jira._plain_text(doc)
    assert "KS-30" in flat and "Email alerts" in flat


def test_outcome_comment_lists_duplicates_and_questions() -> None:
    doc = jira.outcome_comment(
        headline="Nothing created.",
        questions=["Who approves?"],
        duplicates=[
            DuplicateMatch(
                proposed_title="Email alerts",
                existing_key="KS-7",
                existing_summary="Alerts by email",
                score=0.82,
            )
        ],
        notified="Emailed lead@example.com.",
    )
    flat = jira._plain_text(doc)
    assert "Who approves?" in flat
    assert "KS-7" in flat
    assert "Emailed lead@example.com." in flat
