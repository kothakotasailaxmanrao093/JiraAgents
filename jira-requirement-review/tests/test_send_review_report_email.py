"""Pulsar notification tool: recipient resolution + skip-path degradation.

The actual HTTP POST to Pulsar is not exercised here (no injected session to
fake against, matching this repo's convention of not unit-testing the network
edge of thin @tool shims) — only the pure-ish precedence/skip logic.
"""

from __future__ import annotations

import pytest

from tools.send_review_report_email_tool import _resolve_recipients, send_review_report_email


@pytest.fixture(autouse=True)
def _clear_notify_env(monkeypatch):
    monkeypatch.delenv("JIRA_NOTIFY_USER_IDS", raising=False)
    monkeypatch.delenv("JIRA_NOTIFY_EMAILS", raising=False)
    monkeypatch.delenv("NOTIFICATIONS_BASE_URL", raising=False)


def test_resolve_recipients_user_ids_take_precedence(monkeypatch):
    monkeypatch.setenv("JIRA_NOTIFY_USER_IDS", "uuid-1, uuid-2")
    monkeypatch.setenv("JIRA_NOTIFY_EMAILS", "a@example.com")

    def _boom(_emails):
        raise AssertionError("DB lookup should be skipped when USER_IDS is set")

    monkeypatch.setattr(
        "tools.send_review_report_email_tool.db_handler.get_user_ids_by_emails", _boom
    )
    assert _resolve_recipients() == ["uuid-1", "uuid-2"]


def test_resolve_recipients_falls_back_to_email_lookup(monkeypatch):
    monkeypatch.setenv("JIRA_NOTIFY_EMAILS", "a@example.com, b@example.com")
    monkeypatch.setattr(
        "tools.send_review_report_email_tool.db_handler.get_user_ids_by_emails",
        lambda emails: [f"resolved-{e}" for e in emails],
    )
    assert _resolve_recipients() == ["resolved-a@example.com", "resolved-b@example.com"]


def test_resolve_recipients_empty_when_neither_set():
    assert _resolve_recipients() == []


async def test_send_review_report_email_skips_without_recipients():
    result = await send_review_report_email("tmpl", {"foo": "bar"})
    assert result == {"status": "skipped", "reason": "no_recipients"}


async def test_send_review_report_email_skips_without_base_url(monkeypatch):
    monkeypatch.setenv("JIRA_NOTIFY_USER_IDS", "uuid-1")
    result = await send_review_report_email("tmpl", {"foo": "bar"})
    assert result == {"status": "skipped", "reason": "no_base_url"}
