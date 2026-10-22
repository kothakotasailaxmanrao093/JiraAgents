"""The gates that decide whether a delivery deserves a run at all.

Two of the four are pure and live here. They are the cheapest and the most
consequential: gate 1 stops the system reacting to ordinary conversation, and
gate 2 stops it reacting to itself — which, without the guard, never stops.
"""

from __future__ import annotations

import pytest

from routing.ingress import IgnoreReason, idempotency_key, screen_comment
from shared.keywords import AGENT_FOOTER, AGENT_SIGNATURE


def test_a_comment_mentioning_the_agent_proceeds() -> None:
    assert screen_comment("@Aetherion build a thing").ignored is None


@pytest.mark.parametrize(
    "body",
    ["just talking to my colleague", "", "   ", "we should build this next sprint"],
)
def test_a_comment_without_the_keyword_is_dropped_silently(body: str) -> None:
    """Most comments on a busy project are people talking to each other."""
    verdict = screen_comment(body)
    assert verdict.ignored is not None
    assert verdict.ignored is IgnoreReason.NO_KEYWORD


@pytest.mark.parametrize(
    "body",
    [
        # KS-17185 (2026-10-06): a colleague's note naming the agent was answered
        # on a real team's ticket. Only an explicit "@Aetherion" asks for work.
        "Hey @Tyler Judkins here is the first version of the reports created by Aetherion Agent",
        "Aetherion build a thing",
        "mail the summary to bot@aetherion.io",
    ],
)
def test_naming_the_agent_without_the_mention_is_dropped(body: str) -> None:
    verdict = screen_comment(body)
    assert verdict.ignored is not None
    assert verdict.ignored is IgnoreReason.NO_KEYWORD


def test_a_near_miss_on_the_keyword_does_not_fire() -> None:
    """Whole-word matching: "aetherionx" is not a mention."""
    assert screen_comment("@aetherionx build a thing").ignored is not None


def test_the_keyword_is_matched_case_insensitively() -> None:
    assert screen_comment("@aetherion build a thing").ignored is None


def test_the_keyword_is_configurable() -> None:
    assert screen_comment("@Helper build x", keyword="Helper").ignored is None
    assert screen_comment("@Aetherion build x", keyword="Helper").ignored is not None


# --- the self-guard, which is the one that must never fail -------------------


def test_the_systems_own_reply_can_never_start_a_run() -> None:
    """Without this the system answers its own reply, forever."""
    own_reply = f"{AGENT_SIGNATURE} · Work breakdown created\n\n@Aetherion\n{AGENT_FOOTER}"
    assert screen_comment(own_reply).ignored is not None


def test_the_signature_alone_is_enough_to_stop_a_run() -> None:
    assert screen_comment(f"@Aetherion build x\n{AGENT_FOOTER}").ignored is not None


def test_the_bot_account_is_recognised_when_configured() -> None:
    """Cheaper and more reliable than a string match, when it is known."""
    verdict = screen_comment(
        "@Aetherion build x", author_account_id="bot-1", bot_account_id="bot-1"
    )
    assert verdict.ignored is not None
    assert "own account" in verdict.ignored.value


def test_a_human_on_a_different_account_still_proceeds() -> None:
    assert (
        screen_comment(
            "@Aetherion build x", author_account_id="human-9", bot_account_id="bot-1"
        ).ignored
        is None
    )


def test_the_signature_guard_works_even_with_no_account_configured() -> None:
    """Identity cannot decide this: the agent usually posts as the same Jira
    account as the people using it."""
    assert screen_comment(f"{AGENT_SIGNATURE} · @Aetherion said so").ignored is not None


# --- the keys ----------------------------------------------------------------


def test_the_idempotency_key_includes_the_event() -> None:
    """Editing a comment to add a new instruction IS a new request."""
    assert idempotency_key("10501", "comment_created") != idempotency_key(
        "10501", "comment_updated"
    )


def test_the_same_delivery_produces_the_same_key() -> None:
    assert idempotency_key("10501", "comment_created") == idempotency_key(
        " 10501 ", " comment_created "
    )
