# GENERATED FILE — DO NOT EDIT.
# Mirrored from the canonical shared/ package by scripts/sync_shared.py.
# Edit shared/video.py at the repository root and re-run that script.
"""Frame and audio extraction from video/audio attachments. Pure — no network.

Uses ``imageio-ffmpeg``, which ships a static ffmpeg binary inside the pip
package, so this works on a platform pod without a system ffmpeg install.

Video is reduced to the frames where the screen changes, plus a few evenly
spaced ones (read by the vision model, then explained as one story by
shared/video_understanding.py), plus the audio track. Voice is not
transcribed yet — no speech-to-text model is confirmed on the Aetherion AI
Gateway (2026-10-21) — so the extracted audio is currently unused and a note
says so. Wiring in a transcription call once a model is confirmed is a small
change here, not elsewhere: every caller goes through :func:`extract_video`.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ._logging import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_FRAMES = 12
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)")
_NO_AUDIO_RE = re.compile(
    r"Stream map .* does not contain any stream|Output file does not contain any stream"
)


class VideoReadError(Exception):
    """ffmpeg could not make sense of the file at all."""


@dataclass
class VideoFrame:
    timestamp_seconds: float
    png_bytes: bytes


@dataclass
class VideoExtraction:
    frames: list[VideoFrame] = field(default_factory=list)
    audio_wav: bytes | None = None
    duration_seconds: float | None = None
    audio_present: bool = False


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _probe_duration(path: Path) -> float | None:
    """Best-effort duration in seconds, parsed from ffmpeg's own banner.

    ``imageio-ffmpeg`` bundles ffmpeg, not ffprobe, so this reads the
    "Duration: HH:MM:SS.ss" line ffmpeg prints to stderr for any input,
    which needs no output file and so always runs quickly.
    """
    try:
        proc = subprocess.run(
            [_ffmpeg_exe(), "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return None
    match = _DURATION_RE.search(proc.stderr or "")
    if not match:
        return None
    hours, minutes, seconds, centis = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(centis) / 100


# How much the picture must change to count as a new screen (ffmpeg's scene
# score, 0-1). Low on purpose: in a screen recording a dialog opening or an error
# appearing changes only part of the screen.
SCENE_THRESHOLD = 0.06
_MAX_SCENE_FRAMES = 60
# Evenly spaced frames added on top of the screen changes, so slow changes the
# scene score misses (typing into a field) are still seen.
_EXTRA_EVEN_FRAMES = 4
_PTS_RE = re.compile(r"pts_time:\s*([0-9.]+)")


def _run_frames(path: Path, vf: str, limit: int) -> list[VideoFrame]:
    """Frames ffmpeg selects with filter ``vf``, each with its real timestamp."""
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        cmd = [
            _ffmpeg_exe(),
            "-y",
            "-i",
            str(path),
            "-vf",
            f"{vf},showinfo",
            "-vsync",
            "vfr",
            "-frames:v",
            str(limit),
            str(out_dir / "frame_%04d.png"),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
        stamps = [float(s) for s in _PTS_RE.findall((proc.stderr or b"").decode(errors="replace"))]
        files = sorted(out_dir.glob("frame_*.png"))
        if not files and proc.returncode != 0:
            raise VideoReadError((proc.stderr or b"").decode(errors="replace")[-500:])
        return [
            VideoFrame(
                timestamp_seconds=round(stamps[i] if i < len(stamps) else 0.0, 1),
                png_bytes=f.read_bytes(),
            )
            for i, f in enumerate(files)
        ]


def _spread(frames: list[VideoFrame], count: int) -> list[VideoFrame]:
    """``count`` frames spread evenly across ``frames``, first and last kept."""
    if len(frames) <= count:
        return frames
    if count <= 1:
        return frames[:1]
    step = (len(frames) - 1) / (count - 1)
    return [frames[round(i * step)] for i in range(count)]


def _extract_frames(path: Path, duration: float | None, max_frames: int) -> list[VideoFrame]:
    """The moments worth reading: every change of screen, plus a few even ones.

    A screen recording is mostly still: the same page for ten seconds, then a
    click, a new page, an error. Frames taken every N seconds read the same page
    five times and can miss the error entirely, so frames are taken where the
    picture changes (ffmpeg's scene score), with a few evenly spaced frames added
    so slow changes are not missed. Exact repeats are dropped.
    """
    scene = _run_frames(
        path, f"select='eq(n\\,0)+gt(scene\\,{SCENE_THRESHOLD})'", _MAX_SCENE_FRAMES
    )
    length = duration or float(max_frames)
    interval = max(length / max(_EXTRA_EVEN_FRAMES, 1), 0.5)
    # select= keeps each frame's real time; fps= would show a later picture
    # under an earlier timestamp.
    even = _run_frames(
        path,
        f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval:.3f})'",
        _EXTRA_EVEN_FRAMES,
    )

    # Screen changes first: their timestamps are exact, while an evenly spaced
    # frame can show the next screen under an earlier time.
    seen: set[bytes] = set()
    merged: list[VideoFrame] = []
    for frame in [*scene, *even]:
        digest = frame.png_bytes[-4096:]
        if digest in seen:
            continue
        if any(abs(m.timestamp_seconds - frame.timestamp_seconds) < 0.5 for m in merged):
            continue  # the same moment from both selections
        seen.add(digest)
        merged.append(frame)
    merged.sort(key=lambda f: f.timestamp_seconds)
    return _spread(merged, max_frames)


def _extract_audio(path: Path) -> tuple[bytes | None, bool]:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "audio.wav"
        cmd = [
            _ffmpeg_exe(),
            "-y",
            "-i",
            str(path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        stderr = (proc.stderr or b"").decode(errors="replace")
        if _NO_AUDIO_RE.search(stderr) or not out_path.exists():
            return None, False
        data = out_path.read_bytes()
        return (data if data else None), bool(data)


def extract_video(
    data: bytes, filename: str, *, max_frames: int = DEFAULT_MAX_FRAMES
) -> VideoExtraction:
    """The frames worth reading plus the audio track of one video/audio file.

    Runs ffmpeg synchronously; callers already extract attachments in a worker
    thread (see :mod:`shared.attachments`), which is where this belongs too.
    Never raises for "this file has no audio" or "could not read duration" —
    only for a file ffmpeg cannot open at all.
    """
    suffix = Path(filename).suffix or ".bin"
    with tempfile.TemporaryDirectory() as tmp:
        in_path = Path(tmp) / f"input{suffix}"
        in_path.write_bytes(data)

        duration = _probe_duration(in_path)
        is_audio_only = suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}

        frames: list[VideoFrame] = []
        if not is_audio_only:
            try:
                frames = _extract_frames(in_path, duration, max_frames)
            except VideoReadError:
                raise
            except Exception as exc:  # noqa: BLE001 — frame extraction is best-effort
                logger.warning(f"Could not extract frames from {filename!r}: {exc}")

        try:
            audio_wav, audio_present = _extract_audio(in_path)
        except Exception as exc:  # noqa: BLE001 — audio extraction is best-effort
            logger.warning(f"Could not extract audio from {filename!r}: {exc}")
            audio_wav, audio_present = None, False

        if not frames and not audio_present and duration is None:
            raise VideoReadError(f"ffmpeg could not read {filename!r} as audio or video")

        return VideoExtraction(
            frames=frames,
            audio_wav=audio_wav,
            duration_seconds=duration,
            audio_present=audio_present,
        )
