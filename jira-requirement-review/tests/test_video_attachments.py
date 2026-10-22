"""Video and audio attachments: every screen read, the whole video explained.

2026-10-21: frames read, voice reported honestly. 2026-10-22: a recording with
no voice is understood from its screens — frames are chosen where the screen
changes, each is described, and one more call explains what happened.

Real ffmpeg (the imageio-ffmpeg binary) on clips generated per test; only the
model calls are faked, since the AI Gateway is not reachable from a laptop.
"""

import subprocess

import imageio_ffmpeg
import pytest

from shared import video_understanding
from shared.attachments import Limits, extract, supported
from shared.video import extract_video

EXPLAINED = "WHAT THIS VIDEO SHOWS: a transfer that fails.\nSTEP BY STEP:\n1. [0:00] ..."


def _make(path, *inputs, extra=()):
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error"]
    for spec in inputs:
        cmd += ["-f", "lavfi", "-i", spec]
    cmd += [*extra, str(path)]
    subprocess.run(cmd, check=True)
    return path.read_bytes()


def _three_screens(tmp_path):
    """Black for 4 s, white for 5 s, blue for 3 s: three screens, no voice."""
    return _make(
        tmp_path / "screens.mp4",
        "color=c=black:s=320x240:d=4:r=10",
        "color=c=white:s=320x240:d=5:r=10",
        "color=c=blue:s=320x240:d=3:r=10",
        extra=[
            "-filter_complex",
            "[0][1][2]concat=n=3:v=1[v]",
            "-map",
            "[v]",
            "-pix_fmt",
            "yuv420p",
        ],
    )


class Models:
    def __init__(self):
        self.screens = []
        self.prompts = []
        self.narration = EXPLAINED

    async def describe(self, frame, prompt, model):
        self.screens.append(prompt)
        return f"screen {len(self.screens)}"

    async def chat(self, prompt, model):
        self.prompts.append(prompt)
        if isinstance(self.narration, Exception):
            raise self.narration
        return self.narration


@pytest.fixture
def models(monkeypatch):
    fake = Models()
    monkeypatch.setattr(video_understanding, "_gateway_describe", fake.describe)
    monkeypatch.setattr(video_understanding, "_gateway_chat", fake.chat)
    return fake


def test_media_types_are_supported():
    for name in ("a.mp4", "a.mov", "a.webm", "a.mkv", "a.avi", "a.mp3", "a.wav", "a.m4a"):
        assert supported(name)


def test_frames_are_taken_where_the_screen_changes(tmp_path):
    extraction = extract_video(_three_screens(tmp_path), "screens.mp4", max_frames=12)
    times = [round(f.timestamp_seconds) for f in extraction.frames]
    assert times == [0, 4, 9]
    assert not extraction.audio_present


async def test_a_silent_video_is_explained_from_its_screens(tmp_path, models):
    data = _three_screens(tmp_path)
    result = await extract(data, "screens.mp4", context="BTS-30: Transfer fails")
    assert result.usable
    assert result.text.startswith("[Video, 0:12 long, 3 screen(s) read]")
    assert EXPLAINED in result.text
    assert "SCREEN BY SCREEN:\n[0:00] screen" in result.text
    assert "spoken audio" not in result.text
    # the ticket is the context, and the timeline reaches the explanation
    assert "BTS-30: Transfer fails" in models.screens[0]
    assert "Ticket: BTS-30: Transfer fails" in models.prompts[0]
    assert "[0:04]" in models.prompts[0] and "[0:09]" in models.prompts[0]


async def test_when_the_explanation_fails_the_screens_are_still_given(tmp_path, models):
    models.narration = RuntimeError("gateway down")
    result = await extract(_three_screens(tmp_path), "screens.mp4")
    assert result.usable and "WHAT THIS VIDEO SHOWS" not in result.text
    assert "[0:04] screen" in result.text and "[0:09] screen" in result.text


async def test_video_with_voice_reads_screens_and_names_the_voice(tmp_path, models):
    data = _make(
        tmp_path / "meeting.mp4",
        "testsrc=duration=20:size=320x240:rate=10",
        "sine=frequency=440:duration=20",
        extra=["-shortest", "-pix_fmt", "yuv420p"],
    )
    result = await extract(data, "meeting.mp4", limits=Limits(max_video_frames=6))
    assert 1 <= len(models.screens) <= 6
    assert result.text.startswith("[Video, 0:20 long, ")
    assert "with spoken audio that was not transcribed" in models.prompts[0]
    assert "Speech-to-text is not available" in result.text


async def test_voice_memo_is_reported_not_pretended(tmp_path, models):
    data = _make(tmp_path / "v.mp3", "sine=frequency=300:duration=3")
    result = await extract(data, "v.mp3")
    assert not result.usable and not models.screens
    assert "speech-to-text is not available" in result.note


async def test_a_broken_video_is_a_note_not_a_crash(models):
    result = await extract(b"definitely not a video", "x.mp4")
    assert not result.usable and result.note == "Could not read this file as audio or video."


async def test_media_has_its_own_bigger_size_cap(models):
    big = b"0" * (11 * 1024 * 1024)  # over the 10 MB document cap, under 100 MB
    doc = await extract(big, "spec.pdf")
    assert "over the" in doc.note
    small_media_cap = await extract(big, "x.mp4", limits=Limits(max_media_bytes=1024))
    assert "over the 1 KB limit" in small_media_cap.note


async def test_videos_are_skipped_when_image_reading_is_off():
    assert not supported("a.mp4", read_images=False)


def test_the_model_picks_its_gateway_provider():
    assert video_understanding.provider_for("gpt-5.1") == "openai"
    assert video_understanding.provider_for("claude-sonnet-5-5") == "anthropic"
    assert video_understanding.provider_for("gemini-2.5-pro") == "google"
