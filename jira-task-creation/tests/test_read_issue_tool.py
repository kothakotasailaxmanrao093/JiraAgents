"""The read_jira_issue tool: ingestion, gates and failure reporting."""

from __future__ import annotations

import pytest
from builders import attachment, changelog, comment, issue_payload, link, worklog
from fakes import FakeClient, FakeResponse

from src.jira import api as jira
from src.tools.tools import read_jira_issue


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """Install a FakeClient and hand it back for assertions."""

    def install(routes: dict) -> FakeClient:
        fake = FakeClient(routes)
        monkeypatch.setattr(jira, "jira_client", lambda *a, **k: fake)
        return fake

    return install


def issue_route(payload: dict, **extra) -> dict:
    return {"/rest/api/3/issue/": FakeResponse(200, payload), **extra}


# --- configuration guards ---------------------------------------------------


async def test_missing_issue_key_is_refused() -> None:
    result = await read_jira_issue("")
    assert not result["ok"]
    assert "No Jira issue key" in result["error"]


async def test_missing_credentials_are_refused(client) -> None:
    client(issue_route(issue_payload()))
    result = await read_jira_issue("KS-12")
    assert not result["ok"]
    assert "JIRA_BASE_URL" in result["error"]


# --- happy path -------------------------------------------------------------


async def test_a_full_issue_is_read_and_assembled(jira_env: None, client) -> None:
    payload = issue_payload(
        comments=[comment("Opt-out is mandatory.")],
        issuelinks=[link("KS-3")],
        changelog=changelog(),
        worklogs=[worklog()],
    )
    client(issue_route(payload))

    result = await read_jira_issue("KS-12")

    assert result["ok"]
    assert result["key"] == "KS-12"
    assert result["summary"] == "Notification preferences"
    assert result["reporter"] == "Priya"
    assert len(result["comments"]) == 2  # the discussion plus the request itself
    assert len(result["history"]) == 1
    assert len(result["worklogs"]) == 1
    assert len(result["linked_issues"]) == 1
    # The requirement is the triggering comment; the ticket and other comments
    # are carried as background.
    assert "Send email and SMS notifications" in result["requirement_text"]
    assert "Opt-out is mandatory." in result["requirement_text"]
    assert result["trigger_comment_id"] == "9001"
    assert result["trigger_comment_author"] == "Priya"


async def test_the_project_key_comes_from_the_issue_not_a_form(jira_env: None, client) -> None:
    client(issue_route(issue_payload()))
    result = await read_jira_issue("KS-12")
    assert result["project_key"] == "KS"
    assert result["project_name"] == "Knowledge Suite"


async def test_the_issue_key_is_uppercased(jira_env: None, client) -> None:
    fake = client(issue_route(issue_payload()))
    await read_jira_issue("ks-12")
    assert "/rest/api/3/issue/KS-12" in fake.calls[0][1]


async def test_changelog_is_requested_on_the_same_call(jira_env: None, client) -> None:
    fake = client(issue_route(issue_payload()))
    await read_jira_issue("KS-12")
    assert fake.calls[0][2]["params"]["expand"] == "changelog"


# --- trigger gates ----------------------------------------------------------


async def test_the_trigger_keyword_is_detected(jira_env: None, client) -> None:
    client(issue_route(issue_payload()))
    assert (await read_jira_issue("KS-12"))["trigger_matched"] is True


async def test_an_issue_with_no_request_comment_is_flagged(jira_env: None, client) -> None:
    """A ticket nobody has asked anything on is not a request."""
    client(issue_route(issue_payload(trigger=None)))
    result = await read_jira_issue("KS-12")
    assert result["trigger_matched"] is False
    assert any("No unanswered comment" in note for note in result["notes"])


async def test_the_keyword_is_honoured_in_a_comment(jira_env: None, client) -> None:
    payload = issue_payload(
        summary="Notifications",
        description="Send emails and SMS.",
        comments=[comment("Aetherion, please break this down.")],
    )
    client(issue_route(payload))
    assert (await read_jira_issue("KS-12"))["trigger_matched"] is True


async def test_an_already_answered_request_is_flagged(jira_env: None, client) -> None:
    """Answered requests are tracked per comment, so one ticket takes many."""
    # The properties route must come first: FakeClient matches on the first
    # fragment that appears in the URL, and "/rest/api/3/issue/" is a prefix of
    # the property URL too.
    client(
        {
            "/properties/ltw-answered-comments": FakeResponse(200, {"value": {"ids": ["9001"]}}),
            "/rest/api/3/issue/": FakeResponse(200, issue_payload()),
        }
    )
    result = await read_jira_issue("KS-12", None, True, "9001")
    assert result["already_processed"] is True
    assert any("already been answered" in note for note in result["notes"])


async def test_force_reads_an_issue_that_fails_both_gates(jira_env: None, client) -> None:
    payload = issue_payload(
        summary="No keyword", description="Nothing here.", labels=["ltw-processed"]
    )
    client(issue_route(payload))
    result = await read_jira_issue("KS-12", False, False)
    assert result["ok"]
    assert result["requirement_text"]


# --- attachments ------------------------------------------------------------


async def test_attachment_text_is_extracted_into_the_requirement(jira_env: None, client) -> None:
    client(
        issue_route(
            issue_payload(attachments=[attachment("matrix.csv")]),
            **{"/attach/1": FakeResponse(200, content=b"channel,enabled\nemail,yes\n")},
        )
    )
    result = await read_jira_issue("KS-12")
    assert result["attachments"][0]["filename"] == "matrix.csv"
    assert "channel" in result["requirement_text"]


async def test_an_attachment_download_failure_does_not_fail_the_run(jira_env: None, client) -> None:
    client(
        issue_route(
            issue_payload(attachments=[attachment("matrix.csv")]),
            **{"/attach/1": FakeResponse(500, {"error": "boom"})},
        )
    )
    result = await read_jira_issue("KS-12")
    assert result["ok"]
    assert "Could not download" in result["attachments"][0]["note"]


async def test_unsupported_attachments_are_not_downloaded(jira_env: None, client) -> None:
    fake = client(issue_route(issue_payload(attachments=[attachment("setup.exe")])))
    result = await read_jira_issue("KS-12")
    assert "Unsupported attachment type" in result["attachments"][0]["note"]
    assert not any("/attach/" in url for _, url, _ in fake.calls)


async def test_attachments_are_capped(
    jira_env: None, monkeypatch: pytest.MonkeyPatch, client
) -> None:
    monkeypatch.setenv("LTW_ATTACHMENT_MAX_FILES", "1")
    client(
        issue_route(
            issue_payload(attachments=[attachment("a.csv"), attachment("b.csv")]),
            **{"/attach/1": FakeResponse(200, content=b"x,y\n1,2\n")},
        )
    )
    result = await read_jira_issue("KS-12")
    assert len(result["attachments"]) == 1
    assert any("only the first 1 were read" in note for note in result["notes"])


async def test_attachments_are_skipped_when_there_is_no_request(jira_env: None, client) -> None:
    fake = client(issue_route(issue_payload(trigger=None, attachments=[attachment("a.csv")])))
    result = await read_jira_issue("KS-12")
    assert result["attachments"] == []
    assert not any("/attach/" in url for _, url, _ in fake.calls)


# --- failure paths ----------------------------------------------------------


async def test_a_missing_issue_with_valid_credentials_says_not_found(
    jira_env: None, client
) -> None:
    client(
        {"/rest/api/3/issue/": FakeResponse(404), "/myself": FakeResponse(200, {"accountId": "1"})}
    )
    result = await read_jira_issue("KS-99")
    assert not result["ok"]
    assert "was not found" in result["error"]


async def test_a_404_with_bad_credentials_says_authentication_failed(
    jira_env: None, client
) -> None:
    client({"/rest/api/3/issue/": FakeResponse(404), "/myself": FakeResponse(401)})
    result = await read_jira_issue("KS-99")
    assert not result["ok"]
    assert "authentication failed" in result["error"].lower()


async def test_a_403_says_permission_denied(jira_env: None, client) -> None:
    client({"/rest/api/3/issue/": FakeResponse(403)})
    result = await read_jira_issue("KS-12")
    assert not result["ok"]
    assert "not permitted" in result["error"]
