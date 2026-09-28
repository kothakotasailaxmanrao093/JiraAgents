"""The version must read the same in all three places it is written.

`pyproject.toml` and `metadata.json` drifted once: the metadata was left behind
while pyproject was bumped three times, so the platform UI reported a version
nobody had deployed.

The third place — the `AGENT_VERSION` constant — exists because this agent runs
inside a Temporal workflow, which may not read files. It cannot look its own
version up at runtime, so it carries it. That makes a third chance to drift, and
this is the test that removes it.

`scripts/publish.py` writes all three together; this fails if anyone edits one
by hand.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CONSTANT_FILE = PROJECT / "src/config.py"

_PYPROJECT = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_CONSTANT = re.compile(r'^AGENT_VERSION\s*=\s*"([^"]+)"', re.MULTILINE)

FIX = "Bump with: uv run python scripts/publish.py --bump patch"


def _pyproject_version() -> str:
    match = _PYPROJECT.search((PROJECT / "pyproject.toml").read_text())
    assert match, "no version in pyproject.toml"
    return match.group(1)


def _metadata_version() -> str:
    return str(json.loads((PROJECT / "src/agent/metadata.json").read_text())["version"])


def _constant_version() -> str:
    match = _CONSTANT.search(CONSTANT_FILE.read_text())
    assert match, f"no AGENT_VERSION in {CONSTANT_FILE}"
    return match.group(1)


def test_pyproject_and_metadata_agree() -> None:
    """The platform registers the metadata version, so a mismatch means the UI
    reports a number that was never deployed."""
    assert _pyproject_version() == _metadata_version(), FIX


def test_the_constant_agrees_with_the_files() -> None:
    """The agent reports this version to the router, and the router puts it in
    the Jira comment. A stale constant misattributes every reply."""
    assert _constant_version() == _pyproject_version(), FIX
