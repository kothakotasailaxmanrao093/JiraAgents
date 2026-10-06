"""Every ticket in the project, indexed by word, kept up to date — in memory.

Duplicate detection used to read only the newest 300 tickets
(LTW_MAX_CONTEXT_ISSUES), so in an 18,000-ticket project 17,700 were never
compared. Reading all of them on every build takes ~75 s (180 pages of 100,
~0.4 s each, measured on FL). So instead:

* the first build after a worker starts reads the whole project once;
* every later build asks Jira only what changed (``updated >= -Nm``) — usually
  nothing, one page at most;
* a full re-read every ``LTW_INDEX_FULL_REFRESH_HOURS`` (24) drops tickets that
  were deleted or moved, which "what changed" cannot report — and before that,
  any match is checked to still exist before it is reported, so a deleted
  ticket can never block new work in between (tools._still_there).

The index maps each word to the tickets containing it — like the index of a
book — so a proposed title is compared only with the tickets that share a word
with it. That changes no result: every rule (title ≥ 0.75, description ≥ 0.85,
requirement ≥ 0.80, near miss ≥ 0.30) scores 0 for a ticket sharing no word, so
the tickets it skips are exactly the ones that could never match.

Kept in this worker's memory (about 30–40 MB for 18,000 tickets): nothing to
install. A restarted worker simply rebuilds it on its next build.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

import httpx
from common_lib.utils.logger import setup_logger

from src.config.settings import env_bool, env_int
from src.jira.client import _quote
from src.jira.context import EXISTING_FIELDS, parse_existing
from src.jira.duplicates import _tokens, requirement_threshold
from src.models.schemas import ExistingIssue

logger = setup_logger(__name__)

DEFAULT_FULL_REFRESH_HOURS = 24


def enabled() -> bool:
    """LTW_DUPLICATE_INDEX: compare against every ticket (on by default)."""
    return env_bool("LTW_DUPLICATE_INDEX", default=True)


def full_refresh_seconds() -> int:
    return max(1, env_int("LTW_INDEX_FULL_REFRESH_HOURS", DEFAULT_FULL_REFRESH_HOURS)) * 3600


@dataclass
class ProjectIndex:
    """One project's tickets, and which words each one contains."""

    project_key: str
    issues: dict[str, ExistingIssue] = field(default_factory=dict)
    words: dict[str, frozenset[str]] = field(default_factory=dict)
    postings: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    loaded_at: float = 0.0  # the last full read
    synced_at: float = 0.0  # the last read of what changed

    def put(self, issue: ExistingIssue) -> None:
        """Add a ticket, or replace the old version of it."""
        self.drop(issue.key)
        words = frozenset(_tokens(f"{issue.summary}\n{issue.description or ''}"))
        self.issues[issue.key] = issue
        self.words[issue.key] = words
        for word in words:
            self.postings[word].add(issue.key)

    def drop(self, key: str) -> None:
        for word in self.words.pop(key, frozenset()):
            holders = self.postings.get(word)
            if holders is not None:
                holders.discard(key)
                if not holders:
                    del self.postings[word]
        self.issues.pop(key, None)

    def candidates(self, titles: list[str], requirement: str) -> list[ExistingIssue]:
        """The only tickets any duplicate rule could match, newest first.

        A ticket sharing a word with a proposed title (every title rule), or
        sharing enough words with the whole requirement to reach the
        requirement rule's threshold (containment ≥ 0.80 of its words).
        """
        keys: set[str] = set()
        for title in titles:
            for word in _tokens(title):
                keys |= self.postings.get(word, set())

        wanted = _tokens(requirement)
        if wanted:
            need = math.ceil(requirement_threshold() * len(wanted))
            shared: dict[str, int] = defaultdict(int)
            for word in wanted:
                for key in self.postings.get(word, ()):
                    shared[key] += 1
            keys |= {key for key, count in shared.items() if count >= need}

        return sorted((self.issues[k] for k in keys), key=_newest_first)


def _newest_first(issue: ExistingIssue) -> int:
    tail = issue.key.rsplit("-", 1)[-1]
    return -int(tail) if tail.isdigit() else 0


_INDEXES: dict[str, ProjectIndex] = {}
_LOCKS: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _search(client: httpx.AsyncClient, jql: str, base_url: str) -> list[ExistingIssue]:
    """Every ticket a JQL query matches, following each page."""
    out: list[ExistingIssue] = []
    token: str | None = None
    while True:
        params: dict[str, object] = {"jql": jql, "maxResults": 100, "fields": EXISTING_FIELDS}
        if token:
            params["nextPageToken"] = token
        resp = await client.get("/rest/api/3/search/jql", params=params)
        resp.raise_for_status()
        payload = resp.json()
        out.extend(parse_existing(item, base_url) for item in payload.get("issues", []))
        token = payload.get("nextPageToken")
        if payload.get("isLast") or not token:
            return out


async def up_to_date(client: httpx.AsyncClient, project_key: str, base_url: str) -> ProjectIndex:
    """This project's index, brought up to date. Raises when Jira cannot be read.

    One read at a time per project, so two builds starting together share one
    full read instead of both doing it.
    """
    project_key = project_key.upper()
    async with _LOCKS[project_key]:
        started = time.time()
        index = _INDEXES.get(project_key)
        if index is None or started - index.loaded_at >= full_refresh_seconds():
            fresh = ProjectIndex(project_key)
            for issue in await _search(
                client, f"project = {_quote(project_key)} ORDER BY created DESC", base_url
            ):
                fresh.put(issue)
            fresh.loaded_at = fresh.synced_at = started
            _INDEXES[project_key] = fresh
            logger.info(
                f"Ticket index for {project_key}: read all {len(fresh.issues)} tickets "
                f"in {time.time() - started:.1f}s"
            )
            return fresh

        # A minute of overlap: Jira's "updated" has minute precision.
        minutes = math.ceil((started - index.synced_at) / 60) + 1
        changed = await _search(
            client, f'project = {_quote(project_key)} AND updated >= "-{minutes}m"', base_url
        )
        for issue in changed:
            index.put(issue)
        index.synced_at = started
        logger.info(
            f"Ticket index for {project_key}: {len(changed)} changed ticket(s) in "
            f"{time.time() - started:.2f}s ({len(index.issues)} in total)"
        )
        return index


def forget_tickets(project_key: str, keys: set[str]) -> None:
    """Drop tickets found deleted or moved, without waiting for the full re-read."""
    index = _INDEXES.get(project_key.upper())
    for key in keys if index else ():
        index.drop(key)


def forget() -> None:
    """Drop every index (tests; a worker restart does the same)."""
    _INDEXES.clear()
