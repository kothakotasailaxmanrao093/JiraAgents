"""Video and audio attachments (2026-10-21): screens read, voice reported honestly.

Real ffmpeg (the imageio-ffmpeg binary) on clips generated per test; only the
vision call is faked, since the AI Gateway is not reachable from a laptop.
"""

import subprocess

import imageio_ffmpeg
import pytest
from agent_lib.utils import file_utils

from src.shared.attachments import Limits, extract, supported


def _make(path, *inputs, extra=()):
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error"]
    for spec in inputs:
        cmd += ["-f", "lavfi", "-i", spec]
    cmd += [*extra, str(path)]
    subprocess.run(cmd, check=True)
    return path.read_bytes()


@pytest.fixture
def vision(monkeypatch):
    calls = []

    def fake(image_bytes):
        calls.append(image_bytes)
        return f"screen {len(calls)}"

    monkeypatch.setattr(file_utils, "extract_text_from_image", fake)
    return calls


def test_media_types_are_supported():
    for name in ("a.mp4", "a.mov", "a.webm", "a.mkv", "a.avi", "a.mp3", "a.wav", "a.m4a"):
        assert supported(name)


async def test_video_with_voice_reads_every_screen_and_names_the_voice(tmp_path, vision):
    data = _make(
        tmp_path / "meeting.mp4",
        "testsrc=duration=20:size=320x240:rate=10",
        "sine=frequency=440:duration=20",
        extra=["-shortest", "-pix_fmt", "yuv420p"],
    )
    result = await extract(data, "meeting.mp4", limits=Limits(max_video_frames=6))
    assert len(vision) == 6
    assert result.text.startswith("[Video, 0:20 long, 6 screen(s) read]")
    assert "[0:00] screen 1" in result.text and "screen 6" in result.text
    assert "Speech-to-text is not available" in result.text


async def test_silent_video_has_no_voice_line(tmp_path, vision):
    data = _make(
        tmp_path / "s.mp4", "testsrc=duration=5:size=320x240:rate=10", extra=["-pix_fmt", "yuv420p"]
    )
    result = await extract(data, "s.mp4", limits=Limits(max_video_frames=3))
    assert len(vision) == 3 and "spoken audio" not in result.text


async def test_voice_memo_is_reported_not_pretended(tmp_path, vision):
    data = _make(tmp_path / "v.mp3", "sine=frequency=300:duration=3")
    result = await extract(data, "v.mp3")
    assert not result.usable and not vision
    assert "speech-to-text is not available" in result.note


async def test_a_broken_video_is_a_note_not_a_crash(vision):
    result = await extract(b"definitely not a video", "x.mp4")
    assert not result.usable and result.note == "Could not read this file as audio or video."


async def test_media_has_its_own_bigger_size_cap(vision):
    big = b"0" * (11 * 1024 * 1024)  # over the 10 MB document cap, under 100 MB
    doc = await extract(big, "spec.pdf")
    assert "over the" in doc.note
    small_media_cap = await extract(big, "x.mp4", limits=Limits(max_media_bytes=1024))
    assert "over the 1 KB limit" in small_media_cap.note


async def test_videos_are_skipped_when_image_reading_is_off(tmp_path, vision):
    assert not supported("a.mp4", read_images=False)
