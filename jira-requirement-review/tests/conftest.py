"""Shared test fixtures.

Sets dummy Jira/LLM env so modules that read settings can be constructed without
real credentials. Pure-logic tests ignore these.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok-abcdef123456")
    monkeypatch.setenv("OPENAI_MODEL_ID", "gpt-4o")
    monkeypatch.delenv("TENANT_ID", raising=False)
    # Guard against a real .env being loaded into the process by any module's
    # module-level load_dotenv() (e.g. a tools.* module imported during test
    # collection) — without this, whichever test happens to import such a
    # module first leaks its .env values into every later test in the session.
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
