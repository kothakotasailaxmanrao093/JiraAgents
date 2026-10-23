"""@tool: read-only look through the organisation's code, for a bug investigation.

A bug report says what is wrong; the code says why. This tool finds the files
most likely to be involved and returns their text as labelled documents — the
same shape as every other context source — so the model can cite
"Code AetherionAgentDemo/bank-transfer-service/src/main/java/…".

How files are found: each repository's file list is read (GitHub's tree API),
code files are scored by the bug report's own words in their path, the best
candidates are opened and scored again by those words in their content, and the
top few are kept. GitHub's code *search* is not used: it does not index forks
(``bank-transfer-service`` is one) and lags behind new pushes, so it found
nothing on exactly the repositories that matter (2026-10-22).

Read-only by construction: only GET requests. It never writes, branches or comments.

Never fails a run. No token, no organisation, a rate limit or an unreachable
GitHub all come back as a note the reply states, and the investigation
continues from the ticket alone.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp
from aetherion_sdk import tool
from dotenv import load_dotenv

from review.code_terms import keywords, search_terms
from shared.attachments import fit_to_budget

load_dotenv()
logger = logging.getLogger(__name__)

API = "https://api.github.com"
MAX_REPOS = 20
MAX_TREE_FILES = 2_000  # per repository, after filtering
MAX_OPENED = 30  # candidate files opened to score their content
MAX_FILES = 6  # files given to the model
MAX_FILE_BYTES = 200_000
MAX_FILE_CHARS = 12_000
MAX_TOTAL_CHARS = 60_000

CODE_EXT = (
    ".java .kt .kts .scala .groovy .py .js .jsx .ts .tsx .go .cs .rb .php .rs .swift "
    ".sql .yml .yaml .properties .xml .gradle .sh .html .vue"
).split()
_SKIP_PARTS = ("node_modules/", "/dist/", "/build/", "/vendor/", ".min.", "/target/", "/.git/")
_SKIP_NAMES = ("package-lock.json", "yarn.lock", "uv.lock", "poetry.lock", "pnpm-lock.yaml")


def is_code(path: str) -> bool:
    lowered = path.lower()
    if any(part in f"/{lowered}" for part in _SKIP_PARTS):
        return False
    if lowered.rsplit("/", 1)[-1] in _SKIP_NAMES:
        return False
    return any(lowered.endswith(ext) for ext in CODE_EXT)


def path_score(path: str, words: list[str]) -> int:
    """How strongly a path names the report's subject. Earlier words weigh more."""
    lowered = path.lower()
    total = len(words)
    return sum((total - i) * 3 for i, w in enumerate(words) if w in lowered)


def content_score(text: str, words: list[str]) -> int:
    """How much a file talks about the report's subject. Capped per word, so one
    word repeated a hundred times does not outrank a file that mentions them all."""
    lowered = text.lower()
    total = len(words)
    return sum(min(lowered.count(w), 5) * (total - i) for i, w in enumerate(words))


@tool(name="search_code")
async def search_code(
    issue_key: str,
    summary: str = "",
    text: str = "",
) -> dict[str, Any]:
    """Find and read the code files a bug report points at.

    Returns ``{"documents": [...], "terms": [...], "searched": str, "note": str}``.
    ``note`` is empty when code was found and read; otherwise it says why not,
    in a sentence the reply can show as it is.
    """
    token = os.getenv("GITHUB_TOKEN", "").strip()
    org = (os.getenv("GITHUB_ORG") or os.getenv("ORCH_GITHUB_ORG") or "").strip()
    words = keywords(summary, text)
    terms = search_terms(summary, text)
    searched = ", ".join(words[:8])
    if not token or not org:
        return {
            "documents": [],
            "terms": terms,
            "searched": searched,
            "note": "The code could not be searched: GitHub access is not configured for "
            "this agent (GITHUB_TOKEN and GITHUB_ORG).",
        }
    if not words:
        return {
            "documents": [],
            "terms": terms,
            "searched": "",
            "note": "The code was not searched: the report names nothing to look for.",
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    problems: list[str] = []
    timeout = aiohttp.ClientTimeout(total=75)
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            # 1. Every repository's code files, with their size.
            async with session.get(
                f"{API}/orgs/{org}/repos", params={"per_page": "100", "type": "all"}
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"listing {org}'s repositories returned HTTP {resp.status}")
                repos = [
                    r
                    for r in await resp.json()
                    if not r.get("archived") and (r.get("size") or 0) > 0
                ][:MAX_REPOS]

            candidates: list[tuple[int, str, str, int]] = []  # score, repo, path, size
            for repo in repos:
                name = repo["full_name"]
                url = f"{API}/repos/{name}/git/trees/{repo.get('default_branch') or 'HEAD'}"
                async with session.get(url, params={"recursive": "1"}) as resp:
                    if resp.status != 200:
                        problems.append(f"{name} could not be listed (HTTP {resp.status})")
                        continue
                    tree = (await resp.json()).get("tree") or []
                files = [
                    t
                    for t in tree
                    if t.get("type") == "blob"
                    and is_code(t.get("path") or "")
                    and (t.get("size") or 0) <= MAX_FILE_BYTES
                ][:MAX_TREE_FILES]
                for t in files:
                    candidates.append(
                        (path_score(t["path"], words), name, t["path"], t.get("size") or 0)
                    )

            # 2. Open the likeliest files: named for the subject first, then the
            #    rest, so a small repository is read in full.
            candidates.sort(key=lambda c: (-c[0], c[3]))
            scored: list[tuple[int, str, str, str]] = []
            for p_score, name, path, _size in candidates[:MAX_OPENED]:
                url = f"{API}/repos/{name}/contents/{path}"
                async with session.get(
                    url, headers={"Accept": "application/vnd.github.raw"}
                ) as resp:
                    if resp.status != 200:
                        problems.append(f"{name}/{path} could not be read (HTTP {resp.status})")
                        continue
                    content = await resp.text(errors="replace")
                c_score = content_score(content, words)
                if p_score + c_score > 0:
                    scored.append((p_score + c_score, name, path, content))
    except Exception as exc:  # noqa: BLE001 — code search must never fail the run
        logger.warning("search_code failed for %s: %s", issue_key, exc)
        return {
            "documents": [],
            "terms": terms,
            "searched": searched,
            "note": f"The code could not be searched: {exc}",
        }

    # 3. The best few, within the size budget.
    scored.sort(key=lambda s: -s[0])
    documents: list[dict[str, Any]] = []
    used = 0
    for score, name, path, content in scored[:MAX_FILES]:
        budget = min(MAX_FILE_CHARS, MAX_TOTAL_CHARS - used)
        if budget <= 500:
            break
        content, cut = fit_to_budget(content, budget)
        used += len(content)
        documents.append(
            {
                "source_id": f"code:{name}:{path}",
                "source_label": f"Code {name}/{path}",
                "kind": "source_code",
                "text": content,
                "metadata": {"repo": name, "path": path, "score": score, "note": cut},
            }
        )

    if documents:
        note = ""
    else:
        why = f" ({'; '.join(problems[:3])})" if problems else ""
        note = (
            f"The code for this bug was not found in the repositories this agent can read "
            f"({org}); searched for: {searched}{why}. The cause above comes from the report."
        )
    logger.info(
        "search_code %s: words=%s, %d candidate(s), %d file(s) kept, problems=%s",
        issue_key,
        words,
        len(scored),
        len(documents),
        problems[:3],
    )
    return {"documents": documents, "terms": terms, "searched": searched, "note": note}
