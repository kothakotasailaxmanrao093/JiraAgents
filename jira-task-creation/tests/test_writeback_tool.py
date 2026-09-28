"""report_to_issue and notify_email: write-back, labels and delivery."""

from __future__ import annotations

import pytest
from fakes import FakeClient, FakeResponse

from src.jira import api as jira
from src.notifications import email as notifier
from src.tools.tools import notify_email, report_to_issue


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    def install(routes: dict | None = None) -> FakeClient:
        fake = FakeClient(
            routes
            if routes is not None
            else {
                "/comment": FakeResponse(200, {"id": "10001"}),
                "/rest/api/3/issue/": FakeResponse(204),
            }
        )
        monkeypatch.setattr(jira, "jira_client", lambda *a, **k: fake)
        return fake

    return install


# --- report_to_issue --------------------------------------------------------


async def test_missing_issue_key_is_refused() -> None:
    result = await report_to_issue("")
    assert not result["ok"]
    assert "No issue key" in result["error"]


async def test_missing_credentials_are_refused() -> None:
    result = await report_to_issue("KS-12", "Done.")
    assert not result["ok"]
    assert "JIRA_BASE_URL" in result["error"]


async def test_a_comment_is_posted_and_the_id_returned(jira_env: None, client) -> None:
    fake = client()
    result = await report_to_issue("KS-12", "Work breakdown created.")
    assert result["ok"]
    assert result["comment_id"] == "10001"
    posted = [entry for entry in fake.calls if entry[0] == "POST"][0]
    assert "/rest/api/3/issue/KS-12/comment" in posted[1]
    assert posted[2]["json"]["body"]["type"] == "doc"


async def test_marking_processed_adds_and_removes_the_right_labels(jira_env: None, client) -> None:
    fake = client()
    await report_to_issue("KS-12", "Done.", mark_processed=True)
    put = [entry for entry in fake.calls if entry[0] == "PUT"][0]
    operations = put[2]["json"]["update"]["labels"]
    assert {"add": "ltw-processed"} in operations
    assert {"remove": "ltw-awaiting-input"} in operations


async def test_marking_awaiting_is_the_mirror_image(jira_env: None, client) -> None:
    fake = client()
    await report_to_issue("KS-12", "Needs answers.", mark_awaiting=True)
    operations = [e for e in fake.calls if e[0] == "PUT"][0][2]["json"]["update"]["labels"]
    assert {"add": "ltw-awaiting-input"} in operations
    assert {"remove": "ltw-processed"} in operations


async def test_no_label_call_is_made_when_neither_marker_is_requested(
    jira_env: None, client
) -> None:
    fake = client()
    await report_to_issue("KS-12", "Just a note.")
    assert not [entry for entry in fake.calls if entry[0] == "PUT"]


async def test_a_label_failure_is_reported_but_the_comment_stands(jira_env: None, client) -> None:
    fake = client(
        {
            "/comment": FakeResponse(200, {"id": "10001"}),
            "/rest/api/3/issue/KS-12": FakeResponse(400),
        }
    )
    result = await report_to_issue("KS-12", "Done.", mark_processed=True)
    assert result["comment_id"] == "10001"
    assert not result["ok"]
    assert "labels could not be updated" in result["error"]
    assert "may be picked up again" in result["error"]
    assert fake  # the client was used


async def test_a_comment_failure_is_returned_not_raised(jira_env: None, client) -> None:
    client({"/comment": FakeResponse(500)})
    result = await report_to_issue("KS-12", "Done.")
    assert not result["ok"]
    assert "Could not comment on KS-12" in result["error"]


async def test_created_issues_and_duplicates_reach_the_comment(jira_env: None, client) -> None:
    fake = client()
    await report_to_issue(
        "KS-12",
        "Nothing created.",
        "This overlaps existing work.",
        [{"key": "KS-30", "issue_type": "Story", "summary": "Email alerts"}],
        ["Is this new work?"],
        [
            {
                "proposed_title": "Email alerts",
                "existing_key": "KS-7",
                "existing_summary": "Alerts by email",
                "score": 0.82,
            }
        ],
    )
    body = [e for e in fake.calls if e[0] == "POST"][0][2]["json"]["body"]
    flat = jira._plain_text(body)
    assert "KS-30" in flat
    assert "KS-7" in flat
    assert "Is this new work?" in flat


# --- notify_email -----------------------------------------------------------


async def test_notify_email_passes_the_situation_through(
    gmail_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(
        notifier,
        "_send_sync",
        lambda h, p, s, pw, to, subj, body: captured.append({"subject": subj, "body": body}),
    )

    result = await notify_email(
        "duplicates_found",
        "KS-12",
        "Notifications",
        "KS",
        "This overlaps KS-7.",
        None,
        [
            {
                "proposed_title": "Email alerts",
                "existing_key": "KS-7",
                "existing_summary": "x",
                "score": 0.8,
            }
        ],
    )

    assert result["sent"] is True
    assert "already exists" in captured[0]["subject"]
    assert "This overlaps KS-7." in captured[0]["body"]


async def test_an_unknown_kind_is_treated_as_a_failure(gmail_env: None, monkeypatch) -> None:
    monkeypatch.setattr(notifier, "_send_sync", lambda *a, **k: None)
    result = await notify_email("something_else", "KS-12")
    assert result["kind"] == "failed"
    assert result["sent"] is True


async def test_notify_email_never_raises_when_unconfigured() -> None:
    result = await notify_email("failed", "KS-12")
    assert result["sent"] is False
    assert result["error"]
