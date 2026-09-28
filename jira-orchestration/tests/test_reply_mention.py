"""A Reply in Jira pre-fills "@<author>", which hid the verb (BGV-1, 2026-09-24).

The exact ADF Jira stored for comment 10815: a mention node for the person being
replied to, then the text the user typed.
"""

from __future__ import annotations

from routing.classify import classify_by_verb
from shared.adf import comment_text


def _reply(*inline):
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": list(inline)}],
    }


MENTION = {"type": "mention", "attrs": {"id": "712020:x", "text": "@Kothakota Sai Laxman Rao"}}


def test_the_prefilled_mention_is_dropped_so_the_verb_leads() -> None:
    body = comment_text(_reply(MENTION, {"type": "text", "text": " @Aetherion review"}))
    assert body == "@Aetherion review"
    decision = classify_by_verb(body)
    assert decision is not None and decision.layer == 1 and decision.intent == "REVIEW"


def test_a_mention_later_in_the_comment_is_kept() -> None:
    body = comment_text(
        _reply({"type": "text", "text": "@Aetherion build and assign it to "}, MENTION)
    )
    assert body.endswith("@Kothakota Sai Laxman Rao")


def test_a_mention_of_the_agent_itself_is_never_dropped() -> None:
    agent = {"type": "mention", "attrs": {"text": "@Aetherion"}}
    body = comment_text(_reply(agent, {"type": "text", "text": " review"}), keep="Aetherion")
    assert body == "@Aetherion review"
