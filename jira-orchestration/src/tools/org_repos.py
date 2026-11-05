"""Which repositories Planning and Coding work on: the whole organization (2026-10-16).

Every repository of ORCH_GITHUB_ORG, read from GitHub when a command runs — a
repository created today is designed and coded against today, with no setting
to change. ORCH_PLANNING_REPOS still names services ("bank: org/bank-transfer-service")
and may add repositories outside the organization.

Read in activities only: a workflow may not reach the network. A failed read
falls back to ORCH_PLANNING_REPOS, so GitHub being down never stops a design.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from common_lib.utils.logger import setup_logger

from routing import github_events as gh
from routing import settings

logger = setup_logger(__name__)

# One read serves the commands of the next few minutes; a new repository still
# appears within this much time.
_CACHE_SECONDS = 300
_cache: dict[str, Any] = {"at": 0.0, "org": "", "repos": []}


async def _org_repos() -> list[dict]:
    org = settings.github_org()
    if _cache["org"] == org and time.monotonic() - _cache["at"] < _CACHE_SECONDS:
        return _cache["repos"]
    repos: list[dict] = []
    async with httpx.AsyncClient(
        base_url=settings.github_api_url(),
        timeout=30.0,
        headers={
            "Authorization": f"Bearer {settings.github_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    ) as client:
        for page in range(1, 11):
            resp = await client.get(
                f"/orgs/{org}/repos", params={"type": "all", "per_page": 100, "page": page}
            )
            resp.raise_for_status()
            batch = resp.json()
            repos.extend(batch)
            if len(batch) < 100:
                break
    wanted = settings.github_repos()
    if wanted:  # ORCH_GITHUB_REPOS still narrows the organization when set
        repos = [r for r in repos if str(r.get("name") or "").lower() in wanted]
    repos = active(repos, settings.org_repo_max_idle_days())
    # What each repository is, for the ranking of a card's services (2026-11-03).
    from memory_service import ranking

    ranking.ABOUT.update({
        str(r.get("name") or "").lower(): "; ".join(
            str(v) for v in (r.get("language"), r.get("description")) if v
        )
        for r in repos
        if r.get("language") or r.get("description")
    })
    _cache.update(at=time.monotonic(), org=org, repos=repos)
    return repos


def active(repos: list[dict], max_idle_days: int, now: datetime | None = None) -> list[dict]:
    """The repositories still worked on: not archived, pushed to within
    ``max_idle_days`` (0 keeps all). One with no push date is kept."""
    if not max_idle_days:
        return repos
    cutoff = (now or datetime.now(UTC)) - timedelta(days=max_idle_days)
    kept = []
    for repo in repos:
        if repo.get("archived"):
            continue
        pushed = str(repo.get("pushed_at") or "")
        try:
            if pushed and datetime.fromisoformat(pushed.replace("Z", "+00:00")) < cutoff:
                continue
        except ValueError:
            pass
        kept.append(repo)
    return kept


async def planning_repos() -> str:
    """The services text Planning and Coding are given: "name: org/repo[@branch]" lines."""
    configured = settings.planning_repos()
    if not settings.planning_uses_org():
        return configured
    try:
        return gh.with_org_repos(configured, await _org_repos())
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            f"the organization's repositories could not be read ({exc}); "
            "using ORCH_PLANNING_REPOS alone"
        )
        return configured


async def with_repos(values: dict[str, Any]) -> dict[str, Any]:
    """``values`` (settings.pipeline() / settings.commands()) with the full repository list."""
    return {**values, "repos": await planning_repos()}
