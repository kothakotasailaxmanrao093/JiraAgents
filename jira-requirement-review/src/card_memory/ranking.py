"""Which services a card touches, ranked 0-10 with a reason (2026-10-30).

Done during the review, from what the review can see: the card (its text and
its files' text), the services configured for the project, and earlier cards
about the same thing from the history, with their pull requests and branches.
The code itself is read later, by Planning, for the services ranked here.

    9-10  the centre of the change      3-5  might change; check it
    6-8   must change, not the centre   0-2  not touched; not returned

A reason may name only what was found: a file on the card, an earlier card or
PR. A reason naming anything else is replaced by the evidence itself, so a
rank is never explained by a guess.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from card_memory import model

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 30
MAX_RANKED = 10
MIN_RANK = 3
MAX_TEXT = 12_000
NAMED_RANK, EARLIER_RANK = 8, 6  # when the model cannot be reached
_NAMED_THING = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b|#\d+|\b[\w-]+\.[a-z0-9]{1,5}\b")
_SPEC_LINE = re.compile(r"^([^:=#]+?)\s*[:=]\s*([^@\s]+)(?:@(\S+))?$")


def parse_repos(repos_text: str) -> dict[str, tuple[str, str]]:
    """{service: (repo, branch)} from "name: org/repo@branch" lines (Planning's
    format); no branch is "main"."""
    out = {}
    for line in (repos_text or "").splitlines():
        found = _SPEC_LINE.match(line.strip())
        if found:
            name, repo, branch = found.groups()
            out[name.strip()] = (repo, branch or "main")
    return out


def named_in(card_text: str, service: str) -> bool:
    """The card names the service: "payments-service", or "payments service"."""
    text = " ".join(re.split(r"[\s_-]+", card_text.lower()))
    return " ".join(re.split(r"[\s_-]+", service.lower())) in text


def candidates(
    card_text: str, services: list[str], related: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every configured service, each with its evidence: the card names it, an
    earlier card on the same thing touched it (with its PRs and branches).
    Those with evidence come first."""
    out = []
    for service in services:
        evidence, prs = [], []
        if named_in(card_text, service):
            evidence.append("the card names this service")
        for card in related:
            if service not in (card.get("services") or []):
                continue
            mine = [p for p in card.get("prs") or [] if p.get("service") in (None, "", service)]
            done = ", ".join(
                f"PR #{p['pr']} on {p.get('branch') or 'its branch'} ({p.get('state', 'open')})"
                for p in mine
            )
            evidence.append(
                f"earlier card {card['card']} ({card.get('title') or 'no title'}) touched it"
                + (f": {done}" if done else "")
            )
            prs += [
                {
                    "card": card["card"],
                    "pr": p["pr"],
                    "branch": p.get("branch", ""),
                    "url": p.get("url", ""),
                    "state": p.get("state", "open"),
                }
                for p in mine
            ]
        out.append({"service": service, "evidence": evidence, "prs": prs})
    out.sort(key=lambda c: (not c["evidence"], c["service"]))
    return out[:MAX_CANDIDATES]


RANK_SYSTEM_PROMPT = """You rank the services a Jira card will change, from 0 to 10.

9-10: the centre of the change. 6-8: must change, but not the centre.
3-5: might change; worth checking. 0-2: not touched.

You get the card (its text and the text of its attached files) and the
project's services, each with the EVIDENCE found for it: the card names it,
earlier cards about the same thing changed it (with their pull requests).
Rank each service by what the CARD asks for, using its name and the evidence.
The reason is ONE short sentence that names only things in the card or in that
service's evidence (a file name on the card, an earlier card key or PR
number). Never name anything that is not there.

Return ONLY a JSON object:
{"services": [{"service": "<name as given>", "rank": <0-10>, "reason": "..."}]}"""


def _prompt(card_text: str, found: list[dict[str, Any]]) -> str:
    lines = [f"CARD:\n{card_text[:MAX_TEXT]}", "", "SERVICES:"]
    for c in found:
        lines.append(f"- {c['service']}: " + ("; ".join(c["evidence"]) or "no evidence found"))
    return "\n".join(lines)


def honest_reason(reason: str, card_text: str, evidence: list[str]) -> bool:
    """Every card key, PR number and file name in ``reason`` is in the card or
    the evidence."""
    known = (card_text + "\n" + "\n".join(evidence)).lower()
    return all(thing.lower() in known for thing in _NAMED_THING.findall(reason or ""))


def _fallback(c: dict[str, Any]) -> int:
    if "the card names this service" in c["evidence"]:
        return NAMED_RANK
    return EARLIER_RANK if c["evidence"] else 0


def ranked(
    found: list[dict[str, Any]], answer: dict[str, Any] | None, card_text: str
) -> list[dict[str, Any]]:
    """The model's ranks, checked. Without its answer, a service the card
    names is 8, one an earlier card touched 6, any other is left out."""
    given = {}
    for item in (answer or {}).get("services") or []:
        if isinstance(item, dict) and item.get("service"):
            given[str(item["service"])] = item
    out = []
    for c in found:
        item = given.get(c["service"])
        try:
            rank = int(round(float(item["rank"]))) if item else _fallback(c)
        except (TypeError, ValueError, KeyError):
            rank = _fallback(c)
        rank = max(0, min(10, rank))
        reason = " ".join(str((item or {}).get("reason") or "").split())
        if not reason or not honest_reason(reason, card_text, c["evidence"]):
            reason = "; ".join(c["evidence"]) or "what the card asks for"
        out.append({"service": c["service"], "rank": rank, "reason": reason, "prs": c["prs"]})
    out.sort(key=lambda s: (-s["rank"], s["service"]))
    return [s for s in out if s["rank"] >= MIN_RANK][:MAX_RANKED]


async def rank(
    card_text: str, repos_text: str, related: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The services the card touches, best first:
    [{"service", "repo", "branch", "rank", "reason", "prs"}]."""
    repos = parse_repos(repos_text)
    found = candidates(card_text, list(repos), related)
    if not found or not card_text.strip():
        return []
    try:
        answer = await model.ask_json(RANK_SYSTEM_PROMPT, _prompt(card_text, found))
    except Exception as error:  # noqa: BLE001 - ranked from the evidence instead
        logger.warning("Service ranking fell back to the evidence: %s", error)
        answer = None
    return [
        {"service": s["service"], "repo": repos[s["service"]][0], "branch": repos[s["service"]][1]}
        | s
        for s in ranked(found, answer, card_text)
    ]
