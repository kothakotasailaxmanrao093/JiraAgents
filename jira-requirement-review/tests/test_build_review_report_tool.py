"""Where the Word report goes: object storage on Aetherion, a folder on a laptop."""

from __future__ import annotations

import pytest
from docx import Document

from tools import build_review_report_tool as tool_module

RESULTS = [
    {
        "issue_key": "KS-1",
        "summary": "Bulk archive",
        "status": "success",
        "by_category": {"Ambiguity": 1},
        "readiness": {"level": "needs_major_clarification", "score": 2},
        "findings": [{"finding_type": "Ambiguity", "description": "Unarchive in scope?"}],
    }
]


def _run(**kwargs):
    import asyncio

    fn = getattr(tool_module.build_review_report, "__wrapped__", tool_module.build_review_report)
    return asyncio.run(fn("KS-1", RESULTS, **kwargs))


@pytest.fixture
def no_storage(monkeypatch):
    """Fail the test if the storage client is touched."""
    import common_lib.storage.storage_client as sc

    def boom(*a, **k):
        raise AssertionError("object storage must not be used on a laptop run")

    monkeypatch.setattr(sc.storage, "init_client", boom)
    monkeypatch.setattr(sc.storage, "store_object", boom)


def test_laptop_run_saves_the_docx_in_the_report_folder(monkeypatch, tmp_path, no_storage):
    monkeypatch.setenv("AETHERION_LOCAL_RUN", "1")
    monkeypatch.setenv("REVIEW_REPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("TENANT_ID", "would-be-bucket")  # ignored on a laptop run

    out = _run()

    assert out["error"] is None
    assert out["s3_key"] is None
    saved = tmp_path / "reports" / out["local_path"].rsplit("/", 1)[-1]
    assert out["local_path"] == str(saved.resolve())
    assert saved.name.startswith("KS_1_") and saved.suffix == ".docx"
    text = "\n".join(p.text for p in Document(str(saved)).paragraphs)
    assert "KS-1" in text and "Unarchive in scope?" in text


def test_platform_run_without_a_bucket_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TENANT_ID", raising=False)

    out = _run()

    assert out["s3_key"] is None
    assert out["error"] == "TENANT_ID not set"
    assert "local_path" not in out
    assert not (tmp_path / "review-reports").exists()


def test_platform_run_uploads_to_the_bucket(monkeypatch):
    import common_lib.storage.storage_client as sc

    stored = {}
    monkeypatch.setattr(sc.storage, "init_client", lambda *a, **k: None)
    monkeypatch.setattr(sc.storage, "store_object", lambda **kw: stored.update(kw))
    monkeypatch.setenv("TENANT_ID", "tenant-bucket")

    out = _run()

    assert out["error"] is None
    assert out["s3_key"].startswith("RequirementReviewReports/KS_1_")
    assert stored["bucket_name"] == "tenant-bucket"
    assert "local_path" not in out
