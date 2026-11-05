"""The story's files reach Planning (2026-10-27): documents, sheets, text,
images and screen recordings — read once, named when not read, and compared
by the memory without making a reworded story look changed.

Jira's attachment field and its download (a 303 to the media host) are faked
as Jira sends them; documents are real bytes parsed by the platform's readers.
"""

from __future__ import annotations

import asyncio
import io

import httpx
from docx import Document

from routing import memory, pipeline
from shared import attachments
from shared.video import VideoExtraction, VideoFrame
from shared.video_understanding import understand_video
from tools import pipeline_tools


def _docx(text: str) -> bytes:
    doc = Document()
    doc.add_paragraph(text)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


FILES = {
    "rules.docx": _docx("A remark is at most 50 characters."),
    "cases.csv": b"remark,expected\nrent October,accepted\ncafe!,rejected\n",
    "notes.md": b"# Notes\nRemarks are never sent to the receiving bank.\n",
    "error.png": b"\x89PNG\r\n\x1a\n not really a picture",
}


def _jira(files: dict[str, bytes], broken: set[str] = frozenset()) -> httpx.AsyncClient:
    names = list(files)
    listed = [
        {
            "id": str(10001 + i),
            "filename": name,
            "mimeType": "",
            "size": len(files[name]),
            "created": f"2026-10-27T05:{i:02d}:00.000+0530",
            "content": f"https://x.atlassian.net/rest/api/3/attachment/content/{10001 + i}",
        }
        for i, name in enumerate(names)
    ]
    listed.reverse()  # Jira's order is not the order they were attached in

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/rest/api/3/issue/BTS-1":
            return httpx.Response(200, json={"fields": {"attachment": listed}})
        if "/attachment/content/" in path:
            name = names[int(path.rsplit("/", 1)[1]) - 10001]
            if name in broken:
                return httpx.Response(500)
            return httpx.Response(303, headers={"Location": f"https://media.example/{name}"})
        if request.url.host == "media.example":
            return httpx.Response(200, content=files[path.strip("/")])
        return httpx.Response(404)

    return httpx.AsyncClient(
        base_url="https://x.atlassian.net", transport=httpx.MockTransport(handler)
    )


async def _read(files, media=False, broken=frozenset()):
    async with _jira(files, broken) as client:
        return await pipeline_tools._attachments(client, "BTS-1", "Remarks", media)


async def test_documents_are_read_in_the_order_they_were_attached():
    files = await _read(FILES)
    assert [f["filename"] for f in files] == list(FILES)
    by_name = {f["filename"]: f for f in files}
    assert "at most 50 characters" in by_name["rules.docx"]["text"]
    assert "rent October" in by_name["cases.csv"]["text"]
    assert "receiving bank" in by_name["notes.md"]["text"]


async def test_without_media_an_image_is_kept_by_file_not_read():
    image = {f["filename"]: f for f in await _read(FILES)}["error.png"]
    assert image["kind"] == "image" and image["text"] == ""
    assert "compared by file" in image["note"]


async def test_with_media_an_image_is_read(monkeypatch):
    async def look(data, filename, mime_type="", **_):
        return attachments.Extracted(filename=filename, text="TEXT: Error: Remark too long")

    monkeypatch.setattr(attachments, "extract", look)
    image = {f["filename"]: f for f in await _read(FILES, media=True)}["error.png"]
    assert image["text"] == "TEXT: Error: Remark too long"


async def test_a_file_that_cannot_be_downloaded_is_named_with_why():
    files = {f["filename"]: f for f in await _read(FILES, broken={"cases.csv"})}
    assert (
        files["cases.csv"]["text"] == "" and "could not be downloaded" in files["cases.csv"]["note"]
    )
    assert files["rules.docx"]["text"]


async def test_only_the_first_files_are_read_and_the_rest_are_named(monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_FILES", 2)
    files = await _read(FILES)
    assert [bool(f["text"]) for f in files[:2]] == [True, True]
    assert all("only the first 2 files" in f["note"] for f in files[2:])


async def test_a_file_still_reading_at_the_deadline_is_named_not_waited_for(monkeypatch):
    async def slow(data, filename, mime_type="", **_):
        await asyncio.sleep(5)

    monkeypatch.setattr(attachments, "extract", slow)
    monkeypatch.setattr(pipeline, "FILES_SECONDS", 0.05)
    files = await _read({"notes.md": FILES["notes.md"]})
    assert files[0]["text"] == "" and "not read within" in files[0]["note"]


def test_planning_gets_each_file_and_the_ones_not_read():
    files = [
        {"filename": "rules.docx", "kind": "document", "text": "At most 50 characters."},
        {"filename": "demo.mp4", "kind": "video", "text": "[0:02] Step 2: Click Send"},
        {"filename": "call.m4a", "kind": "audio", "text": "", "note": "speech-to-text …"},
    ]
    text = pipeline.planning_text({"key": "BTS-1", "summary": "Remarks"}, [], [], None, [], files)
    assert "=== rules.docx (document) ===\nAt most 50 characters." in text
    assert "=== demo.mp4 (video) ===" in text
    assert "- call.m4a: speech-to-text …" in text


def test_the_files_share_one_budget(monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_FILES_CHARS", 30)
    files = [
        {"filename": "a.md", "kind": "document", "text": "x" * 40},
        {"filename": "b.md", "kind": "document", "text": "y" * 10},
    ]
    lines = pipeline.file_lines(files)
    assert (
        "x" * 30 + " …" in lines and "- b.md: not included: the files' space was used up" in lines
    )


def test_a_long_story_description_reaches_planning_whole():
    story = "rule. " * 1500  # 9,000 characters, cut at 3,000 before
    text = pipeline.planning_text(
        {"key": "BTS-1", "summary": "s", "description": story}, [], [], None
    )
    assert story.strip() in text


def test_the_reply_says_which_files_were_read():
    lines = pipeline.files_summary(
        [
            {"filename": "spec.pdf", "kind": "document", "read": True},
            {"filename": "demo.mp4", "kind": "video", "read": False, "note": "no screen read"},
        ]
    )
    assert lines == ["spec.pdf — read", "demo.mp4 (video) — not read: no screen read"]


def test_the_memory_compares_documents_by_text_and_pictures_by_file():
    root = {"key": "BTS-1", "summary": "Remarks", "description": "d"}
    files = [
        {"id": "10001", "filename": "rules.docx", "text": "At most 50 characters."},
        {"id": "10004", "filename": "error.png", "text": ""},
    ]
    text = memory.story_text(root, [], [], files)
    assert "- rules.docx (file 10001):\nAt most 50 characters." in text
    assert "- error.png (file 10004)" in text
    replaced = memory.story_text(root, [], [], [files[0], {**files[1], "id": "10009"}])
    assert memory.normalised_hash(text) != memory.normalised_hash(replaced)


async def test_a_video_with_no_screen_read_is_not_passed_on_as_read():
    frames = [VideoFrame(timestamp_seconds=0.0, png_bytes=b"x")]
    extraction = VideoExtraction(frames=frames, duration_seconds=4.0)

    async def blind(*_args, **_kwargs):
        return ""

    text = await understand_video("demo.mp4", extraction, model="m", describe=blind, chat=blind)
    assert text == ""
