"""The webhook receiver: what it accepts, what it refuses, and why.

Every refusal here exists to stop a loop or a duplicate run, so these are the
tests that keep the service from stampeding Jira.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.webhook import server


@pytest.fixture(autouse=True)
def _no_real_runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record which issues would have been run, without running anything."""
    started: list[str] = []

    async def fake_run(issue_key: str, comment_id: str = "") -> dict[str, Any]:
        started.append(issue_key)
        return {"status": "JIRA_CREATED", "message": "fake"}

    monkeypatch.setattr(server, "run_agent", fake_run)
    monkeypatch.setattr(server, "_recent_runs", {})
    monkeypatch.setattr(server, "_slots", None)

    async def no_account() -> str:
        return "agent-account-id"

    monkeypatch.setattr(server, "_agent_account_id", no_account)
    return started


# Every delivery must be authenticated: the endpoint creates Jira issues, so an
# unauthenticated deployment is an open write endpoint. A configured secret is
# therefore the normal state, and the tests below run in it.
TEST_SECRET = "test-webhook-secret"


@pytest.fixture(autouse=True)
def _configured_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTW_WEBHOOK_SECRET", TEST_SECRET)


@pytest.fixture
def client() -> TestClient:
    """A caller that authenticates, for tests about everything except auth."""
    return TestClient(server.app, headers={"X-LTW-Secret": TEST_SECRET})


@pytest.fixture
def raw_client() -> TestClient:
    """A caller that sends no credentials, so auth tests stay honest.

    Used by the tests that decide *whether* a delivery is authenticated; giving
    them the authenticating client would let a header pass a test that is meant
    to exercise the signature path.
    """
    return TestClient(server.app)


def issue_event(
    key: str = "ORD-1",
    description: str = "@Aetherion Create a login feature for all users.",
    author_id: str = "person-1",
    comment: str | None = None,
) -> dict[str, Any]:
    """A Jira webhook body, shaped the way Jira actually sends it."""
    payload: dict[str, Any] = {
        "webhookEvent": "jira:issue_created",
        "user": {"accountId": author_id, "emailAddress": "person@example.com"},
        "issue": {
            "key": key,
            "fields": {
                "summary": "Login feature",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": description}]}
                    ],
                },
            },
        },
    }
    if comment is not None:
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": author_id},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}],
            },
        }
    return payload


# --- health -----------------------------------------------------------------


def test_health_reports_the_settings_that_matter(client: TestClient, jira_env: None) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["jira_configured"] is True
    assert body["trigger_keyword"] == "Aetherion"
    assert body["debounce_seconds"] > 0


# --- the happy path ---------------------------------------------------------


def test_a_mentioning_issue_starts_a_run(client: TestClient, _no_real_runs: list[str]) -> None:
    body = client.post("/jira/webhook", json=issue_event()).json()
    assert body["accepted"] is True
    assert body["issue_key"] == "ORD-1"
    assert body["event"] == "jira:issue_created"
    assert _no_real_runs == ["ORD-1"]


def test_a_mention_in_a_comment_starts_a_run(client: TestClient, _no_real_runs: list[str]) -> None:
    payload = issue_event(description="Nothing here.", comment="@Aetherion please break this down.")
    assert client.post("/jira/webhook", json=payload).json()["accepted"] is True
    assert _no_real_runs == ["ORD-1"]


def test_the_automation_shorthand_payload_is_accepted(
    client: TestClient, _no_real_runs: list[str]
) -> None:
    # A Jira Automation rule usually sends only this.
    body = client.post("/jira/webhook", json={"issue_key": "ORD-7"}).json()
    assert body["accepted"] is True
    assert _no_real_runs == ["ORD-7"]


def test_the_issue_key_is_uppercased(client: TestClient, _no_real_runs: list[str]) -> None:
    client.post("/jira/webhook", json={"issue_key": "ord-9"})
    assert _no_real_runs == ["ORD-9"]


# --- loop prevention --------------------------------------------------------


def test_the_agents_own_reply_is_ignored(client: TestClient, _no_real_runs: list[str]) -> None:
    """The reply comment must never start another run.

    Recognised by its signature, not by who posted it — the agent usually runs
    as the same Jira account as the people using it.
    """
    payload = issue_event(
        author_id="agent-account-id",
        comment="AetherionAgent · Work breakdown created. Created ORD-2. "
        "— AetherionAgent · automated reply",
    )
    body = client.post("/jira/webhook", json=payload).json()
    assert body["accepted"] is False
    assert body["reason"] == "own_change"
    assert _no_real_runs == []


def test_a_persons_comment_starts_a_run_even_on_the_agents_account(
    client: TestClient, _no_real_runs: list[str]
) -> None:
    """A request is a comment; it must run whoever's account posted it."""
    payload = issue_event(
        author_id="agent-account-id",
        comment="@Aetherion Allow drivers to record a rest break.",
    )
    assert client.post("/jira/webhook", json=payload).json()["accepted"] is True
    assert _no_real_runs == ["ORD-1"]


def test_a_person_sharing_the_agents_jira_account_is_not_ignored(
    client: TestClient, _no_real_runs: list[str]
) -> None:
    """The commonest single-account setup: the human IS the agent's account.

    Ignoring everything that account does ignored the human too — on project
    TT2 every ticket a person created was dropped as "caused by the agent
    itself", so nothing was ever built.
    """
    body = client.post("/jira/webhook", json=issue_event(author_id="agent-account-id")).json()
    assert body["accepted"] is True
    assert _no_real_runs == ["ORD-1"]


def test_a_label_only_update_by_the_agent_is_ignored(
    client: TestClient, _no_real_runs: list[str]
) -> None:
    """Setting ltw-processed must not start a run on the ticket it marks."""
    payload = issue_event(author_id="agent-account-id")
    payload["webhookEvent"] = "jira:issue_updated"
    payload["changelog"] = {"items": [{"field": "labels"}]}
    assert client.post("/jira/webhook", json=payload).json()["reason"] == "own_change"
    assert _no_real_runs == []


def test_a_description_edit_is_processed_even_from_the_agent_account(
    client: TestClient, _no_real_runs: list[str]
) -> None:
    """Rewording a ticket after a clarification request must re-run it."""
    payload = issue_event(author_id="agent-account-id")
    payload["webhookEvent"] = "jira:issue_updated"
    payload["changelog"] = {"items": [{"field": "description"}]}
    assert client.post("/jira/webhook", json=payload).json()["accepted"] is True
    assert _no_real_runs == ["ORD-1"]


def test_an_issue_the_agent_just_created_is_ignored(
    client: TestClient, _no_real_runs: list[str]
) -> None:
    """Created Stories carry the idempotency label; they are not new requests."""
    payload = issue_event(author_id="agent-account-id")
    payload["issue"]["fields"]["labels"] = ["ltw-d444700f82ef14e4"]
    assert client.post("/jira/webhook", json=payload).json()["reason"] == "own_change"
    assert _no_real_runs == []


def test_an_event_without_the_keyword_is_ignored(
    client: TestClient, _no_real_runs: list[str]
) -> None:
    payload = issue_event(description="Please review the refund policy.")
    body = client.post("/jira/webhook", json=payload).json()
    assert body["reason"] == "no_keyword"
    assert _no_real_runs == []


def test_repeated_deliveries_start_only_one_run(
    client: TestClient, _no_real_runs: list[str]
) -> None:
    """One edit in Jira can produce several deliveries; only one may run."""
    for _ in range(5):
        client.post("/jira/webhook", json=issue_event())
    assert _no_real_runs == ["ORD-1"], "the other four must be debounced"


def test_different_issues_are_not_debounced_against_each_other(
    client: TestClient, _no_real_runs: list[str]
) -> None:
    client.post("/jira/webhook", json=issue_event(key="ORD-1"))
    client.post("/jira/webhook", json=issue_event(key="ORD-2"))
    assert _no_real_runs == ["ORD-1", "ORD-2"]


def test_the_debounce_window_is_configurable(
    client: TestClient, _no_real_runs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LTW_WEBHOOK_DEBOUNCE_SECONDS", "0")
    client.post("/jira/webhook", json=issue_event())
    client.post("/jira/webhook", json=issue_event())
    assert len(_no_real_runs) == 2


# --- the shared secret ------------------------------------------------------


def test_a_secret_is_required_when_configured(
    raw_client: TestClient, _no_real_runs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LTW_WEBHOOK_SECRET", "s3cret")
    assert raw_client.post("/jira/webhook", json=issue_event()).status_code == 401
    assert _no_real_runs == []


def test_the_right_secret_is_accepted_in_a_header(
    raw_client: TestClient, _no_real_runs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LTW_WEBHOOK_SECRET", "s3cret")
    resp = raw_client.post("/jira/webhook", json=issue_event(), headers={"X-LTW-Secret": "s3cret"})
    assert resp.status_code == 200
    assert _no_real_runs == ["ORD-1"]


def test_the_secret_also_works_as_a_query_parameter(
    raw_client: TestClient, _no_real_runs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LTW_WEBHOOK_SECRET", "s3cret")
    resp = raw_client.post("/jira/webhook?secret=s3cret", json=issue_event())
    assert resp.status_code == 200


def test_a_wrong_secret_is_refused(
    raw_client: TestClient, _no_real_runs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LTW_WEBHOOK_SECRET", "s3cret")
    resp = raw_client.post("/jira/webhook", json=issue_event(), headers={"X-LTW-Secret": "wrong"})
    assert resp.status_code == 401


# --- malformed input --------------------------------------------------------


def test_a_payload_without_an_issue_key_is_ignored(
    client: TestClient, _no_real_runs: list[str]
) -> None:
    body = client.post("/jira/webhook", json={"webhookEvent": "jira:issue_created"}).json()
    assert body["reason"] == "no_issue_key"
    assert _no_real_runs == []


def test_a_non_object_body_is_rejected(client: TestClient) -> None:
    assert client.post("/jira/webhook", json=["not", "an", "object"]).status_code == 400


def test_an_unhandled_run_error_does_not_kill_the_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_key: str, _comment_id: str = "") -> dict[str, Any]:
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(server, "run_agent", boom)
    # The delivery is still accepted, and the background failure is swallowed.
    assert client.post("/jira/webhook", json=issue_event()).json()["accepted"] is True


# --- Jira System Webhook signatures -----------------------------------------
# A Jira **Automation** rule can set any header, so it sends the secret as
# X-LTW-Secret. A Jira **System Webhook** cannot set headers: it signs the body
# with the secret and sends X-Hub-Signature instead. Both must work.

import hashlib  # noqa: E402
import hmac as _hmac  # noqa: E402
import json as _json  # noqa: E402

ATLASSIAN_SECRET = "It's a Secret to Everybody"
ATLASSIAN_BODY = b"Hello World!"
ATLASSIAN_SIGNATURE = "sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9"


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_matches_atlassians_published_test_vector() -> None:
    """Atlassian documents this exact pair; matching it proves the algorithm."""
    assert server.verify_signature(ATLASSIAN_BODY, ATLASSIAN_SIGNATURE, ATLASSIAN_SECRET)


def test_a_wrong_secret_fails_the_signature() -> None:
    assert not server.verify_signature(ATLASSIAN_BODY, ATLASSIAN_SIGNATURE, "wrong")


def test_a_tampered_body_fails_the_signature() -> None:
    assert not server.verify_signature(b"Hello World?", ATLASSIAN_SIGNATURE, ATLASSIAN_SECRET)


@pytest.mark.parametrize("header", ["", "no-equals-sign", "md9=abc", "sha256="])
def test_malformed_signature_headers_are_refused(header: str) -> None:
    assert not server.verify_signature(ATLASSIAN_BODY, header, ATLASSIAN_SECRET)


def test_a_system_webhook_with_a_valid_signature_is_accepted(
    raw_client: TestClient, _no_real_runs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LTW_JIRA_WEBHOOK_SECRET", "jira-generated-secret")
    body = _json.dumps(issue_event()).encode()

    resp = raw_client.post(
        "/jira/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature": sign(body, "jira-generated-secret"),
        },
    )

    assert resp.status_code == 200
    assert _no_real_runs == ["ORD-1"]


def test_a_system_webhook_with_a_bad_signature_is_refused(
    raw_client: TestClient, _no_real_runs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LTW_JIRA_WEBHOOK_SECRET", "jira-generated-secret")
    body = _json.dumps(issue_event()).encode()

    resp = raw_client.post(
        "/jira/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature": sign(body, "the-wrong-secret"),
        },
    )

    assert resp.status_code == 401
    assert _no_real_runs == []


def test_either_style_is_accepted_when_both_are_configured(
    raw_client: TestClient, _no_real_runs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One deployment can serve an Automation rule and a System Webhook."""
    monkeypatch.setenv("LTW_WEBHOOK_SECRET", "header-style")
    monkeypatch.setenv("LTW_JIRA_WEBHOOK_SECRET", "signature-style")

    # the Automation style
    assert (
        raw_client.post(
            "/jira/webhook",
            json=issue_event(key="ORD-1"),
            headers={"X-LTW-Secret": "header-style"},
        ).status_code
        == 200
    )

    # the System Webhook style
    body = _json.dumps(issue_event(key="ORD-2")).encode()
    assert (
        raw_client.post(
            "/jira/webhook",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": sign(body, "signature-style"),
            },
        ).status_code
        == 200
    )
    assert _no_real_runs == ["ORD-1", "ORD-2"]


def test_health_reports_which_secret_styles_are_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This deployment uses the signature style only, so the header style that
    # the rest of these tests configure has to be taken away.
    monkeypatch.delenv("LTW_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("LTW_JIRA_WEBHOOK_SECRET", "signature-style")
    body = client.get("/health").json()
    assert body["secret_required"] is True
    assert body["jira_signature_configured"] is True
    assert body["header_secret_configured"] is False


def test_the_root_page_explains_the_service(client: TestClient) -> None:
    """Opening the URL in a browser should say what this is, not just 404."""
    body = client.get("/").json()
    assert "webhook receiver" in body["service"]
    assert "POST /jira/webhook" in body["endpoints"]
    assert "/jira/webhook" in body["jira_webhook_url"]


def test_posting_to_the_root_says_what_the_correct_url_is(client: TestClient) -> None:
    """Pointing the Jira webhook at the bare domain is the commonest mistake."""
    resp = client.post("/", json={"issue_key": "ORD-1"})
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "/jira/webhook" in detail
    assert "Update the URL in Jira" in detail


def test_an_unconfigured_deployment_refuses_every_delivery(
    raw_client: TestClient, _no_real_runs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed. This endpoint creates Jira issues.

    A blank secret used to skip the check entirely, which turned a
    misconfiguration into an open write endpoint that anyone who found the URL
    could drive.
    """
    monkeypatch.delenv("LTW_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("LTW_JIRA_WEBHOOK_SECRET", raising=False)

    resp = raw_client.post("/jira/webhook", json=issue_event())

    assert resp.status_code == 503
    assert _no_real_runs == [], "nothing may run on an unauthenticated deployment"


def test_nothing_the_agent_posts_can_retrigger_it() -> None:
    """The agent comments on the ticket it was triggered from.

    Locally the signature check catches its own reply. A platform subscription
    routing on "comment body contains @Aetherion" has no such check, so any
    reply text carrying that mention loops forever. The Bucket A reply used to
    say "Mention @Aetherion with a description of what should be built".
    """
    import asyncio

    from src.jira.api import trigger_keyword
    from src.tools.tools import validate_requirement

    mention = f"@{trigger_keyword()}"

    result = asyncio.run(validate_requirement("hi", {"project": {"key": "FL"}}))
    assert mention not in result["reason"], result["reason"]

    from src.jira.api import AGENT_FOOTER, AGENT_SIGNATURE

    assert mention not in AGENT_SIGNATURE
    assert mention not in AGENT_FOOTER
