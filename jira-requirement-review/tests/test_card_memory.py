"""The review's memory of a card (2026-10-30): questions kept encrypted and
never asked twice, answers in the people's own words, earlier cards found with
their exact branches and pull requests, services ranked with honest reasons."""

from __future__ import annotations

import base64
import json
import os

import pytest
from fake_memsy import FakeMemsy

from agent import memory_modes
from card_memory import card, history, model, questions, ranking, store

REPOS = (
    "imps-service: AetherionAgentDemo/imps-service\n"
    "payments-service: AetherionAgentDemo/payments-service@develop\n"
    "statement-service: AetherionAgentDemo/statement-service\n"
    "loan-service: AetherionAgentDemo/loan-service"
)
CARD = (
    "Add a remark to IMPS payments\n"
    "Customers should be able to add a remark when sending an IMPS payment. The remark "
    "must appear on the account statement.\n=== spec.pdf ===\nPOST /imps/transfer gains remark"
)
LIKE_NEFT = "Remark like NEFT for IMPS"
ASKED = [
    "Does the 50-character rule apply to IMPS remarks?",
    "Is the remark cut when the statement line is too long?",
    "Should the SMS confirmation include the remark?",
]


@pytest.fixture
def memsy(monkeypatch):
    fake = FakeMemsy()
    monkeypatch.setattr(store, "client", lambda: fake)
    monkeypatch.setenv(store.KEY_NAME, base64.b64encode(os.urandom(32)).decode())
    return fake


@pytest.fixture
def llm(monkeypatch):
    """The model: told per test which answers comments give and how services rank."""
    said = {"answers": [], "services": None, "calls": []}

    async def ask_json(system, prompt):
        said["calls"].append(prompt)
        if system == questions.MATCH_SYSTEM_PROMPT:
            return {"answers": said["answers"]}
        if said["services"] is None:
            raise RuntimeError("gateway down")
        return {"services": said["services"]}

    monkeypatch.setattr(model, "ask_json", ask_json)
    return said


async def _review(key, comments, asked, title="Add a remark to IMPS payments", text=CARD):
    memory = await card.recall(key, title, text, comments, REPOS)
    return memory, card.keep(key, memory, asked, title, "Needs minor clarification.")


async def test_questions_are_kept_encrypted_and_the_card_goes_into_history(memsy, llm):
    _, kept = await _review("BTS-60", [], ASKED)
    assert [q["question"] for q in kept["questions"]] == ASKED
    (snapshot,) = memsy.stored("review:BTS-60")
    assert "50-character" not in snapshot.metadata
    assert json.loads(snapshot.metadata)["format"] == store.FORMAT


async def test_answers_are_the_comments_own_words_and_nothing_is_asked_twice(memsy, llm):
    await _review("BTS-60", [], ASKED)
    llm["answers"] = [{"comment_id": "10520", "question_id": "Q1"}]
    comments = [{"id": "10520", "text": "Yes, same as NEFT."}]
    memory, kept = await _review(
        "BTS-60", comments, ["Does the 50 character rule apply to the IMPS remarks?"]
    )
    assert len(kept["questions"]) == 3
    assert kept["questions"][0]["answers"] == ["Yes, same as NEFT."]
    shown = card.document(memory)["text"]
    assert "Answer: Yes, same as NEFT." in shown
    assert "Should the SMS confirmation include the remark?" in shown


async def test_a_comment_matched_once_is_not_sent_to_the_model_again(memsy, llm):
    await _review("BTS-60", [], ASKED)
    llm["answers"] = [{"comment_id": "1", "question_id": "Q2"}]
    comments = [{"id": "1", "text": "Never cut; wrap to a second line."}]
    await _review("BTS-60", comments, [])
    before = len(llm["calls"])
    memory, _ = await _review("BTS-60", comments, [])
    matching = [c for c in llm["calls"][before:] if c.startswith("QUESTIONS:")]
    assert matching == []
    assert memory["snapshot"]["questions"][1]["answers"][0]["text"].startswith("Never cut")


async def test_when_the_model_is_down_comments_are_matched_next_time(memsy, llm, monkeypatch):
    await _review("BTS-60", [], ASKED)

    async def down(system, prompt):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(model, "ask_json", down)
    comments = [{"id": "7", "text": "Yes, same as NEFT."}]
    memory, _ = await _review("BTS-60", comments, [])
    assert "7" not in memory["snapshot"].get("seen", {})


def _earlier_work():
    history.write(
        "BTS-20",
        "card",
        {"title": "NEFT remark", "services": ["payments-service"], "summary": "remark on NEFT"},
    )
    history.write(
        "BTS-20",
        "pr_opened",
        {
            "title": "NEFT remark",
            "repo": "AetherionAgentDemo/payments-service",
            "pr": 41,
            "url": "https://github.com/AetherionAgentDemo/payments-service/pull/41",
            "branch": "feature/BTS-20-neft-remark",
            "service": "payments-service",
        },
        "41",
    )
    history.write(
        "BTS-20",
        "pr_state",
        {
            "repo": "AetherionAgentDemo/payments-service",
            "pr": 41,
            "state": "merged",
            "files": ["src/RemarkValidator.java"],
        },
        "41-merged",
    )


async def test_an_earlier_card_is_found_with_its_branch_pr_and_merge(memsy, llm):
    _earlier_work()
    llm["services"] = [
        {"service": "imps-service", "rank": 9, "reason": "spec.pdf names POST /imps/transfer"},
        {"service": "payments-service", "rank": 8, "reason": "BTS-20 PR #41 added the remark"},
        {"service": "statement-service", "rank": 7, "reason": "the remark is on the statement"},
        {"service": "loan-service", "rank": 0, "reason": "not touched"},
    ]
    memory, kept = await _review("BTS-60", [], ASKED, title=LIKE_NEFT)
    (earlier,) = memory["related"]
    assert earlier["card"] == "BTS-20"
    (pr,) = earlier["prs"]
    assert (pr["branch"], pr["state"], pr["files"]) == (
        "feature/BTS-20-neft-remark",
        "merged",
        ["src/RemarkValidator.java"],
    )
    services = {s["service"]: s for s in kept["services"]}
    assert list(services) == ["imps-service", "payments-service", "statement-service"]
    assert services["payments-service"]["branch"] == "develop"
    assert services["payments-service"]["prs"][0]["pr"] == 41
    assert "BTS-20" in card.document(memory)["text"]
    card_events = [
        json.loads(e.metadata) for e in memsy.stored("work:BTS") if '"event": "card"' in e.metadata
    ]
    assert any(e["key"] == "BTS-60" for e in card_events)


async def test_a_reason_naming_something_not_found_is_replaced_by_the_evidence(memsy, llm):
    _earlier_work()
    llm["services"] = [{"service": "payments-service", "rank": 8, "reason": "BTS-99 PR #77 did it"}]
    memory, _ = await _review("BTS-60", [], [], title=LIKE_NEFT)
    (service,) = [s for s in memory["services"] if s["service"] == "payments-service"]
    assert "BTS-99" not in service["reason"] and "BTS-20" in service["reason"]


def test_without_the_model_a_named_service_is_8_and_an_earlier_ones_6():
    found = ranking.candidates(
        "The statement-service shows it",
        ["imps-service", "payments-service", "statement-service"],
        [{"card": "BTS-20", "title": "NEFT", "services": ["payments-service"], "prs": []}],
    )
    out = {s["service"]: s["rank"] for s in ranking.ranked(found, None, "")}
    assert out == {"statement-service": 8, "payments-service": 6}


async def test_for_a_tdd_the_answers_since_the_review_are_added(memsy, llm):
    llm["services"] = [{"service": "imps-service", "rank": 9, "reason": "the card names IMPS"}]
    await _review("BTS-60", [], ASKED)
    llm["answers"] = [{"comment_id": "9", "question_id": "Q3"}]
    out = await card.refresh(
        "BTS-60", "IMPS remark", CARD, [{"id": "9", "text": "No SMS change."}], REPOS
    )
    assert out["questions"][2]["answers"] == ["No SMS change."]
    assert [s["service"] for s in out["services"]] == ["imps-service"]


async def test_a_card_never_reviewed_with_its_memory_is_ranked_for_its_tdd(memsy, llm):
    llm["services"] = [{"service": "statement-service", "rank": 7, "reason": "on the statement"}]
    out = await card.refresh("BTS-61", "Statement remark", "Show it", [], REPOS)
    assert out["services"][0]["service"] == "statement-service"
    _, stored = questions.load("BTS-61")
    assert stored["services"][0]["service"] == "statement-service"


def test_without_the_key_nothing_is_stored(memsy, monkeypatch):
    monkeypatch.delenv(store.KEY_NAME)
    monkeypatch.setattr(store, "secret", lambda name: "")
    with pytest.raises(store.MemoryUnavailable):
        questions.save("BTS-60", 1, {"questions": []})
    assert memsy.events == []


def test_without_memsy_settings_the_client_says_so(monkeypatch):
    monkeypatch.setattr(store, "secret", lambda name: "")
    with pytest.raises(store.MemoryUnavailable):
        store.client()


async def test_the_history_mode_writes_one_event(memsy):
    async def execute(name, *args, **_):
        from tools import card_memory_tool

        return await getattr(card_memory_tool, name)(*args)

    out = await memory_modes.card_history(
        {"issue_key": "BTS-60", "event": "design", "data": {"version": "v1"}, "event_id": "v1"},
        execute,
    )
    assert out == {"status": "success", "mode": "history", "actor": "work:BTS"}
    assert "TDD v1" in memsy.stored("work:BTS")[0].content


async def test_the_modes_report_a_missing_key_or_a_failure():
    async def failing(name, *args, **_):
        return {"ok": False, "error": "MemoryUnavailable: not set"}

    assert (await memory_modes.card_memory({}, failing))["status"] == "error"
    out = await memory_modes.card_memory({"issue_key": "BTS-60"}, failing)
    assert out == {"status": "error", "message": "MemoryUnavailable: not set"}
