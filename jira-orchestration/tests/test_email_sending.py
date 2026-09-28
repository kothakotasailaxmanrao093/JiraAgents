"""The router sends email (D1b) — and a reply says only what really happened.

Before this, nothing in the router could send: children stood down in delegated
mode for a sender the router never had, and replies said "Emailed …" anyway
(FLAWS.md F37). These drive the real shared sender with SMTP faked, the real
router tools, and the real routing rule for which outcomes email.
"""

from __future__ import annotations

from typing import Any

import pytest

from shared import mailer
from tools import tools


@pytest.fixture(autouse=True)
def _smtp(monkeypatch):
    """Capture instead of connecting. No test here opens a socket."""
    sent: list[dict[str, Any]] = []

    def _fake(host, port, sender, password, to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(mailer, "send_sync", _fake)
    monkeypatch.setenv("GMAIL_SENDER", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("ORCH_NOTIFY_EMAILS", "lead@example.com")
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.delenv("ORCH_NOTIFY_ON", raising=False)
    mailer.forget_recent_sends()
    return sent


# --- the shared sender -------------------------------------------------------


async def test_deliver_reports_sent_only_when_smtp_accepted(_smtp) -> None:
    result = await mailer.deliver(["a@example.com"], "Subject", "Body")
    assert result.sent is True and result.attempted is True
    assert _smtp[0]["to"] == ["a@example.com"]


async def test_missing_credentials_are_named_and_nothing_is_sent(_smtp, monkeypatch) -> None:
    monkeypatch.delenv("GMAIL_APP_PASSWORD")
    result = await mailer.deliver(["a@example.com"], "S", "B")
    assert result.sent is False
    assert "GMAIL_APP_PASSWORD" in result.error
    assert _smtp == []


async def test_an_smtp_failure_is_attempted_not_sent(monkeypatch) -> None:
    def _boom(*args):
        raise ConnectionRefusedError("no route")

    monkeypatch.setattr(mailer, "send_sync", _boom)
    monkeypatch.setenv("GMAIL_SENDER", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    result = await mailer.deliver(["a@example.com"], "S", "B")
    assert result.attempted is True and result.sent is False
    assert "ConnectionRefusedError" in result.error


async def test_an_identical_email_inside_the_window_is_not_sent_twice(_smtp) -> None:
    key = ("outcome", "created", "BGV-1", "Work breakdown created")
    first = await mailer.deliver(["a@x.com"], "S", "B", repeat_key=key, repeat_window_seconds=900)
    second = await mailer.deliver(["a@x.com"], "S", "B", repeat_key=key, repeat_window_seconds=900)
    assert first.sent is True
    assert second.sent is False and second.suppressed_repeat is True
    assert len(_smtp) == 1


# --- the router's tools ------------------------------------------------------


async def test_an_outcome_email_goes_out_with_the_ticket_and_the_facts(_smtp) -> None:
    result = await tools.send_outcome_email(
        "created", "BGV-7", "Work breakdown created", ["Created Story BGV-8: Record consent"], "r1"
    )
    assert result["sent"] is True
    assert "BGV-7" in _smtp[0]["subject"]
    assert "https://example.atlassian.net/browse/BGV-7" in _smtp[0]["body"]
    assert "BGV-8" in _smtp[0]["body"]


async def test_a_repeat_is_suppressed_but_a_different_question_is_sent(_smtp) -> None:
    await tools.send_outcome_email("clarification", "BGV-3", "More info", ["Q1"], "r1")
    again = await tools.send_outcome_email("clarification", "BGV-3", "More info", ["Q1"], "r2")
    other = await tools.send_outcome_email("clarification", "BGV-3", "More info", ["Q2"], "r3")
    assert again["suppressed_repeat"] is True
    assert other["sent"] is True
    assert len(_smtp) == 2


async def test_an_outcome_not_in_orch_notify_on_is_skipped(_smtp, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_NOTIFY_ON", "clarification,duplicates")
    result = await tools.send_outcome_email("created", "BGV-7", "Created", [], "r1")
    assert result["sent"] is False and "ORCH_NOTIFY_ON" in result["skipped"]
    assert _smtp == []


async def test_the_admin_alert_is_really_emailed_now(_smtp) -> None:
    result = await tools.notify_admins("Work Breakdown did not respond on BGV-7", "Timeout", "r1")
    assert result["sent"] is True
    assert "ALERT" in _smtp[0]["subject"]


async def test_an_admin_alert_with_no_address_is_not_attempted(_smtp, monkeypatch) -> None:
    monkeypatch.delenv("ORCH_NOTIFY_EMAILS")
    result = await tools.notify_admins("x", "y", "r1")
    assert result["attempted"] is False and result["sent"] is False
    assert _smtp == []


# --- D2: the health check's login probe -------------------------------------


async def test_the_login_check_logs_in_and_sends_nothing(monkeypatch, _smtp) -> None:
    logins: list[tuple] = []
    monkeypatch.setattr(mailer, "login_sync", lambda *a: logins.append(a))
    result = await tools.smtp_login_check()
    assert result == {"ok": True, "error": ""}
    assert len(logins) == 1 and _smtp == []


async def test_a_rejected_password_is_named(monkeypatch) -> None:
    import smtplib

    def _reject(*_a):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(mailer, "login_sync", _reject)
    monkeypatch.setenv("GMAIL_SENDER", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    result = await tools.smtp_login_check()
    assert result["ok"] is False and "app password" in result["error"]


def test_both_deployments_email_the_same_outcomes_and_never_on_creation() -> None:
    """One list must mean the same thing in both deployments (settings.py). And
    the owner's decision (2026-09-24, re-confirmed 2026-09-25): no email when
    tickets are created."""
    from pathlib import Path

    def value(path: Path, key: str) -> set[str]:
        for line in path.read_text().splitlines():
            if line.startswith(f"{key}="):
                return {k.strip() for k in line.split("=", 1)[1].split(",") if k.strip()}
        raise AssertionError(f"{key} missing from {path}")

    here = Path(__file__).resolve().parents[1]
    router = value(here / ".env.template", "ORCH_NOTIFY_ON")
    breakdown = value(here.parent / "jira-task-creation" / ".env.template", "LTW_NOTIFY_ON")
    assert router == breakdown == {"clarification", "duplicates", "failed"}
