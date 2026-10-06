"""Asking a model when an agent runs on a laptop instead of Aetherion (2026-10-03).

On Aetherion the platform gives every agent the AI Gateway — its address and
access — and each agent calls it exactly as before; nothing here changes that.
A laptop has no gateway (the client falls back to localhost:8080 and fails), so
a worker started by the local launcher — which sets ``AETHERION_LOCAL_RUN=1`` —
with ``OPENAI_API_KEY`` in its environment sends the same request straight to
OpenAI.

Both come only from ``scripts/local_worker.py`` / ``scripts/run_local.sh`` (the
key from the git-ignored ``.env.local``), never from an agent's own ``.env``,
which is packed into the published agent. So on Aetherion this branch can
never switch on, whatever the platform puts in the environment.
"""

from __future__ import annotations

import os

import httpx

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
# A model named for another provider (e.g. an Anthropic one through the
# gateway) is answered by this OpenAI model locally.
DEFAULT_LOCAL_MODEL = "gpt-4o"
# Reasoning models spend completion tokens thinking before they answer; a cap
# sized for the answer alone can leave nothing for it, so they get at least this.
REASONING_MIN_TOKENS = 8000
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


LOCAL_RUN_FLAG = "AETHERION_LOCAL_RUN"


def direct_openai() -> bool:
    """True only in a laptop run started by the local launcher, with a key."""
    return os.environ.get(LOCAL_RUN_FLAG) == "1" and bool(
        os.environ.get("OPENAI_API_KEY", "").strip()
    )


def openai_request(
    *,
    provider: str,
    model: str,
    prompt: str,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """The chat-completions body for the same request the gateway would get."""
    local_default = os.environ.get("LOCAL_OPENAI_MODEL", DEFAULT_LOCAL_MODEL)
    name = model if provider == "openai" else local_default
    reasoning = name.startswith(_REASONING_PREFIXES)
    messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
    body: dict = {"model": name, "messages": [*messages, {"role": "user", "content": prompt}]}
    # Reasoning models accept only their default temperature.
    if temperature is not None and not reasoning:
        body["temperature"] = temperature
    if max_tokens is not None:
        floor = REASONING_MIN_TOKENS if reasoning else 0
        body["max_completion_tokens"] = max(max_tokens, floor)
    return body


async def openai_chat(*, transport: httpx.AsyncBaseTransport | None = None, **request) -> str:
    """The model's reply text. Raises on any HTTP error, as the gateway client does."""
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}"}
    async with httpx.AsyncClient(timeout=300, transport=transport) as client:
        resp = await client.post(OPENAI_CHAT_URL, json=openai_request(**request), headers=headers)
        resp.raise_for_status()
    choices = resp.json().get("choices") or [{}]
    return (choices[0].get("message") or {}).get("content") or ""
