"""Files uploaded on the Task Creation form are read (2026-09-30).

The form had no upload field: a spec or a transcript could only reach the
agent attached to a Jira ticket. Now files picked on the form are stored by the
platform, their keys arrive in ``uploaded_files``, and each is read like a Jira
attachment — into the breakdown, the sources listed and the PDF.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from src.agent import agent as agent_module
from src.agent.agent import JiraTaskCreation
from src.context import uploads
from src.shared.attachments import supported
from src.tools import tools
from tests.test_agent_webhook import DEFAULTS, Recorder, jira_context
from tests.test_root_ticket import small

VTT = (
    "WEBVTT\n\nab12/1-0\n00:00:04.210 --> 00:00:09.870\n"
    "<v Anjali Mehta>Any gap longer than 6 months needs an explanation.</v>\n"
)


def docx_bytes(*lines: str) -> bytes:
    from docx import Document

    document = Document()
    for line in lines:
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def storage(monkeypatch) -> dict[str, Any]:
    """The platform's object storage, answering from a dict: "bucket/key" -> bytes.

    Only its two calls are replaced; the module stays real, because the
    platform's document readers import from it too.
    """
    from common_lib.storage import storage_client

    stored: dict[str, Any] = {}

    def retrieve(bucket: str, key: str, mode: Any) -> bytes:
        assert mode == storage_client.RetrievalMode.FULL_OBJECT
        value = stored[f"{bucket}/{key}"]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(storage_client.storage, "init_client", lambda: None)
    monkeypatch.setattr(storage_client.storage, "retrieve", retrieve)
    monkeypatch.setenv("TENANT_ID", "team-7")
    return stored


# --- which files were uploaded ------------------------------------------------------


def test_the_keys_come_in_order_without_repeats() -> None:
    assert uploads.upload_keys(
        {"uploaded_files": ["u/spec.docx", "u/call.vtt", "u/spec.docx"]}
    ) == [
        "u/spec.docx",
        "u/call.vtt",
    ]


def test_a_string_or_key_objects_are_understood_too() -> None:
    assert uploads.upload_keys({"uploaded_files": "u/a.pdf, u/b.vtt"}) == ["u/a.pdf", "u/b.vtt"]
    assert uploads.upload_keys({"supporting_files": [{"key": "u/c.docx"}]}) == ["u/c.docx"]


def test_the_file_name_is_the_last_part_of_the_key() -> None:
    assert uploads.file_name("team-7/uploads/9f3a/Employment_Gap_Spec.docx") == (
        "Employment_Gap_Spec.docx"
    )


# --- reading them ---------------------------------------------------------------------


async def test_a_spec_and_a_transcript_are_both_read(storage) -> None:
    storage["team-7/u/Employment_Gap_Spec.docx"] = docx_bytes("A gap over 3 months is explained.")
    storage["team-7/u/Employment_Gap_Call.vtt"] = VTT.encode()
    result = await tools.read_uploaded_files(
        {"uploaded_files": ["u/Employment_Gap_Spec.docx", "u/Employment_Gap_Call.vtt"]}
    )
    files = {f["filename"]: f for f in result["files"]}
    assert "A gap over 3 months is explained." in files["Employment_Gap_Spec.docx"]["text"]
    assert files["Employment_Gap_Call.vtt"]["text"] == (
        "[00:00:04] Anjali Mehta: Any gap longer than 6 months needs an explanation."
    )
    section = result["section"]
    assert section.startswith(f"## {uploads.HEADING_UPLOADS}")
    assert "### Attachment: Employment_Gap_Spec.docx" in section
    assert "### Attachment: Employment_Gap_Call.vtt" in section


async def test_the_payload_team_is_the_bucket_when_given(storage) -> None:
    storage["team-9/u/notes.txt"] = b"Officers work in two shifts."
    result = await tools.read_uploaded_files({"uploaded_files": ["u/notes.txt"]}, "team-9")
    assert result["files"][0]["text"] == "Officers work in two shifts."


async def test_a_file_that_cannot_be_read_is_named_with_why(storage) -> None:
    storage["team-7/u/lost.pdf"] = RuntimeError("NoSuchKey")
    storage["team-7/u/notes.txt"] = b"Officers work in two shifts."
    result = await tools.read_uploaded_files({"uploaded_files": ["u/lost.pdf", "u/notes.txt"]})
    lost, notes = result["files"]
    assert lost["filename"] == "lost.pdf" and not lost["text"]
    assert "NoSuchKey" in lost["note"]
    assert notes["text"] == "Officers work in two shifts.", "one bad file never loses the rest"


async def test_without_object_storage_every_file_says_so(monkeypatch) -> None:
    monkeypatch.delenv("TENANT_ID", raising=False)
    result = await tools.read_uploaded_files({"uploaded_files": ["u/spec.docx"]})
    [only] = result["files"]
    assert only["note"] == uploads.NO_STORAGE
    assert result["section"] == ""


async def test_an_unsupported_type_is_named_not_downloaded(storage) -> None:
    result = await tools.read_uploaded_files({"uploaded_files": ["u/archive.zip"]})
    assert "Unsupported attachment type '.zip'" in result["files"][0]["note"]


# --- the build uses them --------------------------------------------------------------


UPLOADED = {
    "files": [
        {"filename": "Employment_Gap_Spec.docx", "text": "A gap over 3 months is explained."},
        {"filename": "scan.zip", "text": "", "note": "Unsupported attachment type '.zip'"},
    ],
    "notes": [],
    "section": "## Uploaded on the Aetherion form (supporting material)\n"
    "### Attachment: Employment_Gap_Spec.docx\nA gap over 3 months is explained.",
}


def _results() -> dict[str, Any]:
    return {
        "read_uploaded_files": UPLOADED,
        "inspect_jira_context": jira_context(),
        "validate_requirement": {
            "status": "READY_FOR_JIRA",
            "requirement": "x",
            "request_text": "x",
        },
        "generate_work_breakdown": {"breakdown": small(), "counts": {}, "duplicate_matches": []},
        "generate_breakdown_pdf": {"status": "PLANNED", "summary": "emailed", "proposed": []},
        **DEFAULTS,
    }


@pytest.fixture
def run(monkeypatch):
    recorder = Recorder(_results())
    monkeypatch.setattr(agent_module.toolExecutor, "execute", recorder.execute)
    return recorder


async def test_a_manual_run_builds_from_the_uploaded_files(run) -> None:
    await JiraTaskCreation.fn(
        {
            "requirement": "Candidates explain gaps in their work history.",
            "project_key": "KS",
            "uploaded_files": ["team-7/u/Employment_Gap_Spec.docx", "team-7/u/scan.zip"],
            "output": "pdf_by_email",
        }
    )
    _, args, _ = run.call("read_uploaded_files")
    assert args[0] == {"uploaded_files": ["team-7/u/Employment_Gap_Spec.docx", "team-7/u/scan.zip"]}

    _, args, _ = run.call("validate_requirement")
    assert args[0].startswith("Candidates explain gaps in their work history.")
    assert "### Attachment: Employment_Gap_Spec.docx" in args[0]

    _, args, _ = run.call("generate_breakdown_pdf")
    report = args[0]["report"]
    assert "Attachment: Employment_Gap_Spec.docx" in report["sources_read"]
    assert any("scan.zip" in line for line in report["sources_missing"]), "named, not dropped"


async def test_uploads_can_be_the_whole_requirement(run) -> None:
    await JiraTaskCreation.fn(
        {
            "project_key": "KS",
            "uploaded_files": ["u/Employment_Gap_Spec.docx"],
            "output": "pdf_by_email",
        }
    )
    _, args, _ = run.call("validate_requirement")
    assert args[0] == UPLOADED["section"]


async def test_without_uploads_nothing_is_read_from_storage(run) -> None:
    await JiraTaskCreation.fn({"requirement": "Send reminders.", "project_key": "KS"})
    assert not run.ran("read_uploaded_files")


# --- the form -------------------------------------------------------------------------

# Printed by the platform when it refused ".srt" (2026-09-30).
PLATFORM_UPLOADS = set(
    ".bmp .csv .doc .docx .gif .jpeg .jpg .json .md .odt .pdf .png .ppt .pptx .tar "
    ".tar.gz .tgz .tif .tiff .txt .vtt .webp .xls .xlsx .zip".split()
)


def test_the_form_has_an_upload_field_for_what_the_agent_can_read() -> None:
    triggers = json.loads(Path("src/agent/metadata.json").read_text())["config"]["triggers"]
    [field] = [t for t in triggers if t["type"] == "file"]
    offered = set(field["accepts"].split(","))
    assert {".docx", ".pdf", ".vtt"} <= offered
    assert offered <= PLATFORM_UPLOADS, offered - PLATFORM_UPLOADS
    assert all(supported(f"x{ext}") for ext in offered), "never offer a type it cannot read"
    assert field["multiple"] is True
    assert field["name"] in agent_module._UPLOAD_FIELDS
