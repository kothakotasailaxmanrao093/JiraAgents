"""A card's memory, kept by Orchestration (2026-10-30): the review's questions
with the answers people gave, the services the card touches, and what the
review is told before it reviews.

    actor  review:<CARD>   snapshots memory:review:<CARD>:<seq>:<part> (encrypted)

    {"questions": [{"id": "Q1", "question": "...",
                    "answers": [{"comment_id": "10520", "text": "Yes, same as NEFT."}]}],
     "seen": {"10520": "<hash of the comment's text>"},
     "services": [{"service", "repo", "branch", "rank", "reason", "prs"}],
     "text_hash": "<hash of the card's text>", "related": [<earlier cards>]}

Answers, made safe: a comment that starts "Q2:" (or "A2:") answers Q2,
without a model. Otherwise a model names the question AND quotes the words of
the comment that answer it; a link whose quote is not in the comment is
dropped. The answer kept is always the comment's own text.

Searches, kept few: one search per review, and none when the card's text has
not changed since the last one (the earlier cards found then are reused).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any

from memory_service import designs, history, model, ranking, store

logger = logging.getLogger(__name__)

MAX_ANSWER_CHARS = 600
MAX_EARLIER = 5
SAME_QUESTION = 0.8  # word overlap at which two questions are one
MIN_QUOTE_WORDS = 2
SOURCE_ID = "card_memory"
SOURCE_LABEL = "Earlier answers and related work"
_WORD = re.compile(r"[a-z0-9]+")
_NUMBERED = re.compile(r"^\s*[\[(]?\s*[QA]\s*(\d{1,3})\s*[\])]?\s*[:.)\-–]", re.IGNORECASE)


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


def _hash(text: str) -> str:
    return hashlib.sha256(" ".join((text or "").split()).encode("utf-8")).hexdigest()[:16]


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


def changed_comments(snapshot: dict, comments: list[dict]) -> list[dict]:
    """Comments not read before, or edited since."""
    seen = snapshot.get("seen") or {}
    return [
        c
        for c in comments
        if str(c.get("id") or "") and seen.get(str(c["id"])) != _hash(str(c.get("text") or ""))
    ]


def numbered_matches(questions: list[dict], comments: list[dict]) -> list[dict]:
    """A comment that starts "Q2:" or "A2:" answers Q2 — no model needed."""
    ids = {q["id"] for q in questions}
    out = []
    for c in comments:
        found = _NUMBERED.match(str(c.get("text") or ""))
        if found and f"Q{int(found.group(1))}" in ids:
            out.append({"comment_id": str(c["id"]), "question_id": f"Q{int(found.group(1))}"})
    return out


def quoted(match: dict, comments: list[dict]) -> bool:
    """The model's quote is really in the comment it names."""
    quote = " ".join(_WORD.findall(str(match.get("quote") or "").lower()))
    if len(quote.split()) < MIN_QUOTE_WORDS:
        return False
    for c in comments:
        if str(c.get("id")) == str(match.get("comment_id")):
            return quote in " ".join(_WORD.findall(str(c.get("text") or "").lower()))
    return False


MATCH_SYSTEM_PROMPT = """You link the comments on a Jira card to the review questions they answer.

You get the questions (each with an id) and some comments (each with an id).
For each comment, name every question it answers, and quote the exact words
of the comment that give the answer. A comment answers a question only when it
gives the information the question asks for; a comment that only thanks,
repeats the question, asks something else or says it will answer later answers
nothing. A comment may answer several questions; most answer none. Use only
the ids given, and quote the comment word for word.

Return ONLY a JSON object:
{"answers": [{"comment_id": "...", "question_id": "...", "quote": "..."}]}"""


async def match_answers(questions: list[dict], comments: list[dict]) -> list[dict]:
    """Which question each comment answers: numbered answers first, then a
    model's links that quote the comment. [] when nothing to match."""
    if not questions or not comments:
        return []
    numbered = numbered_matches(questions, comments)
    direct = {m["comment_id"] for m in numbered}
    rest = [c for c in comments if str(c["id"]) not in direct]
    if not rest:
        return numbered
    asked = "\n".join(f"[{q['id']}] {q['question']}" for q in questions)
    said = "\n".join(
        f"[{c['id']}] {' '.join(str(c.get('text') or '').split())[:1500]}" for c in rest
    )
    found = await model.ask_json(MATCH_SYSTEM_PROMPT, f"QUESTIONS:\n{asked}\n\nCOMMENTS:\n{said}")
    linked = [m for m in found.get("answers") or [] if isinstance(m, dict) and quoted(m, rest)]
    return numbered + linked


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
            continue  # an id that is not real is dropped
        answers = by_id[question_id]["answers"]
        if all(a["comment_id"] != comment_id for a in answers):
            # The answer without its "Q2:" label.
            text = _NUMBERED.sub("", now[comment_id], count=1).strip() or now[comment_id]
            answers.append({"comment_id": comment_id, "text": text.strip()[:MAX_ANSWER_CHARS]})
    seen = {cid: _hash(text) for cid, text in now.items()}
    return {**snapshot, "questions": questions, "seen": seen}


async def with_comments(card_key: str, snapshot: dict, comments: list[dict]) -> dict:
    """The snapshot with new or edited comments linked to the questions they
    answer. When the model cannot be reached, only numbered answers are kept and
    the rest are not marked read, so the next review matches them again."""
    fresh = changed_comments(snapshot, comments)
    questions = snapshot.get("questions") or []
    try:
        matches = await match_answers(questions, fresh) if fresh else []
    except Exception as error:  # noqa: BLE001 - matched next time
        logger.warning("Answers on %s not matched: %s", card_key, error)
        matches = numbered_matches(questions, fresh)
        direct = {m["comment_id"] for m in matches}
        comments = [c for c in comments if c not in fresh or str(c["id"]) in direct]
    return with_answers(snapshot, comments, matches)


def view(snapshot: dict) -> list[dict]:
    """The questions as the TDD gets them: each with its id and answers' text."""
    return [
        {
            "id": q["id"],
            "question": q["question"],
            "answers": [a["text"] for a in q.get("answers") or []],
        }
        for q in snapshot.get("questions") or []
    ]


# --- before and after a review --------------------------------------------------------------


async def _earlier(card_key: str, title: str, card_text: str, snapshot: dict) -> list[dict]:
    """Earlier cards about the same thing: one search, or none when the card's
    text is unchanged since the last one."""
    if snapshot.get("text_hash") == _hash(card_text) and "related" in snapshot:
        return list(snapshot.get("related") or [])
    try:
        return await asyncio.to_thread(history.find, card_key, f"{title}\n{card_text}")
    except Exception as error:  # noqa: BLE001 - ranked from the card alone
        logger.warning("No history for %s: %s", card_key, error)
        return []


async def recall(
    card_key: str, title: str, card_text: str, comments: list[dict], repos_text: str
) -> dict[str, Any]:
    """The card's memory before its review: {"seq", "snapshot", "changed",
    "services", "related", "decisions"}. Nothing is saved yet; ``keep`` saves
    once, after the review."""
    seq, stored = await asyncio.to_thread(load, card_key)
    snapshot = await with_comments(card_key, stored, comments)
    related = await _earlier(card_key, title, card_text, snapshot)
    services = await ranking.rank(card_text, repos_text, related)
    snapshot = {**snapshot, "text_hash": _hash(card_text), "related": related}
    try:
        decisions = await asyncio.to_thread(
            designs.decisions_in_force, card_key, [s["service"] for s in services]
        )
    except Exception as error:  # noqa: BLE001 - the review goes on without them
        logger.warning("No decisions for %s: %s", card_key, error)
        decisions = []
    return {
        "seq": seq,
        "snapshot": snapshot,
        "changed": snapshot != stored,
        "services": services,
        "related": related,
        "decisions": decisions,
    }


def document(memory: dict[str, Any]) -> dict[str, Any] | None:
    """The memory as one more source for the review; None when it holds nothing."""
    snapshot = memory.get("snapshot") or {}
    answered = [q for q in snapshot.get("questions") or [] if q.get("answers")]
    still_open = [q for q in snapshot.get("questions") or [] if not q.get("answers")]
    earlier = list(memory.get("related") or [])[:MAX_EARLIER]
    services = list(memory.get("services") or [])
    decisions = list(memory.get("decisions") or [])
    if not (answered or still_open or earlier or services or decisions):
        return None
    lines = [
        "What is already known about this card and earlier work in the project. Treat an "
        "answer below as part of the requirement and never ask that question again. Use the "
        "earlier cards and the agreed decisions to ask sharper questions (for example whether "
        "a rule an earlier card set also applies here); do not ask what they already settled "
        "unless this card says something different."
    ]
    if answered:
        lines += ["", "Questions already answered in the comments:"]
        for q in answered:
            said = " / ".join(a["text"] for a in q["answers"])
            lines.append(f"- {q['id']} {q['question']} Answer: {said}")
    if still_open:
        lines += ["", "Questions asked before and not answered yet:"]
        lines += [f"- {q['id']} {q['question']}" for q in still_open]
    if earlier:
        lines += ["", "Earlier cards about the same thing:"]
        for card in earlier:
            line = f"- {card['card']} {card.get('title') or ''}".rstrip()
            if card.get("services"):
                line += f": changed {', '.join(card['services'])}"
            prs = [
                f"PR #{p['pr']} on {p.get('branch') or 'its branch'} ({p.get('state', 'open')})"
                for p in card.get("prs") or []
            ]
            if prs:
                line += f"; {'; '.join(prs)}"
            lines.append(line)
    if decisions:
        lines += ["", "Design decisions already agreed on these services:"]
        for d in decisions:
            lines.append(f"- {d.get('title') or d.get('id')}: {d.get('choice')} ({d['story_key']})")
    if services:
        lines += ["", "Services this card touches, ranked 0-10:"]
        lines += [f"- {s['service']} {s['rank']}/10: {s['reason']}" for s in services]
    return {
        "source_id": SOURCE_ID,
        "source_label": SOURCE_LABEL,
        "kind": "card_memory",
        "text": "\n".join(lines),
        "metadata": {"answered": len(answered), "earlier_cards": len(earlier)},
    }


def keep(
    card_key: str, memory: dict[str, Any], asked: list[str], title: str, summary: str
) -> dict[str, Any]:
    """After the review: its questions and the ranked services saved, the card
    in the project's history. Returns {"services", "questions"}."""
    seq, before = memory.get("seq") or 0, memory.get("snapshot") or {}
    services = list(memory.get("services") or [])
    updated = {**with_questions(before, asked), "services": services}
    if memory.get("changed") or updated != before:
        save(card_key, seq + 1, updated)
    if services:
        history.write(
            card_key,
            "card",
            {
                "title": title,
                "services": [s["service"] for s in services],
                "summary": (summary or "")[:500],
            },
        )
    return {"services": services, "questions": view(updated)}


async def refresh(
    card_key: str, title: str, card_text: str, comments: list[dict], repos_text: str
) -> dict[str, Any]:
    """For a TDD: the questions with every answer given so far, and the ranked
    services (ranked now when the card was never reviewed with its memory).
    Returns {"services", "questions"}."""
    seq, stored = await asyncio.to_thread(load, card_key)
    updated = await with_comments(card_key, stored, comments)
    services = list(updated.get("services") or [])
    if not services:
        related = await _earlier(card_key, title, card_text, updated)
        services = await ranking.rank(card_text, repos_text, related)
        updated = {
            **updated,
            "services": services,
            "text_hash": _hash(card_text),
            "related": related,
        }
    if updated != stored:
        await asyncio.to_thread(save, card_key, seq + 1, updated)
    return {"services": services, "questions": view(updated)}
