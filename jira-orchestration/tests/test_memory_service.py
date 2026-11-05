"""The agents' memory, kept by Orchestration alone (2026-10-30).

Questions kept encrypted and never asked twice; answers in the people's own
words, linked by "Q2:" or by a model that must quote the comment; one search
per review and none when the card is unchanged; earlier cards found with their
exact branches, PRs and merges; services ranked with honest reasons;
decisions kept in one place; Planning's existing TDD memory read as it is.
"""

from __future__ import annotations

import base64
import json
import os

import pytest

from memory_service import cards, designs, history, model, ranking, store
from tests.fake_memsy import FakeMemsy
from tools import memory_tools

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
# The fake searches by shared words, so a related card is a short text here;
# the real Memsy searches the whole card by meaning (checked live).
SHORT = "A remark on IMPS like the NEFT remark"
ASKED = [
    "Does the 50-character rule apply to IMPS remarks?",
    "Is the remark cut when the statement line is too long?",
    "Should the SMS confirmation include the remark?",
]
# A test key (bytes 0..31), and a snapshot Planning's own code_crypto sealed
# with it under tdd:BTS-25, seq 3 — Planning's TDD memory as it is stored.
TEST_KEY = base64.b64encode(bytes(range(32))).decode()
PLANNING_SEALED = (
    "enc:v1:8016afa0:mgVxVlXwaGNENz9uTqdCR9lfaD32RxC0iCIhW9/PoB51FmVj72PumhKZljNvNja9xzJE3B1rx"
    "QDDsCeUkEX5Przo3VEfNsVg5OOrVDKK2UV0dmmxM6ggTST46wwfk8YYH/mrbhyqBVkTEI5HqwZPHwIqR1aXVYePZY"
    "6BdP77gSYNHL/srAnSCvwfYh1Ym6IKuzPxWowe0TjTClSMho3Jf/V+Z7zNNdcV"
)


@pytest.fixture
def memsy(monkeypatch):
    fake = FakeMemsy()
    monkeypatch.setattr(store, "service", lambda: fake)
    monkeypatch.setattr(store, "_lister", lambda: fake.lister)
    monkeypatch.setenv(store.KEY_NAME, base64.b64encode(os.urandom(32)).decode())
    return fake


@pytest.fixture
def llm(monkeypatch):
    """The model: told per test which answers comments give and how services rank."""
    said = {"answers": [], "services": None, "repeats": [], "calls": []}

    async def ask_json(system, prompt):
        said["calls"].append(system)
        if system == cards.MATCH_SYSTEM_PROMPT:
            return {"answers": said["answers"]}
        if system == cards.SAME_SYSTEM_PROMPT:
            return {"repeats": said["repeats"]}
        if said["services"] is None:
            raise RuntimeError("gateway down")
        return {"services": said["services"]}

    monkeypatch.setattr(model, "ask_json", ask_json)
    return said


async def _review(key, comments, asked, title="Add a remark to IMPS payments", text=CARD):
    memory = await cards.recall(key, title, text, comments, REPOS)
    return memory, await cards.keep(key, memory, asked, title, "Needs minor clarification.")


async def _earlier_work():
    await history.write(
        "BTS-20",
        "card",
        {"title": "NEFT remark", "services": ["payments-service"], "summary": "remark on NEFT"},
    )
    await history.write(
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
    await history.write(
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


# --- the store ------------------------------------------------------------------------------


def test_planning_s_existing_tdd_memory_is_read_as_it_is(monkeypatch):
    monkeypatch.setenv(store.KEY_NAME, TEST_KEY)
    found = store.unseal(PLANNING_SEALED, "tdd:BTS-25", 3)
    assert found["facts"][0]["statement"] == "2,00,000 INR a day"
    assert found["versions"] == [{"version": "v1.0", "page_id": "9"}]


def test_a_snapshot_moved_to_another_card_or_number_does_not_decrypt(monkeypatch):
    monkeypatch.setenv(store.KEY_NAME, TEST_KEY)
    with pytest.raises(store.MemoryUnavailable):
        store.unseal(PLANNING_SEALED, "tdd:BTS-26", 3)
    with pytest.raises(store.MemoryUnavailable):
        store.unseal(PLANNING_SEALED, "tdd:BTS-25", 4)


async def test_a_large_snapshot_is_split_into_sessions_and_joined_back(memsy):
    many = [{"id": f"Q{i}", "question": os.urandom(40).hex(), "answers": []} for i in range(60)]
    await cards.save("BTS-60", 1, {"questions": many})
    parts = memsy.stored("review:BTS-60")
    assert len(parts) > 1 and all(len(p.metadata) <= 4096 for p in parts)
    assert len({p.session_id for p in parts}) == len(parts)  # Memsy merges one session
    assert await cards.load("BTS-60") == (1, {"questions": many})


async def test_without_the_key_nothing_is_stored(memsy, monkeypatch):
    monkeypatch.setattr(store, "secret", lambda name, default="": "")
    with pytest.raises(store.MemoryUnavailable):
        await cards.save("BTS-60", 1, {"questions": []})
    assert memsy.events == []


def test_without_memsy_settings_the_service_says_so(monkeypatch):
    import common_lib.memsy

    unconfigured = type("Unconfigured", (), {"is_configured": False})()
    monkeypatch.setattr(common_lib.memsy, "get_memsy_service", lambda: unconfigured)
    with pytest.raises(store.MemoryUnavailable):
        store.service()


async def test_a_write_the_service_did_not_take_raises(memsy, monkeypatch):
    # Aetherion's service never raises; it returns no ids when a write failed.
    async def refused(events):
        return []

    monkeypatch.setattr(memsy, "ingest", refused)
    with pytest.raises(store.MemoryUnavailable):
        await history.write("BTS-60", "card", {"title": "t", "services": []})


# --- questions and answers ------------------------------------------------------------------


async def test_questions_are_kept_encrypted_and_the_card_goes_into_history(memsy, llm):
    _, kept = await _review("BTS-60", [], ASKED)
    assert [q["question"] for q in kept["questions"]] == ASKED
    (snapshot,) = memsy.stored("review:BTS-60")
    assert "50-character" not in snapshot.metadata
    assert json.loads(snapshot.metadata)["format"] == store.FORMAT


async def test_answers_are_the_comments_own_words_and_nothing_is_asked_twice(memsy, llm):
    await _review("BTS-60", [], ASKED)
    llm["answers"] = [{"comment_id": "10520", "question_id": "Q1", "quote": "Yes, same as NEFT"}]
    comments = [{"id": "10520", "text": "Yes, same as NEFT."}]
    memory, kept = await _review(
        "BTS-60", comments, ["Does the 50 character rule apply to the IMPS remarks?"]
    )
    assert len(kept["questions"]) == 3
    assert kept["questions"][0]["answers"] == ["Yes, same as NEFT."]
    shown = cards.document(memory)["text"]
    assert "Q1 Does the 50-character rule apply to IMPS remarks? Answer: Yes, same as NEFT." in (
        shown
    )
    assert "Q3 Should the SMS confirmation include the remark?" in shown


async def test_a_question_asked_again_in_other_words_is_not_kept_twice(memsy, llm):
    # BTS-39, live 2026-10-30: Q5 asked what Q2 asked, in other words.
    await _review("BTS-60", [], ["Should the note appear in all export formats or only one?"])
    llm["repeats"] = [{"new": 0, "kept": "Q1"}]
    _, kept = await _review(
        "BTS-60",
        [],
        [
            "Must the note be included in every export format supported by history?",
            "Where does the note column go?",
        ],
    )
    assert [q["question"] for q in kept["questions"]] == [
        "Should the note appear in all export formats or only one?",
        "Where does the note column go?",
    ]


async def test_the_review_is_told_not_to_raise_open_questions_again(memsy, llm):
    await _review("BTS-60", [], ASKED)
    memory, _ = await _review("BTS-60", [], [])
    assert "do not raise them again, in these or other words" in cards.document(memory)["text"]


async def test_a_numbered_answer_needs_no_model(memsy, llm, monkeypatch):
    await _review("BTS-60", [], ASKED)

    async def down(system, prompt):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(model, "ask_json", down)
    comments = [{"id": "7", "text": "Q3: No, the SMS stays as it is."}]
    memory = await cards.recall("BTS-60", "IMPS remark", CARD, comments, REPOS)
    q3 = memory["snapshot"]["questions"][2]
    assert q3["answers"] == [{"comment_id": "7", "text": "No, the SMS stays as it is."}]


async def test_a_model_link_that_does_not_quote_the_comment_is_dropped(memsy, llm):
    await _review("BTS-60", [], ASKED)
    llm["answers"] = [
        {"comment_id": "8", "question_id": "Q2", "quote": "never cut the remark"},
        {"comment_id": "8", "question_id": "Q1", "quote": "fifty characters like NEFT"},
    ]
    comments = [{"id": "8", "text": "Yes, fifty characters like NEFT. Thanks!"}]
    memory, _ = await _review("BTS-60", comments, [])
    answers = {q["id"]: q["answers"] for q in memory["snapshot"]["questions"]}
    assert answers["Q2"] == [] and answers["Q1"][0]["comment_id"] == "8"


async def test_a_comment_matched_once_is_not_sent_to_the_model_again(memsy, llm):
    await _review("BTS-60", [], ASKED)
    llm["answers"] = [{"comment_id": "1", "question_id": "Q2", "quote": "Never cut"}]
    comments = [{"id": "1", "text": "Never cut; wrap to a second line."}]
    await _review("BTS-60", comments, [])
    before = llm["calls"].count(cards.MATCH_SYSTEM_PROMPT)
    memory, _ = await _review("BTS-60", comments, [])
    assert llm["calls"].count(cards.MATCH_SYSTEM_PROMPT) == before
    assert memory["snapshot"]["questions"][1]["answers"][0]["text"].startswith("Never cut")


async def test_a_comment_read_before_a_question_was_asked_still_answers_it(memsy, llm):
    """KS-6: the comments were read on the first review, before any question
    existed; the questions asked then were never matched to them."""
    comments = [{"id": "5", "text": "The SMS stays as it is; only the statement shows it."}]
    llm["answers"] = [{"comment_id": "5", "question_id": "Q3", "quote": "The SMS stays as it is"}]
    _, kept = await _review("BTS-61", comments, ASKED)
    answers = {q["id"]: q["answers"] for q in kept["questions"]}
    assert answers["Q3"] and answers["Q1"] == [] and answers["Q2"] == []
    calls = llm["calls"].count(cards.MATCH_SYSTEM_PROMPT)
    await _review("BTS-61", comments, [])
    # Checked once: an unchanged card is not sent to the model again.
    assert llm["calls"].count(cards.MATCH_SYSTEM_PROMPT) == calls


async def test_the_description_answers_a_question_in_its_own_words(memsy, llm):
    llm["answers"] = [
        {"comment_id": "description", "question_id": "Q1", "quote": "must appear on the account statement"}
    ]
    _, kept = await _review("BTS-62", [], ASKED)
    q1 = kept["questions"][0]
    assert q1["answers"] == ["The card says: must appear on the account statement"]


async def test_when_the_model_is_down_comments_are_matched_next_time(memsy, llm, monkeypatch):
    await _review("BTS-60", [], ASKED)

    async def down(system, prompt):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(model, "ask_json", down)
    memory, _ = await _review("BTS-60", [{"id": "7", "text": "Yes, same as NEFT."}], [])
    assert "7" not in memory["snapshot"].get("seen", {})


# --- earlier cards, ranking, searches ---------------------------------------------------------


async def test_an_earlier_card_is_found_with_its_branch_pr_and_merge(memsy, llm):
    await _earlier_work()
    llm["services"] = [
        {
            "service": "imps-service",
            "importance": 9,
            "reason": "spec.pdf names POST /imps/transfer",
        },
        {
            "service": "payments-service",
            "importance": 8,
            "reason": "BTS-20 PR #41 added the remark",
        },
        {
            "service": "statement-service",
            "importance": 7,
            "reason": "the remark is on the statement",
        },
        {"service": "loan-service", "importance": 0, "reason": "not touched"},
    ]
    memory, kept = await _review("BTS-60", [], ASKED, title=LIKE_NEFT, text=SHORT)
    (earlier,) = memory["related"]
    (pr,) = earlier["prs"]
    assert earlier["card"] == "BTS-20"
    assert (pr["branch"], pr["state"], pr["files"]) == (
        "feature/BTS-20-neft-remark",
        "merged",
        ["src/RemarkValidator.java"],
    )
    services = {s["service"]: s for s in kept["services"]}
    assert list(services) == ["imps-service", "payments-service", "statement-service"]
    assert services["payments-service"]["branch"] == "develop"
    assert services["payments-service"]["prs"][0]["pr"] == 41
    assert "BTS-20" in cards.document(memory)["text"]
    events = [json.loads(e.metadata) for e in memsy.stored("work:BTS")]
    assert any(e["key"] == "BTS-60" and e["event"] == "card" for e in events)


async def test_one_search_per_review_and_none_when_the_card_is_unchanged(memsy, llm, monkeypatch):
    await _earlier_work()
    searches = []
    real = memsy.search_rich

    async def counted(query, actor_id, **kwargs):
        searches.append(query)
        return await real(query, actor_id, **kwargs)

    monkeypatch.setattr(memsy, "search_rich", counted)
    await _review("BTS-60", [], ASKED, title=LIKE_NEFT, text=SHORT)
    assert len(searches) == 1
    memory, _ = await _review("BTS-60", [], [], title=LIKE_NEFT, text=SHORT)
    assert len(searches) == 1 and memory["related"][0]["card"] == "BTS-20"
    await _review("BTS-60", [], [], title=LIKE_NEFT, text=SHORT + " Also for UPI.")
    assert len(searches) == 2


async def test_a_reason_naming_something_not_found_is_replaced_by_the_evidence(memsy, llm):
    await _earlier_work()
    llm["services"] = [
        {"service": "payments-service", "importance": 8, "reason": "BTS-99 PR #77 did it"}
    ]
    memory, _ = await _review("BTS-60", [], [], title=LIKE_NEFT, text=SHORT)
    (service,) = [s for s in memory["services"] if s["service"] == "payments-service"]
    assert "BTS-99" not in service["reason"] and "BTS-20" in service["reason"]


def test_without_the_model_a_named_service_comes_before_an_earlier_card_s():
    found = ranking.candidates(
        "The statement-service shows it",
        ["imps-service", "payments-service", "statement-service"],
        [{"card": "BTS-20", "title": "NEFT", "services": ["payments-service"], "prs": []}],
    )
    out = [(s["rank"], s["service"]) for s in ranking.ranked(found, None, "")]
    assert out == [(1, "statement-service"), (2, "payments-service")]


def test_rank_1_is_the_main_service():
    # 2026-11-02: the rank is a position, as in a class: 1 is the main one.
    found = [{"service": n, "evidence": [], "prs": []} for n in ("a", "b", "c", "d")]
    answer = {
        "services": [
            {"service": "a", "importance": 6, "reason": "r"},
            {"service": "b", "importance": 9, "reason": "r"},
            {"service": "c", "importance": 2, "reason": "r"},
            {"service": "d", "importance": 8, "reason": "r"},
        ]
    }
    out = [(s["rank"], s["service"]) for s in ranking.ranked(found, answer, "")]
    assert out == [(1, "b"), (2, "d"), (3, "a")]  # c (2) is not touched


def test_services_kept_on_the_old_0_to_10_scale_are_put_in_order():
    old = [{"service": "x", "rank": 7}, {"service": "y", "rank": 10}]
    assert [(s["rank"], s["service"]) for s in ranking.in_order(old)] == [(1, "y"), (2, "x")]
    new = [{"service": "y", "rank": 1}, {"service": "x", "rank": 2}]
    assert ranking.in_order(new) == new


async def test_for_a_tdd_the_answers_since_the_review_are_added(memsy, llm):
    llm["services"] = [
        {"service": "imps-service", "importance": 9, "reason": "the card names IMPS"}
    ]
    await _review("BTS-60", [], ASKED)
    llm["answers"] = [{"comment_id": "9", "question_id": "Q3", "quote": "No SMS change"}]
    out = await cards.refresh(
        "BTS-60", "IMPS remark", CARD, [{"id": "9", "text": "No SMS change."}], REPOS
    )
    assert out["questions"][2]["answers"] == ["No SMS change."]
    assert [s["service"] for s in out["services"]] == ["imps-service"]


async def test_a_card_never_reviewed_is_ranked_for_its_tdd(memsy, llm):
    llm["services"] = [
        {"service": "statement-service", "importance": 7, "reason": "on the statement"}
    ]
    out = await cards.refresh("BTS-61", "Statement remark", "Show it", [], REPOS)
    assert out["services"][0]["service"] == "statement-service"
    assert (await cards.load("BTS-61"))[1]["services"][0]["service"] == "statement-service"


# --- decisions in one place, and the TDD's conversation ----------------------------------------

PAISE = {
    "uid": "BTS-25:paise",
    "id": "paise",
    "story_key": "BTS-25",
    "title": "Paise",
    "choice": "long paise",
    "status": "agreed",
    "applies_to": [{"service": "payments-service", "file_path": "Money.java", "symbol": ""}],
}


async def test_agreed_decisions_reach_a_later_card_s_review(memsy, llm):
    await designs.save("BTS-25", 1, {"decisions": [PAISE], "versions": [{"version": "v1.0"}]})
    await _earlier_work()
    memory, _ = await _review("BTS-60", [], [], title=LIKE_NEFT, text=SHORT)
    assert memory["decisions"] == [PAISE]
    assert "Paise: long paise (BTS-25)" in cards.document(memory)["text"]
    assert await designs.decisions_in_force("BTS-60", ["loan-service"]) == []


async def test_a_tdd_in_the_history_carries_no_decisions(memsy):
    await history.write(
        "BTS-60", "design", {"title": "IMPS", "version": "v1.0", "services": ["imps"]}
    )
    (event,) = memsy.stored("work:BTS")
    assert "decision" not in event.content.lower()


async def test_the_tdd_conversation_is_kept_and_recalled(memsy):
    await designs.remember_exchange("BTS-25", "9001", "why paise?", "Exact money.", "question", "")
    await designs.remember_version("BTS-25", "v1.1", "9001", [PAISE], [])
    sessions = {e.session_id for e in memsy.stored("tdd:BTS-25")}
    assert sessions == {"page:9001", "versions:BTS-25"}
    assert any(
        "paise" in line.lower() for line in await designs.recall("BTS-25", "why paise money")
    )


# --- Planning always gets a card with ranked services (2026-11-03) -----------------


def test_a_repository_with_no_branch_named_leaves_it_to_planning():
    """keystone/ui has no "main": Planning reads the repository's default branch."""
    assert ranking.parse_repos("ui: keystone/ui\nbank: o/bank@develop") == {
        "ui": ("keystone/ui", ""),
        "bank": ("o/bank", "develop"),
    }


@pytest.mark.asyncio
async def test_without_memsy_the_card_is_still_ranked(monkeypatch):
    async def no_memory(*a, **kw):
        raise store.MemoryUnavailable("Memsy is not configured")

    async def ranked(card_text, repos_text, related):
        return [{"service": "ui", "repo": "keystone/ui", "branch": "", "rank": 1}]

    monkeypatch.setattr(memory_tools.cards, "refresh", no_memory)
    monkeypatch.setattr(memory_tools.ranking, "rank", ranked)
    fn = getattr(memory_tools.refresh_card, "__wrapped__", memory_tools.refresh_card)
    out = await fn("BTS-40", "Client UI shift", "Client UI shift", [], "ui: keystone/ui")
    assert out["ok"] and out["questions"] == [] and out["memory"] is False
    assert out["services"][0]["repo"] == "keystone/ui"
