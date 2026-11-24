"""Is everything the flow needs running and reachable?"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from typing import Any

import httpx

from . import config  # config first: it puts scripts/ on the path

import local_run  # noqa: E402,I001


def workers() -> list[dict[str, Any]]:
    """Each local agent worker (scripts/local_worker.py) and whether it runs."""
    try:
        ps = subprocess.run(
            ["ps", "-axo", "command"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        ps = ""
    out = []
    for folder, name in config.AGENTS.items():
        log = config.LOGS / f"{folder}.log"
        out.append(
            {
                "agent": folder,
                "name": name,
                "running": f"local_worker.py {folder}" in ps,
                "queue": local_run.local_queue(folder),
                "log_updated": log.stat().st_mtime if log.is_file() else None,
            }
        )
    return out


def containers() -> list[dict[str, Any]]:
    """Planning's Neo4j and the local object storage, in Docker."""
    if not shutil.which("docker"):
        return []
    try:
        ps = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = dict(line.split("\t", 1) for line in ps.splitlines() if "\t" in line)
    return [
        {"name": n, "running": n in found, "status": found.get(n, "not running")}
        for n in config.CONTAINERS
    ]


async def _probe(client: httpx.AsyncClient, url: str, **kw: Any) -> tuple[bool, str]:
    try:
        r = await client.get(url, timeout=6, **kw)
        return r.status_code < 400, f"HTTP {r.status_code}"
    except httpx.HTTPError as error:
        return False, type(error).__name__


async def report(busy: bool) -> dict[str, Any]:
    base, auth = config.jira()
    github_site = config.GITHUB_PROBE.split("/api/")[0]
    async with httpx.AsyncClient() as client:
        jira, github = await asyncio.gather(
            _probe(client, f"{base}/rest/api/3/myself", auth=auth),
            _probe(client, config.GITHUB_PROBE),
        )
    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    return {
        "workers": workers(),
        "containers": containers(),
        "services": [
            {"name": "Jira", "ok": jira[0], "detail": f"{base} · {jira[1]}", "url": base},
            {
                "name": "GitHub Enterprise",
                "ok": github[0],
                "detail": "reachable" if github[0] else f"not reachable ({github[1]}) — connect the VPN",
                "url": github_site,
            },
            {
                "name": "Temporal",
                "ok": True,
                "detail": "wfe.sbox.aetherion.io · namespace test",
                "url": config.TEMPORAL_UI,
            },
            {
                "name": "OpenAI",
                "ok": has_key,
                "detail": "key set in .env.local" if has_key else "no OPENAI_API_KEY in .env.local",
                "url": "",
            },
        ],
        "busy": busy,
        "links": {
            "temporal": config.TEMPORAL_UI,
            "jira": base,
            "github": f"{github_site}/{os.environ.get('ORCH_GITHUB_ORG', '')}",
        },
        "project": config.project(),
    }


_COLOURS = re.compile(r"\x1b\[[0-9;]*m")


def agent_log(agent: str, lines: int = 200) -> list[str]:
    """The last lines of one agent's log, without terminal colours or polling noise."""
    path = config.LOGS / f"{agent}.log"
    if not path.is_file():
        return []
    with path.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 200_000))
        text = _COLOURS.sub("", f.read().decode("utf-8", "replace"))
    keep = [
        ln
        for ln in text.splitlines()
        if ln.strip() and "HTTP Request: GET" not in ln and "temporalio" not in ln
    ]
    return keep[-max(1, min(lines, 1000)) :]
