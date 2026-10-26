"""Transcripts for downloaded videos.

Two sources are supported:

* Subtitles published with the video (uploaded by the creator or generated
  automatically by the site). They are fetched through yt-dlp, which is fast
  and needs no extra dependencies.
* Local speech recognition with faster-whisper (optional dependency), for
  videos without subtitles or when the user explicitly asks for it.

Both sources produce timed segments that are saved as an .srt subtitle file
and as a readable .txt transcript.
"""

from __future__ import annotations

import html
import importlib.util
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

# Subtitle formats we can parse, in order of preference.
SUBTITLE_FORMATS = ("json3", "vtt", "srt")

SENTENCE_ENDINGS = (".", "!", "?", "…", "。", "！", "？")


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    segments: list[Segment]
    source: str
    language: str | None = None


# --------------------------------------------------------------------------
# Subtitle selection
# --------------------------------------------------------------------------

def _match_language(tracks: dict, code: str | None) -> str | None:
    """Find a track for `code`: exact match first, then the same base language."""
    if not code:
        return None
    code = code.lower()
    for key in tracks:
        if key.lower() == code:
            return key
    base = code.split("-")[0]
    for key in tracks:
        if key.lower().split("-")[0] == base:
            return key
    return None


def pick_subtitle_track(info: dict) -> tuple[str, list, bool] | None:
    """Choose the subtitle track that best represents what is said in the video.

    Returns ``(language, formats, is_automatic)`` or ``None``. Preference order:
    uploaded subtitles in the spoken language, automatic captions in the
    original language, then any uploaded subtitles (English first).
    """
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if v and k != "live_chat"}
    automatic = {k: v for k, v in (info.get("automatic_captions") or {}).items() if v}
    spoken = info.get("language")

    key = _match_language(manual, spoken)
    if key:
        return key, manual[key], False

    # YouTube marks the untranslated speech-recognition track with "-orig".
    original = [k for k in automatic if k.endswith("-orig")]
    if original:
        return original[0], automatic[original[0]], True
    key = _match_language(automatic, spoken)
    if key:
        return key, automatic[key], True
    if len(automatic) == 1:
        key = next(iter(automatic))
        return key, automatic[key], True

    if manual:
        key = _match_language(manual, "en") or next(iter(manual))
        return key, manual[key], False
    return None


def choose_subtitle_format(formats: list) -> dict | None:
    by_ext: dict[str, dict] = {}
    for fmt in formats:
        by_ext.setdefault(fmt.get("ext"), fmt)
    for ext in SUBTITLE_FORMATS:
        if ext in by_ext:
            return by_ext[ext]
    return None


# --------------------------------------------------------------------------
# Subtitle parsing
# --------------------------------------------------------------------------

_TIMESTAMP = r"(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}"
_CUE_TIMING_RE = re.compile(rf"({_TIMESTAMP})\s*-->\s*({_TIMESTAMP})")
_TAG_RE = re.compile(r"<[^>]*>")
_ASS_TAG_RE = re.compile(r"\{\\[^}]*\}")


def _parse_timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2]) if len(parts) > 1 else 0
    hours = int(parts[-3]) if len(parts) > 2 else 0
    return hours * 3600 + minutes * 60 + seconds


def _clean_line(line: str) -> str:
    line = _TAG_RE.sub("", line)
    line = _ASS_TAG_RE.sub("", line)
    return " ".join(html.unescape(line).split())


def parse_timed_text(text: str) -> list[Segment]:
    """Parse WebVTT or SRT subtitles.

    Automatic captions on YouTube "roll": every cue repeats the previous line
    before adding a new one. Lines already shown in the previous cue are
    dropped so each sentence appears only once.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").split("\n")
    segments: list[Segment] = []
    previous: list[str] = []
    i = 0
    while i < len(lines):
        match = _CUE_TIMING_RE.search(lines[i])
        i += 1
        if not match:
            continue
        # A cue's text runs until an empty line. Whitespace-only lines are part
        # of the cue (YouTube uses them), so they don't end it.
        payload = []
        while i < len(lines) and lines[i] and not _CUE_TIMING_RE.search(lines[i]):
            payload.append(lines[i])
            i += 1
        if i < len(lines) and lines[i] and payload and payload[-1].strip().isdigit():
            payload.pop()  # number of the next cue, in files without blank lines between cues
        cue_lines = [cleaned for cleaned in map(_clean_line, payload) if cleaned]
        if not cue_lines:
            continue
        new_lines = [line for line in cue_lines if line not in previous]
        previous = cue_lines
        if new_lines:
            segments.append(Segment(
                _parse_timestamp(match.group(1)),
                _parse_timestamp(match.group(2)),
                " ".join(new_lines),
            ))
    return segments


def parse_json3(text: str) -> list[Segment]:
    """Parse YouTube's json3 subtitle format."""
    data = json.loads(text)
    segments: list[Segment] = []
    for event in data.get("events") or []:
        pieces = event.get("segs")
        if not pieces:
            continue
        line = " ".join(html.unescape("".join(p.get("utf8", "") for p in pieces)).split())
        if not line:
            continue
        start = event.get("tStartMs", 0) / 1000
        end = start + event.get("dDurationMs", 0) / 1000
        if segments and segments[-1].text == line:
            segments[-1].end = max(segments[-1].end, end)
            continue
        segments.append(Segment(start, end, line))
    return segments


def parse_subtitles(text: str, ext: str) -> list[Segment]:
    if ext == "json3":
        return parse_json3(text)
    return parse_timed_text(text)


def fetch_subtitles(ydl, info: dict, workdir: str) -> Transcript | None:
    """Download and parse the best subtitle track of a video, if it has one."""
    track = pick_subtitle_track(info)
    if not track:
        return None
    language, formats, automatic = track
    fmt = choose_subtitle_format(formats)
    if not fmt:
        return None

    if fmt.get("data") is not None:
        text = fmt["data"]
    else:
        path = os.path.join(workdir, f"{info.get('id', 'video')}.{language}.{fmt['ext']}")
        request = dict(fmt)
        request.setdefault("http_headers", info.get("http_headers"))
        ydl.dl(path, request, subtitle=True)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()

    segments = parse_subtitles(text, fmt["ext"])
    if not segments:
        return None
    kind = "automatic captions" if automatic else "uploaded subtitles"
    name = fmt.get("name") or language
    return Transcript(segments, f"{kind} ({name})", language.split("-")[0])


# --------------------------------------------------------------------------
# Speech recognition (optional)
# --------------------------------------------------------------------------

_models: dict = {}
_models_lock = threading.Lock()


def whisper_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def load_whisper_model(name: str):
    """Load (and on first use download) a faster-whisper model. Models are cached.

    8-bit models on the CPU are fast enough for this use and work on every
    machine, unlike CUDA, which also needs the right cuDNN libraries.
    """
    from faster_whisper import WhisperModel

    with _models_lock:
        if name not in _models:
            _models[name] = WhisperModel(name, device="cpu", compute_type="int8")
        return _models[name]


def load_audio(path: str, ffmpeg: str | None):
    """Decode a media file to the 16 kHz mono samples Whisper expects.

    ffmpeg handles every container yt-dlp produces and avoids depending on the
    exact PyAV version. Without ffmpeg the path is returned unchanged and
    faster-whisper decodes it itself.
    """
    if not ffmpeg:
        return path
    import numpy as np

    command = [ffmpeg, "-nostdin", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    # CREATE_NO_WINDOW stops a console window from flashing up on Windows.
    result = subprocess.run(command, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "ffmpeg couldn't read the audio")
    return np.frombuffer(result.stdout, np.int16).astype(np.float32) / 32768.0


def transcribe_audio(
    model,
    path: str,
    model_name: str,
    ffmpeg: str | None = None,
    on_progress: Callable[[float, float, float], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> Transcript:
    """Transcribe an audio or video file. `check_cancel` may raise to stop early."""
    pieces, info = model.transcribe(load_audio(path, ffmpeg), vad_filter=True)
    duration = info.duration or 0.0
    segments: list[Segment] = []
    for piece in pieces:
        if check_cancel:
            check_cancel()
        text = piece.text.strip()
        if text:
            segments.append(Segment(piece.start, piece.end, text))
        if on_progress and duration > 0:
            on_progress(min(piece.end / duration, 1.0), piece.end, duration)
    source = f"Whisper speech recognition ({model_name} model, language: {info.language})"
    return Transcript(segments, source, info.language)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def normalize_segments(segments: list[Segment]) -> list[Segment]:
    """Sort segments, drop empty ones and trim overlaps so cues play one after another."""
    items = sorted((s for s in segments if s.text.strip()), key=lambda s: s.start)
    result: list[Segment] = []
    for index, seg in enumerate(items):
        next_start = items[index + 1].start if index + 1 < len(items) else None
        end = seg.end
        if end <= seg.start:
            end = next_start if next_start and next_start > seg.start else seg.start + 2.0
        if next_start is not None and seg.start < next_start < end:
            end = next_start
        result.append(Segment(seg.start, end, seg.text.strip()))
    return result


def format_srt_time(seconds: float) -> str:
    millis = int(round(max(seconds, 0.0) * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_clock(seconds: float, with_hours: bool = False) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if with_hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes + hours * 60:02d}:{secs:02d}"


def to_srt(segments: list[Segment]) -> str:
    blocks = [
        f"{i}\n{format_srt_time(s.start)} --> {format_srt_time(s.end)}\n{s.text}\n"
        for i, s in enumerate(normalize_segments(segments), 1)
    ]
    return "\n".join(blocks)


def build_paragraphs(segments: list[Segment], pause: float = 2.0, size: int = 600) -> list[tuple[float, str]]:
    """Group segments into paragraphs at long pauses or after ~`size` characters."""
    paragraphs: list[tuple[float, str]] = []
    current: list[str] = []
    start = last_end = 0.0
    length = 0
    for seg in segments:
        if current:
            sentence_done = current[-1].endswith(SENTENCE_ENDINGS)
            if seg.start - last_end >= pause or (length >= size and sentence_done) or length >= size * 2:
                paragraphs.append((start, " ".join(current)))
                current, length = [], 0
        if not current:
            start = seg.start
        current.append(seg.text)
        length += len(seg.text) + 1
        last_end = seg.end
    if current:
        paragraphs.append((start, " ".join(current)))
    return paragraphs


def to_text(transcript: Transcript, title: str | None = None, url: str | None = None) -> str:
    segments = normalize_segments(transcript.segments)
    with_hours = bool(segments) and segments[-1].end >= 3600
    lines = [line for line in (title, url) if line]
    lines += [f"Transcript source: {transcript.source}", ""]
    for start, text in build_paragraphs(segments):
        lines += [f"[{format_clock(start, with_hours)}] {text}", ""]
    return "\n".join(lines).rstrip() + "\n"


def write_transcript(base_path: str, transcript: Transcript, title: str | None = None, url: str | None = None) -> list[str]:
    """Write `<base_path>.txt` and `<base_path>.srt`; returns the written paths."""
    folder = os.path.dirname(base_path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    txt_path, srt_path = base_path + ".txt", base_path + ".srt"
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(to_text(transcript, title, url))
    with open(srt_path, "w", encoding="utf-8") as fh:
        fh.write(to_srt(transcript.segments))
    return [txt_path, srt_path]
