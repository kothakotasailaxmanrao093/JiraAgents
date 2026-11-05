"""The breakdown PDF kept for Review, when the orchestrator asks (2026-10-08).

A request started on the orchestrator's form writes nothing to Jira. Task
Creation's PDF is the document Requirement Review scores, so it is kept in the
storage every agent reads and its key travels back in the result contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agent.agent import JiraTaskCreation
from src.document import kept
from src.models.schemas import ResultStatus
from src.tools import tools
from tests import test_local_pdf
from tests.test_local_pdf import _results, request
from tests.test_root_ticket import TYPES, large

# The PDF tests' own fixtures: the outbox that captures email, and the recorder.
outbox = test_local_pdf.outbox
run = test_local_pdf.run

KEY = "breakdowns/20261008T101500Z_aetherion-breakdown-BGV-REQUEST-1.pdf"


@pytest.fixture
def storage(monkeypatch) -> list[tuple]:
    from common_lib.storage import storage_client

    stored: list[tuple] = []

    def store(bucket, key, data, content_type="application/octet-stream"):
        stored.append((bucket, key, data, content_type))
        return bucket, key

    monkeypatch.setenv("TENANT_ID", "team-bucket")
    monkeypatch.setattr(storage_client.storage, "init_client", lambda: None)
    monkeypatch.setattr(storage_client.storage, "store_object", store)
    return stored


def test_the_pdf_is_kept_in_the_shared_storage(storage) -> None:
    key = kept.keep_document("aetherion breakdown/BGV.pdf", b"%PDF-1.4")
    [(bucket, stored_key, data, kind)] = storage
    assert (bucket, data, kind) == ("team-bucket", b"%PDF-1.4", "application/pdf")
    assert key == stored_key and stored_key.startswith("breakdowns/")
    assert stored_key.endswith("_BGV.pdf")


def test_without_storage_nothing_is_kept_and_nothing_raises(monkeypatch) -> None:
    monkeypatch.delenv("TENANT_ID", raising=False)
    assert kept.keep_document("x.pdf", b"%PDF") == ""


async def test_asked_to_keep_the_pdf_its_key_comes_back(
    fake_jira, jira_env, outbox, storage
) -> None:
    fake_jira(issue_types=TYPES)
    result = await tools.generate_breakdown_pdf(
        request(large(), issue_key="", root_key="", keep=True)
    )
    assert result["status"] == ResultStatus.PLANNED.value
    assert result["file_key"] == storage[0][1]
    assert storage[0][2].startswith(b"%PDF")


async def test_not_asked_nothing_is_kept(fake_jira, jira_env, outbox, storage) -> None:
    fake_jira(issue_types=TYPES)
    result = await tools.generate_breakdown_pdf(request(large(), issue_key="", root_key=""))
    assert result["file_key"] == "" and storage == []


async def test_a_kept_pdf_is_not_lost_when_email_is_down(
    fake_jira, jira_env, storage, monkeypatch, gmail_env
) -> None:
    from src.notifications import email as notifier

    def down(*_a, **_k):
        raise ConnectionError("smtp unreachable")

    monkeypatch.setattr(notifier, "_send_sync", down)
    fake_jira(issue_types=TYPES)
    result = await tools.generate_breakdown_pdf(
        request(large(), issue_key="", root_key="", keep=True)
    )
    assert result["status"] == ResultStatus.PLANNED.value
    assert "is kept for review" in result["summary"] and "could not be emailed" in result["summary"]
    assert result["headline"] == "Work breakdown ready as a PDF — no Jira changes made"


async def test_the_orchestrator_gets_the_documents_key(run) -> None:
    results: dict[str, Any] = _results(local_pdf=True)
    results["generate_breakdown_pdf"] = {
        **results["generate_breakdown_pdf"],
        "attachment": "aetherion-breakdown-BGV-REQUEST-1.pdf",
        "file_key": KEY,
    }
    recorder = run(**results)
    result = await JiraTaskCreation.fn(
        {
            "mode": "delegated",
            "requirement": "Add a daily transfer limit.",
            "project_key": "BGV",
            "output": "pdf_by_email",
            "keep_document": True,
        }
    )
    _, args, _ = recorder.call("generate_breakdown_pdf")
    assert args[0]["keep"] is True and args[0]["issue_key"] == ""
    assert result["outcome"] == "PLANNED"
    assert result["documents"] == [
        {"name": "aetherion-breakdown-BGV-REQUEST-1.pdf", "file_key": KEY}
    ]
    assert not recorder.ran("read_jira_issue"), "no ticket is read"
    assert not recorder.ran("create_jira_issues"), "nothing is created"
