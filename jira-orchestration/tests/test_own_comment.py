"""The system's own comments are skipped; a person naming the organisation is not (2026-10-15)."""

import pytest

from shared.keywords import AGENT_FOOTER, AGENT_SIGNATURE, is_agent_comment


@pytest.mark.parametrize(
    "comment",
    [
        "@Aetherion code in AetherionAgentDemo/bank-transfer-service",
        "@Aetherion review — the repo is AetherionAgentDemo/bank-transfer-service",
        "see https://github.com/AetherionAgentDemo/bank-transfer-service/pull/22",
    ],
)
def test_a_person_naming_the_organisation_is_answered(comment) -> None:
    assert not is_agent_comment(comment)


@pytest.mark.parametrize(
    "comment",
    [
        f"{AGENT_SIGNATURE} · Requirement review — nothing created\nHandled by …\n\n{AGENT_FOOTER}",
        f"{AGENT_SIGNATURE} · Status of BTS-15\nNothing is running.",
        f"Planning's answer …\n{AGENT_FOOTER}",
        f"{AGENT_SIGNATURE} · Code for AetherionAgentDemo/bank-transfer-service\n{AGENT_FOOTER}",
    ],
)
def test_the_systems_own_replies_are_still_skipped(comment) -> None:
    assert is_agent_comment(comment)
