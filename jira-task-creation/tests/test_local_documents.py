"""On a laptop run, every breakdown document is also kept in docs-export (2026-10-07).

Emailed PDF or tickets created, the same document lands in TASK_DOCUMENT_DIR —
drawn from the breakdown already written, so it costs no tokens. On Aetherion
nothing is saved.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.document import breakdown_pdf as pdf
from src.document.local_copy import save_local
from src.models.schemas import ResultStatus
from src.tools import tools as tool_module
from tests.test_local_pdf import _doc, small


@pytest.fixture
def laptop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("AETHERION_LOCAL_RUN", "1")
    monkeypatch.setenv("TASK_DOCUMENT_DIR", str(tmp_path / "task-creation"))
    return tmp_path / "task-creation"


def test_on_a_laptop_the_document_is_kept(laptop: Path) -> None:
    path = save_local("aetherion-breakdown-BGV-1-abcd.pdf", b"%PDF-1.7 x")
    assert path == str((laptop / "aetherion-breakdown-BGV-1-abcd.pdf").resolve())
    assert Path(path).read_bytes() == b"%PDF-1.7 x"


def test_on_aetherion_nothing_is_saved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AETHERION_LOCAL_RUN", raising=False)
    monkeypatch.setenv("TASK_DOCUMENT_DIR", str(tmp_path))
    assert save_local("x.pdf", b"%PDF") == ""
    assert not list(tmp_path.iterdir())


def test_a_name_cannot_leave_the_folder(laptop: Path) -> None:
    path = save_local("../../evil.pdf", b"%PDF")
    assert Path(path).parent == laptop.resolve()


def test_the_created_version_of_the_pdf_says_so() -> None:
    html = pdf.breakdown_html(_doc(created=["BGV-110 · Story · Bulk print"]))
    assert pdf.CREATED_LINE in html and pdf.MODE_LINE not in html
    assert "Created in Jira" in html and "BGV-110 · Story · Bulk print" in html
    assert "Tickets (created in Jira)" in html and "(not created)" not in html


def test_the_pdf_only_version_is_unchanged() -> None:
    html = pdf.breakdown_html(_doc())
    assert pdf.MODE_LINE in html and "Proposed tickets (not created)" in html


def _created_result() -> dict:
    return {
        "status": ResultStatus.JIRA_CREATED.value,
        "epic": None,
        "stories": [{"key": "BGV-110", "issue_type": "Story", "summary": "Verify ID documents"}],
        "subtasks": [{"key": "BGV-111", "issue_type": "Sub-task", "summary": "Step 1"}],
        "created_keys": ["BGV-110", "BGV-111"],
    }


def test_created_tickets_get_a_created_in_jira_pdf_on_a_laptop(laptop: Path) -> None:
    breakdown = small()
    path = tool_module._keep_created_copy(breakdown, "x", _created_result(), "BGV", "")
    assert path and Path(path).name.startswith("aetherion-created-BGV-REQUEST-")
    assert Path(path).read_bytes().startswith(b"%PDF")


def test_no_copy_when_nothing_was_created(laptop: Path) -> None:
    breakdown = small()
    failed = {**_created_result(), "status": ResultStatus.JIRA_CREATION_FAILED.value}
    assert tool_module._keep_created_copy(breakdown, "x", failed, "BGV", "") == ""
    assert not laptop.exists()


def test_a_broken_breakdown_never_fails_the_creation(laptop: Path) -> None:
    assert tool_module._keep_created_copy({"nonsense": 1}, "x", _created_result(), "BGV", "") == ""


def test_the_tool_passes_the_saved_path_back(laptop: Path, monkeypatch) -> None:
    async def created(*_a, **_k):
        return _created_result()

    monkeypatch.setattr(tool_module, "_create_jira_issues", created)
    fn = getattr(tool_module.create_jira_issues, "__wrapped__", tool_module.create_jira_issues)
    result = asyncio.run(fn(small(), "x", "BGV"))
    assert result["created_keys"] == ["BGV-110", "BGV-111"], "the creation result is unchanged"
    assert Path(result["saved_to"]).exists()
