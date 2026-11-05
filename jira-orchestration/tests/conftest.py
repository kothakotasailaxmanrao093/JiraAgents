"""The router imports its own package flat, like the review agent does."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def _no_local_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shell started by scripts/run_local.sh exports OPENAI_API_KEY, which
    would switch every model call to OpenAI; tests must see the gateway path."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AETHERION_LOCAL_RUN", raising=False)
    # The developer's ORCH_GITHUB_ORG would make every command read GitHub's
    # repository list; tests that want it turn it back on (test_org_repos.py).
    monkeypatch.setenv("ORCH_PLANNING_ORG_REPOS", "false")
    # Nor the developer's GitHub Enterprise server: tests use github.com's paths.
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
