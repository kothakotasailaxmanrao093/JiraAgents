"""Gmail notification: templates, configuration guards and send failures."""

from __future__ import annotations

import smtplib

import pytest

from src.models.schemas import NotificationKind
from src.notifications import email as notifier


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture what would have been sent instead of opening a socket."""
    captured: list[dict] = []

    def fake_send(host, port, sender, password, to, subject, body):  # noqa: ANN001
        captured.append(
            {
                "host": host,
                "port": port,
                "sender": sender,
                "password": password,
                "to": to,
                "subject": subject,
                "body": body,
            }
        )

    monkeypatch.setattr(notifier, "_send_sync", fake_send)
    return captured


# --- configuration ----------------------------------------------------------


def test_not_configured_without_credentials() -> None:
    assert not notifier.is_configured()


def test_configured_when_sender_password_and_recipients_are_set(gmail_env: None) -> None:
    assert notifier.is_configured()
    assert notifier.recipients() == ["lead@example.com", "pm@example.com"]


def test_defaults_are_gmail_starttls() -> None:
    host, port, _, _ = notifier.smtp_settings()
    assert (host, port) == ("smtp.gmail.com", 587)


def test_invalid_port_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_SMTP_PORT", "not-a-number")
    assert notifier.smtp_settings()[1] == 587


def test_an_email_means_a_person_has_something_to_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three outcomes that stop the run and wait on a human.

    Everything writes a comment on the ticket either way. An email is reserved
    for the cases where nobody reading that comment later is good enough.
    """
    assert notifier.notify_on() == ("clarification", "duplicates", "failed")
    assert notifier.should_notify(NotificationKind.CLARIFICATION_REQUIRED)
    assert notifier.should_notify(NotificationKind.DUPLICATES_FOUND)
    assert notifier.should_notify(NotificationKind.PARTIAL_DUPLICATE)
    assert notifier.should_notify(NotificationKind.FAILED)

    # A successful run needs no chasing; the tickets are the outcome.
    assert not notifier.should_notify(NotificationKind.CREATED)

    monkeypatch.setenv("LTW_NOTIFY_ON", "created")
    assert notifier.should_notify(NotificationKind.CREATED)
    assert not notifier.should_notify(NotificationKind.FAILED)


def test_a_comment_that_was_not_a_requirement_never_emails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not configurable, at any setting.

    "hi", "thanks", "who is the PM?" are not events to alert a human about.
    """
    for setting in ("", "created,failed", "clarification,duplicates,created,failed"):
        monkeypatch.setenv("LTW_NOTIFY_ON", setting)
        assert not notifier.should_notify(NotificationKind.INVALID_REQUEST)


async def test_missing_credentials_is_reported_not_raised() -> None:
    result = await notifier.send(NotificationKind.FAILED, issue_key="KS-1")
    assert not result.attempted
    assert not result.sent
    assert "GMAIL_SENDER" in result.error


async def test_missing_recipients_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_SENDER", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    result = await notifier.send(NotificationKind.FAILED, issue_key="KS-1")
    assert not result.attempted
    assert "LTW_NOTIFY_EMAILS" in result.error


# --- delivery ---------------------------------------------------------------


async def test_a_notification_reaches_every_recipient(gmail_env: None, sent: list[dict]) -> None:
    result = await notifier.send(
        NotificationKind.CLARIFICATION_REQUIRED,
        issue_key="KS-12",
        questions=["Who approves?"],
    )
    assert result.sent
    assert result.recipients == ["lead@example.com", "pm@example.com"]
    assert sent[0]["to"] == ["lead@example.com", "pm@example.com"]
    assert sent[0]["subject"] == "[Work Breakdown] KS-12 — Not created: 1 detail missing"


async def test_auth_failure_gives_the_app_password_hint(
    gmail_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    monkeypatch.setattr(notifier, "_send_sync", boom)
    result = await notifier.send(NotificationKind.FAILED, issue_key="KS-12")
    assert result.attempted and not result.sent
    assert "app password" in result.error


async def test_any_send_failure_is_captured_not_raised(
    gmail_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise OSError("network unreachable")

    monkeypatch.setattr(notifier, "_send_sync", boom)
    result = await notifier.send(NotificationKind.FAILED, issue_key="KS-12")
    assert result.attempted and not result.sent
    assert "network unreachable" in result.error


async def test_the_password_is_never_placed_in_the_body(gmail_env: None, sent: list[dict]) -> None:
    await notifier.send(NotificationKind.FAILED, issue_key="KS-12", situation="broke")
    assert "abcd efgh ijkl mnop" not in sent[0]["body"]
    assert "abcd efgh ijkl mnop" not in sent[0]["subject"]


# --- bodies -----------------------------------------------------------------


def test_clarification_body_states_the_situation_and_the_questions() -> None:
    body = notifier.build_body(
        NotificationKind.CLARIFICATION_REQUIRED,
        issue_key="KS-12",
        base_url="https://x.atlassian.net",
        situation="The approving role is unknown.",
        questions=["Who approves?", "What happens after rejection?"],
    )
    assert "https://x.atlassian.net/browse/KS-12" in body
    assert "SITUATION" in body
    assert "The approving role is unknown." in body
    assert "NO JIRA ISSUES WERE CREATED." in body
    assert "1. Who approves?" in body
    assert "WHAT TO DO NEXT" in body


def test_duplicate_body_says_nothing_was_created_and_lists_the_matches() -> None:
    body = notifier.build_body(
        NotificationKind.DUPLICATES_FOUND,
        issue_key="KS-12",
        duplicates=[
            {
                "proposed_title": "Email alerts",
                "existing_key": "KS-7",
                "existing_summary": "Alerts by email",
                "score": 0.82,
            }
        ],
    )
    assert "NOTHING WAS CREATED" in body
    assert "KS-7" in body and "Alerts by email" in body and "0.82" in body


def test_failure_body_lists_errors_and_any_partial_creations() -> None:
    body = notifier.build_body(
        NotificationKind.FAILED,
        issue_key="KS-12",
        situation="Jira rejected the Sub-task type.",
        errors=["HTTP 400: issuetype is not valid"],
        created_keys=["KS-30"],
    )
    assert "PARTIAL RESULT." in body
    assert "HTTP 400" in body
    assert "KS-30" in body
    assert "continues from these rather than duplicating" in body


def test_failure_body_without_partial_creations_says_nothing_was_created() -> None:
    body = notifier.build_body(NotificationKind.FAILED, issue_key="KS-12", situation="boom")
    assert "NO JIRA ISSUES WERE CREATED." in body


def test_success_body_lists_created_keys() -> None:
    body = notifier.build_body(
        NotificationKind.CREATED, issue_key="KS-12", created_keys=["KS-30", "KS-31"]
    )
    assert "KS-30" in body and "KS-31" in body


def test_every_kind_has_a_subject() -> None:
    """No kind may fall through to a blank or prefix-less subject."""
    for kind in NotificationKind:
        subject = notifier._subject_for(
            kind, "KS-1", questions=[], duplicates=[], created_keys=[], errors=[], counts={}
        )
        assert subject.startswith("[Work Breakdown] KS-1 — ")
        assert len(subject) > len("[Work Breakdown] KS-1 — ")


def test_a_subject_states_the_specifics_not_just_the_kind() -> None:
    """The subject line alone has to tell the reader what happened.

    'Jira issues created' sends them into the body to learn how many; these
    forms do not.
    """
    created = notifier._subject_for(
        NotificationKind.CREATED,
        "KS-1",
        questions=[],
        duplicates=[],
        created_keys=["a", "b", "c", "d", "e"],
        errors=[],
        counts={"epics": 1, "stories": 2, "subtasks": 2},
    )
    assert created == "[Work Breakdown] KS-1 — 5 items created (1 epic, 2 stories, 2 sub-tasks)"

    dupe = notifier._subject_for(
        NotificationKind.DUPLICATES_FOUND,
        "KS-1",
        questions=[],
        duplicates=[{"existing_key": "KS-9"}],
        created_keys=[],
        errors=[],
        counts={},
    )
    assert dupe.endswith("this work already exists (matches KS-9)")

    failed = notifier._subject_for(
        NotificationKind.FAILED,
        "KS-1",
        questions=[],
        duplicates=[],
        created_keys=[],
        errors=["Jira rejected the Sub-task type"],
        counts={},
    )
    assert failed == "[Work Breakdown] KS-1 — Failed: Jira rejected the Sub-task type"


def test_a_long_failure_reason_is_trimmed_not_dropped() -> None:
    subject = notifier._subject_for(
        NotificationKind.FAILED,
        "KS-1",
        questions=[],
        duplicates=[],
        created_keys=[],
        errors=["x" * 300],
        counts={},
    )
    assert len(subject) < 130
    assert subject.endswith("...")


# --- success receipts are opt-in -------------------------------------------
# The agent always asks for a receipt because a workflow cannot read the
# environment; the notifier decides whether one is actually sent.


async def test_a_successful_creation_is_not_emailed_by_default(
    gmail_env: None, sent: list[dict]
) -> None:
    """The comment on the ticket is the record. Nothing needs chasing."""
    result = await notifier.send(NotificationKind.CREATED, issue_key="TT2-118")
    assert not result.attempted
    assert result.error == "", "a suppressed receipt is not a failure"
    assert sent == []


async def test_chatter_is_never_emailed(gmail_env: None, sent: list[dict]) -> None:
    result = await notifier.send(NotificationKind.INVALID_REQUEST, issue_key="TT2-118")
    assert not result.attempted
    assert result.error == "", "a suppressed email is not a failure"
    assert sent == [], "no email may be sent for a non-requirement"


async def test_success_receipts_are_sent_when_enabled(
    gmail_env: None, sent: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LTW_NOTIFY_ON", "created")
    result = await notifier.send(
        NotificationKind.CREATED, issue_key="TT2-118", created_keys=["TT2-119"]
    )
    assert result.sent
    assert "TT2-119" in sent[0]["body"]


@pytest.mark.parametrize(
    "kind",
    [
        NotificationKind.CLARIFICATION_REQUIRED,
        NotificationKind.DUPLICATES_FOUND,
    ],
)
async def test_a_stop_that_waits_on_a_person_is_emailed(
    kind: NotificationKind, gmail_env: None, sent: list[dict]
) -> None:
    """Both stop the run and wait for someone to decide."""
    assert (await notifier.send(kind, issue_key="TT2-127")).sent
    assert len(sent) == 1


async def test_a_hard_failure_always_notifies(gmail_env: None, sent: list[dict]) -> None:
    assert (await notifier.send(NotificationKind.FAILED, issue_key="TT2-127")).sent
    assert len(sent) == 1


# --- a retry must not mail the same person twice ----------------------------
# Temporal retries a failing workflow indefinitely. A stuck FL-94 produced runs
# every few seconds for hours, and every one of them reached the notifier with
# identical arguments.


async def test_a_retry_does_not_send_the_same_email_again(
    gmail_env: None, sent: list[dict]
) -> None:
    for _ in range(5):
        await notifier.send(NotificationKind.FAILED, issue_key="KS-12", errors=["Boom"])
    assert len(sent) == 1


async def test_a_suppressed_retry_says_so_rather_than_claiming_success(
    gmail_env: None, sent: list[dict]
) -> None:
    await notifier.send(NotificationKind.FAILED, issue_key="KS-12", errors=["Boom"])
    again = await notifier.send(NotificationKind.FAILED, issue_key="KS-12", errors=["Boom"])
    assert not again.attempted
    assert not again.sent


async def test_a_different_issue_is_still_emailed(gmail_env: None, sent: list[dict]) -> None:
    await notifier.send(NotificationKind.FAILED, issue_key="KS-12", errors=["Boom"])
    await notifier.send(NotificationKind.FAILED, issue_key="KS-13", errors=["Boom"])
    assert len(sent) == 2


async def test_a_different_outcome_on_one_issue_is_still_emailed(
    gmail_env: None, sent: list[dict]
) -> None:
    """Suppression keys on the message, not the ticket."""
    await notifier.send(NotificationKind.FAILED, issue_key="KS-12", errors=["Boom"])
    await notifier.send(
        NotificationKind.CLARIFICATION_REQUIRED, issue_key="KS-12", questions=["Who?"]
    )
    assert len(sent) == 2


async def test_suppression_can_be_turned_off(
    gmail_env: None, sent: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LTW_EMAIL_REPEAT_WINDOW", "0")
    for _ in range(3):
        await notifier.send(NotificationKind.FAILED, issue_key="KS-12", errors=["Boom"])
    assert len(sent) == 3


# --- a system failure goes to the operator, not the whole team --------------


def test_failures_go_to_the_admin_list_when_one_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTW_NOTIFY_EMAILS", "team@example.com")
    monkeypatch.setenv("LTW_ADMIN_EMAILS", "ops@example.com")
    assert notifier.recipients_for(NotificationKind.FAILED) == ["ops@example.com"]
    assert notifier.recipients_for(NotificationKind.CLARIFICATION_REQUIRED) == ["team@example.com"]


def test_failures_fall_back_to_the_normal_list_when_no_admin_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured install must still hear about breakage."""
    monkeypatch.setenv("LTW_NOTIFY_EMAILS", "team@example.com")
    monkeypatch.delenv("LTW_ADMIN_EMAILS", raising=False)
    assert notifier.recipients_for(NotificationKind.FAILED) == ["team@example.com"]
