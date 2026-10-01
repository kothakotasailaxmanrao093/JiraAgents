"""Meeting transcripts (.vtt, .srt) are read, keeping who spoke and when (2026-09-30).

A .vtt attached to a ticket or a Confluence page was skipped as an unsupported
type, and an uploaded one reached the model with every cue number and timing
line in it. The fixtures are the formats Teams, Zoom and SRT tools really export.
"""

from __future__ import annotations

import pytest

from context.base import FetchContext
from context.transcript_source import TranscriptSource
from shared.attachments import extract, supported
from shared.transcripts import clean_transcript, is_transcript

TEAMS_VTT = """WEBVTT

0d5c7a1e-1f2b-4c3d-9e8f-0a1b2c3d4e5f/12-0
00:00:03.120 --> 00:00:06.480
<v Priya Sharma>So the candidate uploads one address proof.</v>

0d5c7a1e-1f2b-4c3d-9e8f-0a1b2c3d4e5f/13-0
00:00:06.480 --> 00:00:09.900
<v Priya Sharma>Electricity bill or rental agreement, PDF or JPG.</v>

0d5c7a1e-1f2b-4c3d-9e8f-0a1b2c3d4e5f/14-0
00:00:10.200 --> 00:00:14.050
<v Ravi Kumar>What&apos;s the size limit &amp; who reviews it?</v>

0d5c7a1e-1f2b-4c3d-9e8f-0a1b2c3d4e5f/15-0
00:01:02.000 --> 00:01:05.000
<v Priya Sharma>Five MB. The officer accepts or rejects it.</v>
"""

ZOOM_VTT = """WEBVTT

1
00:00:03.120 --> 00:00:06.480
Priya Sharma: So the candidate uploads one address proof.

2
00:00:06.480 --> 00:00:09.900
Priya Sharma: Electricity bill or rental agreement.
"""

SRT = """1
00:00:03,120 --> 00:00:06,480
Priya: A rejection needs a reason.

2
00:00:06,480 --> 00:00:09,900
Ravi: And the candidate is emailed.
"""


def test_a_teams_transcript_keeps_speaker_and_time_without_the_noise() -> None:
    assert clean_transcript(TEAMS_VTT) == (
        "[00:00:03] Priya Sharma: So the candidate uploads one address proof. "
        "Electricity bill or rental agreement, PDF or JPG.\n"
        "[00:00:10] Ravi Kumar: What's the size limit & who reviews it?\n"
        "[00:01:02] Priya Sharma: Five MB. The officer accepts or rejects it."
    )


def test_a_zoom_transcript_stays_one_line_per_cue() -> None:
    """No <v> tag: the name is in the text, so nothing is joined."""
    assert clean_transcript(ZOOM_VTT) == (
        "[00:00:03] Priya Sharma: So the candidate uploads one address proof.\n"
        "[00:00:06] Priya Sharma: Electricity bill or rental agreement."
    )


def test_an_srt_file_is_read_the_same_way() -> None:
    assert clean_transcript(SRT) == (
        "[00:00:03] Priya: A rejection needs a reason.\n"
        "[00:00:06] Ravi: And the candidate is emailed."
    )


def test_the_same_words_from_two_people_are_both_kept() -> None:
    vtt = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Priya>Yes.</v>\n\n"
        "00:00:02.000 --> 00:00:03.000\n<v Ravi>Yes.</v>\n\n"
        "00:00:03.000 --> 00:00:04.000\n<v Ravi>Yes.</v>\n"
    )
    assert clean_transcript(vtt) == "[00:00:01] Priya: Yes.\n[00:00:02] Ravi: Yes."


def test_notes_styles_and_minute_only_timings() -> None:
    vtt = (
        "WEBVTT - exported\n\nNOTE recorded 12 Sep\n\nSTYLE\n::cue { color: red }\n\n"
        "01:12.500 --> 01:15.000 align:start\n<v.loud Priya>Max <b>5 MB</b>.</v>\n"
    )
    assert clean_transcript(vtt) == "[00:01:12] Priya: Max 5 MB."


def test_a_file_with_no_cues_is_returned_as_it_is() -> None:
    assert clean_transcript("Just meeting notes.\nNo timings.") == (
        "Just meeting notes.\nNo timings."
    )


def test_transcript_files_are_recognised() -> None:
    assert is_transcript("Standup.VTT") and is_transcript("call.srt")
    assert not is_transcript("notes.txt")


# --- attached to a ticket or a Confluence page ------------------------------------


@pytest.mark.parametrize("name", ["Requirements call.vtt", "call.srt"])
def test_a_transcript_attachment_is_a_supported_type(name: str) -> None:
    assert supported(name)


async def test_a_vtt_attached_to_a_ticket_is_read_cleanly() -> None:
    """Teams exports start with a byte-order mark; it must not hide the header."""
    got = await extract(("﻿" + TEAMS_VTT).encode("utf-8"), "Requirements call.vtt")
    assert got.usable, got.note
    assert got.text.startswith("[00:00:03] Priya Sharma: So the candidate")
    assert "-->" not in got.text and "WEBVTT" not in got.text


# --- uploaded on the Review form --------------------------------------------------


async def test_an_uploaded_vtt_reaches_the_review_cleanly(monkeypatch) -> None:
    """The real read, with only object storage stood in for."""
    import sys
    import types

    storage = types.SimpleNamespace(
        init_client=lambda: None,
        retrieve=lambda team, key, mode: ("\ufeff" + TEAMS_VTT).encode("utf-8"),
    )
    fake = types.ModuleType("common_lib.storage.storage_client")
    fake.storage = storage
    fake.RetrievalMode = types.SimpleNamespace(FULL_OBJECT="full")
    monkeypatch.setitem(sys.modules, "common_lib.storage.storage_client", fake)

    ctx = FetchContext(issue_key="BGV-1", transcript_file_keys=["uploads/call.vtt"], team_id="t1")
    [doc] = await TranscriptSource().load(ctx)
    assert doc.text.startswith("[00:00:03] Priya Sharma: So the candidate")
    assert "-->" not in doc.text and "WEBVTT" not in doc.text


def test_the_upload_field_offers_only_types_the_platform_accepts() -> None:
    """Publishing refused ".srt" on the upload field (2026-09-30); the list below is
    the one the platform printed. .srt is still read when attached to a ticket."""
    import json
    from pathlib import Path

    platform = set(
        ".bmp .csv .doc .docx .gif .jpeg .jpg .json .md .odt .pdf .png .ppt .pptx .tar "
        ".tar.gz .tgz .tif .tiff .txt .vtt .webp .xls .xlsx .zip".split()
    )
    triggers = json.loads(Path("src/agent/metadata.json").read_text())["config"]["triggers"]
    [upload] = [t for t in triggers if t["type"] == "file"]
    offered = set(upload["accepts"].split(","))
    assert ".vtt" in offered
    assert offered <= platform, offered - platform


async def test_a_transcript_and_a_spec_uploaded_together_are_both_read(monkeypatch) -> None:
    """The upload takes several files; each is read and named (2026-09-30)."""
    import sys
    import types

    files = {
        "uploads/call.vtt": TEAMS_VTT.encode("utf-8"),
        "uploads/notes.md": b"Officers work in two shifts.",
    }
    storage = types.SimpleNamespace(
        init_client=lambda: None, retrieve=lambda team, key, mode: files[key]
    )
    fake = types.ModuleType("common_lib.storage.storage_client")
    fake.storage = storage
    fake.RetrievalMode = types.SimpleNamespace(FULL_OBJECT="full")
    monkeypatch.setitem(sys.modules, "common_lib.storage.storage_client", fake)

    ctx = FetchContext(issue_key="BGV-1", transcript_file_keys=list(files), team_id="t1")
    call, notes = await TranscriptSource().load(ctx)
    assert call.source_label == "Meeting Transcript"
    assert call.text.startswith("[00:00:03] Priya Sharma:")
    assert notes.source_label == "Uploaded file: notes.md"
    assert notes.text == "Officers work in two shifts."


async def test_one_unreadable_upload_is_named_and_the_rest_still_read(monkeypatch) -> None:
    import sys
    import types

    def retrieve(team, key, mode):
        if key.endswith("lost.pdf"):
            raise RuntimeError("NoSuchKey")
        return b"Officers work in two shifts."

    fake = types.ModuleType("common_lib.storage.storage_client")
    fake.storage = types.SimpleNamespace(init_client=lambda: None, retrieve=retrieve)
    fake.RetrievalMode = types.SimpleNamespace(FULL_OBJECT="full")
    monkeypatch.setitem(sys.modules, "common_lib.storage.storage_client", fake)

    ctx = FetchContext(
        issue_key="BGV-1", transcript_file_keys=["u/lost.pdf", "u/notes.txt"], team_id="t1"
    )
    [doc] = await TranscriptSource().load(ctx)
    assert doc.source_label == "Uploaded file: notes.txt"
    assert any("lost.pdf could not be read: NoSuchKey" in w for w in ctx.warnings)


def test_the_upload_field_takes_several_files() -> None:
    import json
    from pathlib import Path

    triggers = json.loads(Path("src/agent/metadata.json").read_text())["config"]["triggers"]
    [upload] = [t for t in triggers if t["type"] == "file"]
    assert upload["multiple"] is True
