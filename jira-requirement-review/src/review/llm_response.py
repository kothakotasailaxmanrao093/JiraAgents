"""Post-processing of raw LLM text responses (pure, no SDK).

The AI Gateway returns the assistant message text, but there is no native
JSON-object mode, so we instruct the model to return only JSON and parse it
defensively here. Kept separate from the tool so it is unit-testable without the
SDK/gateway.
"""

from __future__ import annotations

import json
import re

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(content: str) -> dict:
    """Best-effort: pull a JSON object out of possibly fenced/wrapped LLM text.

    Tries, in order: direct ``json.loads``; a ```` ```json ```` fenced block; the
    outermost ``{ ... }`` slice. Raises ``ValueError`` if nothing parses.
    """
    if not content or not content.strip():
        raise ValueError("empty LLM content")
    text = content.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    m = _FENCE.search(text)
    if m:
        try:
            parsed = json.loads(m.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        parsed = json.loads(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("no JSON object found in LLM content")
