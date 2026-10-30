"""A card's review questions and the answers people gave them (2026-10-30).

The review asks questions; people answer in Jira comments, today or days
later. This keeps both, so the next review asks nothing twice and the TDD is
given each question with its answers:

    actor  review:<CARD>   session memory:review:<CARD>:<seq>:<part>

Kept as an encrypted snapshot (store.py):

    {"questions": [{"id": "Q1", "question": "...",
                    "answers": [{"comment_id": "10520", "text": "Yes, same as NEFT."}]}],
     "seen": {"10520": "<hash of the comment's text>"},
     "services": [{"service", "repo", "branch", "rank", "reason", "prs"}]}

A model only says WHICH question a comment answers; the answer kept is the
comment's own text, so nothing is put in a person's mouth. An edited comment
is matched again; a deleted one stops being an answer.
"""

from __future__ import annotations

import hashlib
import logging
import re

from card_memory import model, store

logger = logging.getLogger(__name__)

MAX_ANSWER_CHARS = 600
_WORD = re.compile(r"[a-z0-9]+")
SAME_QUESTION = 0.8  # word overlap at which two questions are one


def _scope(card_key: str) -> tuple[str, str, str]:
    key = card_key.strip().upper()
    return f"review:{key}", f"memory:review:{key}", "review_memory"


def load(card_key: str) -> tuple[int, dict]:
    actor, _, kind = _scope(card_key)
    return store.load(actor, kind)


def save(card_key: str, seq: int, snapshot: dict) -> None:
    actor, session, kind = _scope(card_key)
    asked = len(snapshot.get("questions") or [])
    answered = sum(1 for q in snapshot.get("questions") or [] if q.get("answers"))
    summary = f"{card_key.upper()} review: {asked} questions, {answered} answered."
    store.save(actor, session, kind, seq, snapshot, summary)


def _words(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def same_question(a: str, b: str) -> bool:
    """Two wordings of one question: most of their words are shared."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= SAME_QUESTION


def with_questions(snapshot: dict, asked: list[str]) -> dict:
    """The snapshot with the review's questions added; a question already kept
    (in other words) is not added twice."""
    kept = [dict(q) for q in snapshot.get("questions") or []]
    for text in asked:
        text = " ".join(str(text or "").split())
        if not text or any(same_question(text, q["question"]) for q in kept):
            continue
        kept.append({"id": f"Q{len(kept) + 1}", "question": text, "answers": []})
    return {**snapshot, "questions": kept}


def _hash(text: str) -> str:
    return hashlib.sha256(" ".join((text or "").split()).encode("utf-8")).hexdigest()[:16]


def changed_comments(snapshot: dict, comments: list[dict]) -> list[dict]:
    """Comments not matched before, or edited since."""
    seen = snapshot.get("seen") or {}
    return [
        c
        for c in comments
        if str(c.get("id") or "") and seen.get(str(c["id"])) != _hash(str(c.get("text") or ""))
    ]


def with_answers(snapshot: dict, comments: list[dict], matches: list[dict]) -> dict:
    """The snapshot with each matched comment kept as an answer to its question.

    ``comments`` are every comment on the card now; ``matches`` say which
    question each re-read comment answers. A comment that is gone, or was
    re-read, loses its old links first."""
    now = {str(c["id"]): str(c.get("text") or "") for c in comments if str(c.get("id") or "")}
    reread = {str(m.get("comment_id")) for m in matches} | {
        str(c["id"]) for c in changed_comments(snapshot, comments)
    }
    questions = []
    for q in snapshot.get("questions") or []:
        answers = [
            a
            for a in q.get("answers") or []
            if a.get("comment_id") in now and a.get("comment_id") not in reread
        ]
        questions.append({**q, "answers": answers})
    by_id = {q["id"]: q for q in questions}
    for m in matches:
        comment_id, question_id = str(m.get("comment_id") or ""), str(m.get("question_id") or "")
        if comment_id not in now or question_id not in by_id:
            continue  # a model's id that is not real is dropped
        answers = by_id[question_id]["answers"]
        if all(a["comment_id"] != comment_id for a in answers):
            answers.append(
                {"comment_id": comment_id, "text": now[comment_id].strip()[:MAX_ANSWER_CHARS]}
            )
    seen = {cid: _hash(text) for cid, text in now.items()}
    return {**snapshot, "questions": questions, "seen": seen}


MATCH_SYSTEM_PROMPT = """You link the comments on a Jira card to the review questions they answer.

You get the questions (each with an id) and some comments (each with an id).
For each comment, name every question it answers. A comment answers a question
only when it gives the information the question asks for; a comment that only
thanks, repeats the question, asks something else or says it will answer later
answers nothing. A comment may answer several questions; most answer none.
Use only the ids given.

Return ONLY a JSON object: {"answers": [{"comment_id": "...", "question_id": "..."}]}"""


async def match_answers(questions: list[dict], comments: list[dict]) -> list[dict]:
    """Which question each comment answers, by a model; [] when nothing to match."""
    if not questions or not comments:
        return []
    asked = "\n".join(f"[{q['id']}] {q['question']}" for q in questions)
    said = "\n".join(
        f"[{c['id']}] {' '.join(str(c.get('text') or '').split())[:1500]}" for c in comments
    )
    found = await model.ask_json(MATCH_SYSTEM_PROMPT, f"QUESTIONS:\n{asked}\n\nCOMMENTS:\n{said}")
    return [m for m in found.get("answers") or [] if isinstance(m, dict)]


async def with_comments(card_key: str, snapshot: dict, comments: list[dict]) -> dict:
    """The snapshot with new or edited comments linked to the questions they
    answer. When the model cannot be reached they are not marked seen, so the
    next review matches them again."""
    fresh = changed_comments(snapshot, comments)
    try:
        matches = await match_answers(snapshot.get("questions") or [], fresh) if fresh else []
    except Exception as error:  # noqa: BLE001 - kept unanswered, matched next time
        logger.warning("Answers on %s not matched: %s", card_key, error)
        matches, comments = [], [c for c in comments if c not in fresh]
    return with_answers(snapshot, comments, matches)


def view(snapshot: dict) -> list[dict]:
    """The questions as the TDD gets them: each with its answers' text."""
    return [
        {"question": q["question"], "answers": [a["text"] for a in q.get("answers") or []]}
        for q in snapshot.get("questions") or []
    ]
