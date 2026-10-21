"""Frame and audio extraction from video/audio attachments. Pure — no network.

Uses ``imageio-ffmpeg``, which ships a static ffmpeg binary inside the pip
package, so this works on a platform pod without a system ffmpeg install.

Video is reduced to a handful of evenly-spaced frames (read through the same
vision path as an image attachment) plus the audio track. Voice is not
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


def _extract_frames(path: Path, duration: float | None, max_frames: int) -> list[VideoFrame]:
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        duration = duration or float(max_frames)  # unknown duration: still take a spread
        interval = max(duration / max_frames, 0.5)
        pattern = out_dir / "frame_%04d.png"
        cmd = [
            _ffmpeg_exe(),
            "-y",
            "-i",
            str(path),
            "-vf",
            f"fps=1/{interval:.3f}",
            "-vframes",
            str(max_frames),
            str(pattern),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        frames: list[VideoFrame] = []
        for i, frame_path in enumerate(sorted(out_dir.glob("frame_*.png"))):
            frames.append(
                VideoFrame(
                    timestamp_seconds=round(i * interval, 1), png_bytes=frame_path.read_bytes()
                )
            )
        if not frames and proc.returncode != 0:
            raise VideoReadError((proc.stderr or b"").decode(errors="replace")[:500])
        return frames


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
    """Evenly-spaced frames plus the audio track of one video/audio file.

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
