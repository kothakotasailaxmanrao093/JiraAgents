"""Meeting transcripts (.vtt, .srt) as readable lines. Pure — no I/O.

A Teams or Zoom export repeats a cue number and a ``00:01:12.000 --> 00:01:15.500``
line before every sentence, and wraps the speaker in ``<v Priya>`` tags. Read
as plain text, that noise is most of the file and hides who said what. Each cue
becomes one line that keeps when it was said and by whom:

    [00:01:12] Priya: The proof must be under 5 MB.

Consecutive cues from the same named speaker are joined under the first one's
time, and a cue that exactly repeats the same speaker's last one is dropped.
A file with no cues at all is returned unchanged.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

TRANSCRIPT_SUFFIXES = (".vtt", ".srt")

# "00:01:12.000 --> 00:01:15.500 align:start" (.vtt) or "00:01:12,000 --> …" (.srt).
_TIMING = re.compile(r"^\s*((?:\d+:)?\d{1,2}:\d{2})[.,]\d+\s*-->\s*\S+")
_VOICE = re.compile(r"<v(?:\.[^\s>]+)*\s+([^>]+)>")
_TAG = re.compile(r"<[^>]*>")
# Blocks a .vtt may carry that are not speech.
_NOT_SPEECH = ("WEBVTT", "NOTE", "STYLE", "REGION")


@dataclass
class _Line:
    at: str
    speaker: str
    text: str


def is_transcript(filename: str) -> bool:
    return filename.strip().lower().endswith(TRANSCRIPT_SUFFIXES)


def _clock(stamp: str) -> str:
    """ "01:12" or "0:01:12" → "00:01:12"."""
    parts = [int(p) for p in stamp.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return f"{h:02d}:{m:02d}:{s:02d}"


def _speech(lines: list[str]) -> tuple[str, str]:
    """(speaker, text) of one cue's payload."""
    raw = " ".join(line.strip() for line in lines if line.strip())
    voice = _VOICE.search(raw)
    speaker = voice.group(1).strip() if voice else ""
    text = html.unescape(_TAG.sub("", raw)).replace("\xa0", " ")
    return speaker, re.sub(r"\s+", " ", text).strip()


def clean_transcript(text: str) -> str:
    """Timestamped, speaker-named lines — or ``text`` itself when it has no cues."""
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    out: list[_Line] = []
    for block in blocks:
        lines = block.strip("\n").split("\n")
        timing = next((i for i, line in enumerate(lines) if _TIMING.match(line)), None)
        if timing is None or lines[0].strip().startswith(_NOT_SPEECH):
            continue
        speaker, said = _speech(lines[timing + 1 :])
        if not said:
            continue
        at = _clock(_TIMING.match(lines[timing]).group(1))
        last = out[-1] if out else None
        if last and last.speaker == speaker and said == last.text:
            continue  # a caption repeated by the same speaker
        # Joined only under a named speaker: a Zoom export has no <v> tag and
        # writes "Priya: …" in each cue, so those stay one line per cue.
        if last and speaker and last.speaker == speaker:
            last.text = f"{last.text} {said}"
            continue
        out.append(_Line(at, speaker, said))
    if not out:
        return text
    return "\n".join(
        (f"[{line.at}] {line.speaker}: {line.text}" if line.speaker else f"[{line.at}] {line.text}")
        for line in out
    )
