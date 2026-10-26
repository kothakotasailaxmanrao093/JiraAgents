"""Give Planning and Coding an OpenAI client on a laptop (2026-10-26).

The platform refuses an agent whose own code builds an OpenAI client (it would
bypass the AI gateway), so their published code asks src/.../local_client.py
for one, and only a laptop run installs it, from here. Never imported by the
agents themselves.
"""

from __future__ import annotations

import importlib
import os

_CLIENT_MODULES = {
    "planning-agent": "src.ingestion.local_client",
    "coding-agent": "src.coding.local_client",
}


def install(agent: str) -> bool:
    """Install the OpenAI client class for this agent; False when not needed."""
    module = _CLIENT_MODULES.get(agent)
    if not module or not os.environ.get("OPENAI_API_KEY"):
        return False
    from openai import AsyncOpenAI

    importlib.import_module(module).install(AsyncOpenAI)
    return True
