"""Download engine for Video Downloader, built on yt-dlp.

Nothing in this module touches the GUI. The window starts a `DownloadTask` on a
background thread and receives updates through a `Reporter`.
"""

from __future__ import annotations

import functools
import importlib.util
import logging
import os
import re
import shutil
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError

import transcriber

log = logging.getLogger(__name__)

YTDLP_VERSION = yt_dlp.version.__version__

QUALITY_PRESETS = {
    "best": "Best available",
    "1080": "1080p",
    "720": "720p",
    "480": "480p",
    "360": "360p",
    "mp3": "Audio only (MP3)",
    "none": "No media (transcript only)",
}

TRANSCRIPT_MODES = {
    "off": "Off",
    "auto": "Auto (subtitles, then AI)",
    "subtitles": "Subtitles only",
    "whisper": "AI only (Whisper)",
}

WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3", "turbo")

WHISPER_MISSING = (
    "AI transcription needs faster-whisper. Install it with: "
    "pip install -r requirements-whisper.txt"
)

# Single videos are saved as "<title>.<ext>". Playlists get their own folder and
# numbered files, so they stay in order.
SINGLE_TEMPLATE = "%(playlist_index&{:03d} - |)s%(title)s.%(ext)s"
PLAYLIST_TEMPLATE = "%(playlist_title,playlist_id|Playlist)s/%(playlist_index&{:03d} - |)s%(title)s.%(ext)s"

# JavaScript runtimes yt-dlp can use to solve YouTube's challenges, in order of
# preference. Deno and Node run that code in a sandbox; Bun is skipped because it doesn't.
JS_RUNTIMES = {"deno": "deno", "node": "node", "quickjs": "qjs"}

POSTPROCESSOR_MESSAGES = {
    "Merger": "Merging video and audio…",
    "ExtractAudio": "Converting to MP3…",
    "Metadata": "Adding metadata…",
    "EmbedThumbnail": "Adding cover art…",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class Cancelled(DownloadCancelled):
    msg = "Cancelled by user"


class JobError(Exception):
    """An error with a message that can be shown to the user as-is."""


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Environment:
    ffmpeg: str | None
    js_runtimes: dict = field(default_factory=dict)
    whisper: bool = False
    ejs: bool = False


def _extra_search_dirs() -> list[str]:
    """Places where tools often live but which may be missing from PATH.

    Apps started from Finder don't get the shell's PATH on macOS, and pip puts
    the Deno binary from `yt-dlp[deno]` next to the Python scripts.
    """
    dirs = [sysconfig.get_path("scripts"), os.path.dirname(sys.executable)]
    try:
        dirs.append(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))
    except (AttributeError, KeyError):
        pass
    home = os.path.expanduser("~")
    dirs += [os.path.join(home, ".deno", "bin"), os.path.join(home, ".local", "bin")]
    if sys.platform == "darwin":
        dirs += ["/opt/homebrew/bin", "/usr/local/bin"]
    return [d for d in dirs if d]


def find_executable(name: str) -> str | None:
    return shutil.which(name) or shutil.which(name, path=os.pathsep.join(_extra_search_dirs()))


@functools.lru_cache(maxsize=1)
def detect_environment() -> Environment:
    runtimes = {}
    for runtime, executable in JS_RUNTIMES.items():
        path = find_executable(executable)
        if path:
            runtimes[runtime] = path
    return Environment(
        ffmpeg=find_executable("ffmpeg"),
        js_runtimes=runtimes,
        whisper=transcriber.whisper_available(),
        ejs=importlib.util.find_spec("yt_dlp_ejs") is not None,
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def normalize_url(text: str) -> str | None:
    """Return a usable http(s) URL from user input, or None if it isn't one."""
    url = (text or "").strip()
    if not url or any(ch.isspace() for ch in url):
        return None
    if "://" not in url:
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or "." not in parsed.netloc:
        return None
    return url


def media_options(quality: str, has_ffmpeg: bool) -> dict:
    """yt-dlp options for a quality preset.

    Separate video and audio streams (needed for anything above 360p on
    YouTube) can only be combined with ffmpeg, so without it we fall back to
    single-file formats.
    """
    if quality == "none":
        # Nothing is downloaded; any format will do, as long as the video is available.
        return {"format": "ba/b*"}
    if quality == "mp3":
        if not has_ffmpeg:
            return {"format": "ba[ext=m4a]/ba/b"}
        return {
            "format": "ba/b",
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
        }
    resolution = "res" if quality == "best" else f"res:{quality}"
    # Highest resolution first; at equal resolution prefer H.264/AAC, which plays everywhere.
    sort = [resolution, "+codec:avc:m4a"]
    if has_ffmpeg:
        return {"format": "bv*+ba/b", "format_sort": sort, "merge_output_format": "mp4"}
    return {"format": "b", "format_sort": sort}


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def friendly_error(message: str, has_ffmpeg: bool = True) -> str:
    """Turn a yt-dlp error into a short message for the user."""
    text = strip_ansi(message or "").strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    text = re.sub(r"^\[[^\]]+\]\s*(?:[\w-]+:\s+)?", "", text)
    text = text.splitlines()[0].strip() if text else ""
    text = re.sub(r"\s*\(caused by .*\)$", "", text)
    # Drop hints about yt-dlp command-line options, which don't apply here.
    text = re.split(r"\s+(?=Use --|See\s+https?://|Also see\s|Please report this issue)", text)[0]
    lowered = text.lower()
    if "unsupported url" in lowered:
        return "This link isn't supported. Make sure it points to a video or playlist page."
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return ("YouTube asked to confirm you're not a bot. Wait a few minutes and try again, "
                "or try from another network.")
    if "http error 403" in lowered:
        return (f"The site refused the download (HTTP 403). This usually means yt-dlp is out of date "
                f"(installed: {YTDLP_VERSION}). Update it with: "
                'pip install -U "yt-dlp[default,curl-cffi,deno]"')
    if "requested format is not available" in lowered and not has_ffmpeg:
        return ("This video only has separate audio and video streams, which need ffmpeg to be "
                "combined. Install ffmpeg and try again.")
    network_hints = ("getaddrinfo", "nodename nor servname", "name or service not known",
                     "failed to resolve", "network is unreachable", "timed out")
    if any(hint in lowered for hint in network_hints):
        return "Network error. Check your internet connection and try again."
    return text or "The download failed for an unknown reason."


def format_bytes(size: float | None) -> str:
    if size is None:
        return "?"
    units = ("B", "KB", "MB", "GB", "TB")
    value, unit = float(size), 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    return f"{value:.0f} {units[unit]}" if unit == 0 else f"{value:.1f} {units[unit]}"


def format_duration(seconds: float) -> str:
    hours, rest = divmod(int(round(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def describe_progress(done: float, total: float | None, speed: float | None, eta: float | None) -> str:
    parts = []
    if total:
        parts += [f"{min(done / total, 1.0):.0%}", f"{format_bytes(done)} of {format_bytes(total)}"]
    elif done:
        parts.append(format_bytes(done))
    if speed:
        parts.append(f"{format_bytes(speed)}/s")
    if eta is not None and total:
        parts.append(f"{format_duration(eta)} left")
    return " · ".join(parts)


def stream_kind(info: dict) -> str:
    if info.get("vcodec") == "none":
        return "audio"
    if info.get("acodec") == "none":
        return "video"
    return ""


def iter_videos(info: dict | None):
    """Yield every video in a result, flattening playlists. Failed entries are yielded as None."""
    if info and info.get("_type") in ("playlist", "multi_video"):
        for entry in info.get("entries") or []:
            yield from iter_videos(entry)
    else:
        yield info


def downloaded_files(video: dict | None) -> list[str]:
    paths: list[str] = []
    for item in (video or {}).get("requested_downloads") or []:
        path = item.get("filepath")
        if path and path not in paths and os.path.exists(path):
            paths.append(path)
    return paths


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

@dataclass
class DownloadJob:
    url: str
    output_dir: str
    quality: str = "best"
    transcript: str = "off"
    whisper_model: str = "small"
    playlist: bool = False


@dataclass
class JobResult:
    files: list[str] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Reporter:
    """Receives updates from a running task. Called from the worker thread."""

    def status(self, text: str) -> None:
        pass

    def title(self, title: str, subtitle: str = "") -> None:
        pass

    def progress(self, fraction: float | None, detail: str = "") -> None:
        """`fraction` is between 0 and 1, or None when progress is unknown."""


class _YtDlpLogger:
    def __init__(self):
        self.errors: list[str] = []

    def debug(self, msg):
        log.debug(msg)

    info = debug

    def warning(self, msg):
        log.warning(strip_ansi(msg))

    def error(self, msg):
        msg = strip_ansi(msg)
        log.error(msg)
        self.errors.append(msg)


class DownloadTask:
    """Downloads one link (video or playlist) and optionally writes transcripts."""

    def __init__(self, job: DownloadJob, reporter: Reporter | None = None, env: Environment | None = None):
        self.job = job
        self.reporter = reporter or Reporter()
        self.env = env or detect_environment()
        self.notes: list[str] = []
        self._cancel = threading.Event()
        self._logger = _YtDlpLogger()
        self._tempdir = ""
        self._mute_hooks = False
        self._last_update = 0.0
        self._current_id = None

    # -- cancellation ------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    # -- main flow -----------------------------------------------------------

    def run(self) -> JobResult:
        job = self.job
        if job.quality == "none" and job.transcript == "off":
            raise JobError("Nothing to do: pick a format to download or turn on transcripts.")
        if job.transcript == "whisper" and not self.env.whisper:
            raise JobError(WHISPER_MISSING)
        try:
            os.makedirs(job.output_dir, exist_ok=True)
        except OSError as exc:
            raise JobError(f"Can't use the download folder: {exc.strerror or exc}") from exc

        self._tempdir = tempfile.mkdtemp(prefix="videodownloader-")
        try:
            return self._run()
        except DownloadError as exc:
            if self.cancelled:
                raise Cancelled() from exc
            raise JobError(friendly_error(str(exc), self.env.ffmpeg is not None)) from exc
        except OSError as exc:
            raise JobError(f"File error: {exc}") from exc
        finally:
            shutil.rmtree(self._tempdir, ignore_errors=True)

    def _run(self) -> JobResult:
        job = self.job
        download_media = job.quality != "none"
        self.reporter.status("Fetching video info…")
        self.reporter.progress(None)
        options = {**self._base_options(), **media_options(job.quality, self.env.ffmpeg is not None)}

        with yt_dlp.YoutubeDL(options) as ydl:
            info = self._extract(ydl, job.url)
            self.check_cancel()
            is_playlist = info.get("_type") in ("playlist", "multi_video")
            if is_playlist:
                # yt-dlp reads the template when it names each file, so playlists can get their own folder.
                ydl.params["outtmpl"]["default"] = PLAYLIST_TEMPLATE
                self.reporter.title(info.get("title") or "Playlist", "Playlist")
            else:
                self._announce(info)
            result = ydl.process_ie_result(info, download=download_media)
            self.check_cancel()

            entries = list(iter_videos(result))
            videos = [v for v in entries if v and (downloaded_files(v) or not download_media)]
            if not videos:
                if is_playlist and not entries:
                    raise JobError("This playlist is empty, or none of its videos are available.")
                raise JobError(self._last_error(
                    "Nothing could be downloaded from this link." if download_media
                    else "No videos were found at this link."))
            if len(videos) < len(entries):
                reason = self._last_error("they aren't available.")
                self.notes.append(f"Skipped {len(entries) - len(videos)} of {len(entries)} videos. Last error: {reason}")

            outcome = JobResult()
            for video in videos:
                outcome.files.extend(downloaded_files(video))

            if job.transcript != "off":
                for index, video in enumerate(videos, 1):
                    self.check_cancel()
                    media = next(iter(downloaded_files(video)), None)
                    outcome.transcripts += self._transcribe(ydl, video, media, index, len(videos))

        outcome.notes = self.notes
        return outcome

    def _extract(self, ydl, url: str) -> dict:
        """Extract a link without processing it yet, so we can tell whether it is a playlist.

        Plain redirects (e.g. a video inside a playlist pointing to the playlist) are followed.
        """
        info = ydl.extract_info(url, download=False, process=False)
        for _ in range(5):
            if not info or info.get("_type") != "url":
                break
            info = ydl.extract_info(info["url"], download=False, process=False, ie_key=info.get("ie_key"))
        if not info:
            raise JobError(self._last_error("Couldn't read this link."))
        return info

    def _base_options(self) -> dict:
        options = {
            "quiet": True,
            "noprogress": True,
            "logger": self._logger,
            "progress_hooks": [self._on_progress],
            "postprocessor_hooks": [self._on_postprocess],
            "match_filter": self._match_filter,
            "noplaylist": not self.job.playlist,
            # Errors are collected by the logger, so one broken playlist entry
            # doesn't stop the rest.
            "ignoreerrors": True,
            # Partial files stay in a private temp folder and only finished
            # files are moved to the download folder.
            "paths": {"home": self.job.output_dir, "temp": os.path.join(self._tempdir, "parts")},
            "outtmpl": SINGLE_TEMPLATE,
        }
        if self.env.ffmpeg:
            options["ffmpeg_location"] = self.env.ffmpeg
        if self.env.js_runtimes:
            options["js_runtimes"] = {name: {"path": path} for name, path in self.env.js_runtimes.items()}
        if not self.env.ejs:
            # Without the yt-dlp-ejs package, let yt-dlp fetch its official
            # challenge solver from GitHub so YouTube keeps working.
            options["remote_components"] = ["ejs:github"]
        return options

    def _last_error(self, fallback: str) -> str:
        if self._logger.errors:
            return friendly_error(self._logger.errors[-1], self.env.ffmpeg is not None)
        return fallback

    # -- transcripts ---------------------------------------------------------

    def _transcribe(self, ydl, video: dict, media: str | None, index: int, total: int) -> list[str]:
        title = video.get("title") or video.get("id") or "video"
        label = f" ({index}/{total})" if total > 1 else ""
        self._announce(video)
        mode = self.job.transcript
        transcript = None

        if mode in ("auto", "subtitles"):
            self.reporter.status(f"Looking for subtitles{label}…")
            self.reporter.progress(None)
            transcript = self._fetch_subtitles(ydl, video)
            if transcript is None and mode == "subtitles":
                self.notes.append(f"No subtitles found for “{title}”.")
                return []
            if transcript is None and not self.env.whisper:
                self.notes.append(f"No subtitles found for “{title}”. Install faster-whisper "
                                  "to transcribe videos without subtitles (see README).")
                return []

        if transcript is None:
            try:
                audio = media or self._download_audio(video, label)
                transcript = self._run_whisper(audio, label)
            except Cancelled:
                raise
            except Exception as exc:
                log.exception("Transcription failed")
                message = str(exc) if isinstance(exc, JobError) else f"{type(exc).__name__}: {exc}"
                self.notes.append(f"Couldn't transcribe “{title}”: {message}")
                return []

        if not transcript.segments:
            self.notes.append(f"No speech was found in “{title}”.")
            return []
        base = os.path.splitext(media or ydl.prepare_filename(video))[0]
        return transcriber.write_transcript(base, transcript, title=title, url=video.get("webpage_url"))

    def _fetch_subtitles(self, ydl, video: dict):
        self._mute_hooks = True
        try:
            return transcriber.fetch_subtitles(ydl, video, self._tempdir)
        except Cancelled:
            raise
        except Exception as exc:
            log.warning("Couldn't fetch subtitles: %s", exc)
            return None
        finally:
            self._mute_hooks = False

    def _download_audio(self, video: dict, label: str) -> str:
        self.reporter.status(f"Downloading audio for transcription{label}…")
        url = video.get("webpage_url") or video.get("original_url") or self.job.url
        options = {
            **self._base_options(),
            "format": "ba/b",
            "noplaylist": True,
            "outtmpl": "%(id)s.%(ext)s",
            "paths": {"home": os.path.join(self._tempdir, "audio"), "temp": os.path.join(self._tempdir, "parts")},
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.extract_info(url, download=True)
        files = downloaded_files(result)
        if not files:
            raise JobError(self._last_error("the audio couldn't be downloaded."))
        return files[0]

    def _run_whisper(self, path: str, label: str):
        name = self.job.whisper_model
        self.reporter.status(f"Loading the Whisper “{name}” model (downloaded on first use)…")
        self.reporter.progress(None)
        model = transcriber.load_whisper_model(name)
        self.check_cancel()
        self.reporter.status(f"Transcribing with Whisper{label}…")

        def on_progress(fraction, position, duration):
            detail = f"{fraction:.0%} · {format_duration(position)} of {format_duration(duration)}"
            self.reporter.progress(fraction, detail)

        return transcriber.transcribe_audio(
            model, path, name, ffmpeg=self.env.ffmpeg, on_progress=on_progress, check_cancel=self.check_cancel,
        )

    # -- yt-dlp callbacks ----------------------------------------------------

    def _announce(self, info: dict) -> None:
        """Show which video is being worked on (once per video)."""
        video_id = info.get("id")
        if not video_id or video_id == self._current_id:
            return
        self._current_id = video_id
        title = info.get("title") or video_id
        index, count = info.get("playlist_index"), info.get("n_entries") or info.get("playlist_count")
        if index and count:
            playlist = info.get("playlist_title") or info.get("playlist") or "Playlist"
            self.reporter.title(playlist, f"Video {index} of {count} · {title}")
        else:
            details = [info.get("uploader") or info.get("channel")]
            if info.get("duration"):
                details.append(format_duration(info["duration"]))
            self.reporter.title(title, " · ".join(d for d in details if d))

    def _item_label(self, info: dict) -> str:
        index, count = info.get("playlist_index"), info.get("n_entries") or info.get("playlist_count")
        return f" ({index}/{count})" if index and count else ""

    def _match_filter(self, info, *, incomplete=False):
        self.check_cancel()
        if not incomplete:
            self._announce(info)
        return None

    def _on_progress(self, d: dict) -> None:
        self.check_cancel()
        if self._mute_hooks:
            return
        info = d.get("info_dict") or {}
        if d.get("status") == "finished":
            self.reporter.progress(1.0, "")
            return
        if d.get("status") != "downloading":
            return
        now = time.monotonic()
        if now - self._last_update < 0.1:
            return
        self._last_update = now
        self._announce(info)

        done = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        fraction = done / total if total else None
        if fraction is None and d.get("fragment_count"):
            fraction = (d.get("fragment_index") or 0) / d["fragment_count"]
        kind = stream_kind(info)
        self.reporter.status(f"Downloading{' ' + kind if kind else ''}…{self._item_label(info)}")
        self.reporter.progress(
            min(fraction, 1.0) if fraction is not None else None,
            describe_progress(done, total, d.get("speed"), d.get("eta")),
        )

    def _on_postprocess(self, d: dict) -> None:
        self.check_cancel()
        if d.get("status") != "started":
            return
        name = d.get("postprocessor") or ""
        message = POSTPROCESSOR_MESSAGES.get(name) or ("Finishing up…" if name.startswith("Fixup") else None)
        if message:
            self.reporter.status(message + self._item_label(d.get("info_dict") or {}))
            self.reporter.progress(None)
