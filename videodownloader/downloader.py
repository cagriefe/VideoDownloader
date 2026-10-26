"""Download engine for Video Downloader, built on yt-dlp.

Nothing in this module touches the GUI. The window (and the command line)
start a `DownloadTask` and receive updates through a `Reporter`.
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
from yt_dlp.utils import DownloadCancelled, DownloadError, download_range_func

from . import summarizer, transcriber

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

# Browsers yt-dlp can read cookies from (for age-restricted or members-only videos).
BROWSERS = ("chrome", "firefox", "safari", "edge", "brave", "opera", "vivaldi", "chromium")

# SponsorBlock categories that are cut out: paid sponsors, self-promotion and "like and subscribe" reminders.
SPONSOR_CATEGORIES = ("sponsor", "selfpromo", "interaction")

WHISPER_MISSING = (
    "AI transcription needs faster-whisper. Install it with: "
    "pip install -r requirements-whisper.txt"
)

# JavaScript runtimes yt-dlp can use to solve YouTube's challenges, in order of
# preference. Deno and Node run that code in a sandbox; Bun is skipped because it doesn't.
JS_RUNTIMES = {"deno": "deno", "node": "node", "quickjs": "qjs"}

POSTPROCESSOR_MESSAGES = {
    "Merger": "Merging video and audio…",
    "ExtractAudio": "Converting to MP3…",
    "SponsorBlock": "Looking up sponsor segments…",
    "ModifyChapters": "Cutting out sponsor segments…",
    "Metadata": "Adding chapters and metadata…",
    "EmbedThumbnail": "Adding cover art…",
    "SplitChapters": "Splitting into chapters…",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_HOST_RE = re.compile(r"(?:[\w-]+\.)+[^\W\d_]{2,}(?::\d+)?|localhost(?::\d+)?|\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?")


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
# Links, times and search
# --------------------------------------------------------------------------

def normalize_url(text: str) -> str | None:
    """Return a usable http(s) URL from user input, or None if it isn't one."""
    url = (text or "").strip()
    if not url or any(ch.isspace() for ch in url):
        return None
    if "://" not in url:
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.rsplit("@", 1)[-1].lower()
    if parsed.scheme.lower() not in ("http", "https") or not _HOST_RE.fullmatch(host):
        return None
    return url


def extract_urls(text: str) -> list[str]:
    """All links in a piece of text (one per line, or separated by spaces)."""
    urls: list[str] = []
    for word in (text or "").split():
        url = normalize_url(word.strip("<>\"'(),"))
        if url and url not in urls:
            urls.append(url)
    return urls


def parse_time(text: str) -> float | None:
    """Parse "90", "1:30", "1:02:03" or "1m30s" into seconds. Empty text gives None."""
    text = (text or "").strip().lower().replace(" ", "")
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?(?::\d+(?:\.\d+)?){0,2}", text):
        parts = [float(p) for p in text.split(":")]
        if any(p >= 60 for p in parts[1:]):
            raise ValueError(f"“{text}” isn't a valid time.")
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", text)
    if match and any(match.groups()):
        hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"“{text}” isn't a time. Use for example 1:30, 90 or 1m30s.")


def parse_clip(start_text: str, end_text: str) -> tuple[float, float | None] | None:
    """Turn "from" and "to" times into a clip, or None for the whole video."""
    start, end = parse_time(start_text), parse_time(end_text)
    if start is None and end is None:
        return None
    start = start or 0.0
    if end is not None and end <= start:
        raise ValueError("The clip has to end after it starts.")
    return start, end


def _short_time(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def clip_label(start: float, end: float | None) -> str:
    return f"{_short_time(start)}-{_short_time(end) if end is not None else 'end'}"


def search_youtube(query: str, limit: int = 20, cookies_browser: str = "") -> list[dict]:
    """Search YouTube. Returns dicts with url, title, channel, duration and views."""
    options = {"quiet": True, "skip_download": True, "extract_flat": "in_playlist", "logger": _YtDlpLogger()}
    if cookies_browser:
        options["cookiesfrombrowser"] = (cookies_browser,)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    except DownloadError as exc:
        raise JobError(friendly_error(str(exc))) from exc
    results = []
    for entry in (info or {}).get("entries") or []:
        if entry and entry.get("id"):
            channel = (entry.get("channel") or entry.get("uploader") or "").strip()
            results.append({
                "url": entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}",
                "title": entry.get("title") or entry["id"],
                # Search results sometimes put "Name • 1M views • …" into the channel field.
                "channel": channel.splitlines()[0].split(" • ")[0].strip() if channel else "",
                "duration": entry.get("duration"),
                "views": entry.get("view_count"),
            })
    return results


# --------------------------------------------------------------------------
# yt-dlp options
# --------------------------------------------------------------------------

def media_options(quality: str, has_ffmpeg: bool) -> dict:
    """yt-dlp format selection for a quality preset.

    Separate video and audio streams (needed for anything above 360p on
    YouTube) can only be combined with ffmpeg, so without it we fall back to
    single-file formats.
    """
    if quality == "none":
        # Nothing is downloaded; any format will do, as long as the video is available.
        return {"format": "ba/b*"}
    if quality == "mp3":
        return {"format": "ba/b" if has_ffmpeg else "ba[ext=m4a]/ba/b"}
    resolution = "res" if quality == "best" else f"res:{quality}"
    # Highest resolution first; at equal resolution prefer H.264/AAC, which plays everywhere.
    sort = [resolution, "+codec:avc:m4a"]
    if has_ffmpeg:
        return {"format": "bv*+ba/b", "format_sort": sort, "merge_output_format": "mp4"}
    return {"format": "b", "format_sort": sort}


def postprocessors(job: DownloadJob, has_ffmpeg: bool) -> list[dict]:
    """The processing steps after a download, in the order yt-dlp's own command line uses."""
    if not has_ffmpeg or job.quality == "none":
        return []
    sponsors = job.remove_sponsors and not job.clip
    steps = []
    if sponsors:
        steps.append({"key": "SponsorBlock", "categories": list(SPONSOR_CATEGORIES), "when": "after_filter"})
    if job.quality == "mp3":
        steps.append({"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"})
    if sponsors:
        steps.append({"key": "ModifyChapters", "remove_sponsor_segments": list(SPONSOR_CATEGORIES)})
    # Chapters let players jump between parts; title and artist tags matter for music.
    steps.append({"key": "FFmpegMetadata", "add_chapters": True, "add_metadata": job.quality == "mp3"})
    if job.quality == "mp3":
        steps.append({"key": "EmbedThumbnail"})
    if job.split_chapters and not job.clip:
        steps.append({"key": "FFmpegSplitChapters"})
    return steps


def clip_options(clip: tuple[float, float | None]) -> dict:
    start, end = clip
    # Exact cuts need re-encoding around the cut points; otherwise they snap to keyframes.
    return {
        "download_ranges": download_range_func(None, [(start, float("inf") if end is None else end)]),
        "force_keyframes_at_cuts": True,
    }


def output_templates(job: DownloadJob, playlist: bool) -> dict:
    """File names: "<title>.<ext>"; playlists get their own folder with numbered files.

    Chapters (when splitting) go into a folder named after the video.
    """
    name = "%(playlist_index&{:03d} - |)s%(title)s"
    if job.clip:
        name += f" [{clip_label(*job.clip)}]"
    folder = "%(playlist_title,playlist_id|Playlist)s/" if playlist else ""
    return {
        "default": f"{folder}{name}.%(ext)s",
        "chapter": f"{folder}{name}/%(section_number)02d - %(section_title)s.%(ext)s",
    }


# --------------------------------------------------------------------------
# Messages and formatting
# --------------------------------------------------------------------------

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
    if "confirm your age" in lowered or "age-restricted" in lowered:
        return "This video is age-restricted. Choose your browser under Settings → Browser cookies and try again."
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return ("YouTube asked to confirm you're not a bot. Wait a few minutes and try again, "
                "or use your browser's cookies (Settings).")
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


def format_count(count: int | None) -> str:
    if count is None:
        return ""
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if count >= limit:
            return f"{count / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return str(count)


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


def chapter_files(video: dict | None) -> list[str]:
    """Files created by splitting a video into chapters.

    yt-dlp removes keys from each requested download that equal the video's own,
    so the chapters (with their new file paths) may only be on the video itself.
    """
    paths: list[str] = []
    video = video or {}
    for item in [video, *(video.get("requested_downloads") or [])]:
        for chapter in item.get("chapters") or []:
            path = chapter.get("filepath")
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
    language: str = ""                                # transcript language; "" = as spoken
    whisper_model: str = "small"
    playlist: bool = False
    clip: tuple[float, float | None] | None = None    # (start, end) in seconds; end None = to the end
    remove_sponsors: bool = False
    split_chapters: bool = False
    embed_subtitles: bool = False
    summary: bool = False
    ai: summarizer.AIConfig | None = None
    cookies_browser: str = ""
    archive_file: str = ""                             # remember downloads here and skip them next time


@dataclass
class JobResult:
    title: str = ""
    files: list[str] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def outputs(self) -> list[str]:
        return self.files + self.transcripts + self.summaries


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
        self.archived: set[str] = set()  # videos skipped because they were downloaded before

    def debug(self, msg):
        log.debug(msg)
        if "has already been recorded in the archive" in msg:
            match = re.search(r"\[download\] ([\w-]+):", strip_ansi(msg))
            self.archived.add(match.group(1) if match else msg)

    info = debug

    def warning(self, msg):
        log.warning(strip_ansi(msg))

    def error(self, msg):
        msg = strip_ansi(msg)
        log.error(msg)
        self.errors.append(msg)


class DownloadTask:
    """Downloads one link (video or playlist) and creates transcripts and summaries for it."""

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
        # A summary or embedded subtitles need a transcript, even if none was asked for.
        if job.transcript == "off" and (job.summary or job.embed_subtitles):
            self.transcript_mode = "auto"
        else:
            self.transcript_mode = job.transcript

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
        if job.quality == "none" and self.transcript_mode == "off":
            raise JobError("Nothing to do: pick a format to download or turn on transcripts.")
        if self.transcript_mode == "whisper" and not self.env.whisper:
            raise JobError(WHISPER_MISSING)
        if job.clip and not self.env.ffmpeg:
            raise JobError("Downloading a clip needs ffmpeg. Install ffmpeg and try again.")
        if not self.env.ffmpeg and job.quality != "none" and (job.remove_sponsors or job.split_chapters):
            self.notes.append("Removing sponsors and splitting chapters need ffmpeg, so they were skipped.")
        if job.clip and (job.remove_sponsors or job.split_chapters):
            self.notes.append("Sponsor removal and chapter splitting don't apply to clips, so they were skipped.")
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
        outcome = JobResult()
        self.reporter.status("Fetching video info…")
        self.reporter.progress(None)

        with yt_dlp.YoutubeDL(self._download_options()) as ydl:
            info = self._extract(ydl, job.url)
            self.check_cancel()
            if info is None:
                self.notes.append("You downloaded this video before, so it was skipped "
                                  "(see Settings → Skip videos I've already downloaded).")
                outcome.notes = self.notes
                return outcome
            is_playlist = info.get("_type") in ("playlist", "multi_video")
            if is_playlist:
                # yt-dlp reads the template when it names each file, so playlists can get their own folder.
                ydl.params["outtmpl"].update(output_templates(job, playlist=True))
                outcome.title = info.get("title") or "Playlist"
                self.reporter.title(outcome.title, "Playlist")
            else:
                self._announce(info)
            result = ydl.process_ie_result(info, download=download_media)
            self.check_cancel()

            entries = list(iter_videos(result))
            videos = [v for v in entries if v and (downloaded_files(v) or not download_media)]
            # Videos in the download archive are left out of playlist results by yt-dlp.
            known = len(self._logger.archived)
            if known:
                self.notes.append(f"Skipped {known} video{'s' if known != 1 else ''} you downloaded before.")
            if not videos:
                if known:
                    outcome.notes = self.notes
                    return outcome
                if is_playlist and not entries:
                    raise JobError("This playlist is empty, or none of its videos are available.")
                raise JobError(self._last_error(
                    "Nothing could be downloaded from this link." if download_media
                    else "No videos were found at this link."))
            failed = len(entries) - len(videos)
            if failed:
                reason = self._last_error("they aren't available.")
                self.notes.append(f"Skipped {failed} of {len(entries)} videos. Last error: {reason}")

            outcome.title = outcome.title or videos[0].get("title") or job.url
            for video in videos:
                outcome.files.extend(downloaded_files(video))
                if job.split_chapters and download_media and self.env.ffmpeg and not job.clip:
                    chapters = chapter_files(video)
                    outcome.files.extend(chapters)
                    if not chapters:
                        self.notes.append(f"“{video.get('title')}” has no chapters, so it was saved as one file.")

            if self.transcript_mode != "off":
                for index, video in enumerate(videos, 1):
                    self.check_cancel()
                    media = next(iter(downloaded_files(video)), None)
                    self._handle_transcript(ydl, video, media, index, len(videos), outcome)

        outcome.notes = self.notes
        return outcome

    def _extract(self, ydl, url: str) -> dict | None:
        """Extract a link without processing it yet, so we can tell whether it is a playlist.

        Plain redirects (e.g. a video inside a playlist pointing to the playlist) are
        followed. Returns None if the video is in the download archive.
        """
        info = ydl.extract_info(url, download=False, process=False)
        for _ in range(5):
            if not info or info.get("_type") != "url":
                break
            info = ydl.extract_info(info["url"], download=False, process=False, ie_key=info.get("ie_key"))
        if not info:
            if self._logger.archived:
                return None
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
            "outtmpl": output_templates(self.job, playlist=False),
        }
        if self.env.ffmpeg:
            options["ffmpeg_location"] = self.env.ffmpeg
        if self.env.js_runtimes:
            options["js_runtimes"] = {name: {"path": path} for name, path in self.env.js_runtimes.items()}
        if not self.env.ejs:
            # Without the yt-dlp-ejs package, let yt-dlp fetch its official
            # challenge solver from GitHub so YouTube keeps working.
            options["remote_components"] = ["ejs:github"]
        if self.job.cookies_browser:
            options["cookiesfrombrowser"] = (self.job.cookies_browser,)
        return options

    def _download_options(self) -> dict:
        job = self.job
        has_ffmpeg = self.env.ffmpeg is not None
        options = {**self._base_options(), **media_options(job.quality, has_ffmpeg)}
        steps = postprocessors(job, has_ffmpeg)
        if steps:
            options["postprocessors"] = steps
        if job.quality == "mp3" and has_ffmpeg:
            options["writethumbnail"] = True
        if job.clip:
            options.update(clip_options(job.clip))
        if job.archive_file and job.quality != "none":
            options["download_archive"] = job.archive_file
        return options

    def _last_error(self, fallback: str) -> str:
        if self._logger.errors:
            return friendly_error(self._logger.errors[-1], self.env.ffmpeg is not None)
        return fallback

    # -- transcripts, subtitles and summaries ----------------------------------

    def _handle_transcript(self, ydl, video: dict, media: str | None, index: int, total: int, outcome: JobResult):
        job = self.job
        title = video.get("title") or video.get("id") or "video"
        label = f" ({index}/{total})" if total > 1 else ""
        self._announce(video)
        transcript = self._get_transcript(ydl, video, media, title, label)
        if transcript is None:
            return
        if not transcript.segments:
            self.notes.append(f"No speech was found in “{title}”.")
            return

        base = os.path.splitext(media or ydl.prepare_filename(video))[0]
        if job.transcript != "off":
            paths = transcriber.write_transcript(base, transcript, title=title, url=video.get("webpage_url"))
            outcome.transcripts += paths
            srt_path = paths[1]
        else:
            srt_path = os.path.join(self._tempdir, f"{video.get('id', 'video')}.srt")
            with open(srt_path, "w", encoding="utf-8") as fh:
                fh.write(transcriber.to_srt(transcript.segments))

        if job.embed_subtitles and media:
            self._embed_subtitles(media, srt_path, transcript.language, title, label)
        if job.summary:
            self._summarize(transcript, video, base, title, label, outcome)

    def _get_transcript(self, ydl, video: dict, media: str | None, title: str, label: str):
        job = self.job
        language = job.language or None
        clip = job.clip
        transcript = None

        if self.transcript_mode in ("auto", "subtitles"):
            self.reporter.status(f"Looking for subtitles{label}…")
            self.reporter.progress(None)
            transcript, error = self._fetch_subtitles(ydl, video, language)
            if transcript is None and language:
                transcript, _ = self._fetch_subtitles(ydl, video, None)
                if transcript is not None:
                    wanted = transcriber.language_name(language)
                    original = transcriber.language_name(transcript.language)
                    if error and "429" in error:
                        self.notes.append(f"YouTube is limiting requests for {wanted} translations right now, so the "
                                          f"transcript is in {original}. Try again later, or sign in with "
                                          "Settings → Browser cookies.")
                    else:
                        self.notes.append(f"“{title}” has no {wanted} subtitles, so the transcript is in {original}.")
            if transcript is not None and clip:
                # Subtitles cover the whole video; keep the clip, aligned with the clip file if there is one.
                transcript.segments = transcriber.clip_segments(transcript.segments, clip[0], clip[1], shift=bool(media))
            if transcript is None and self.transcript_mode == "subtitles":
                self.notes.append(f"No subtitles found for “{title}”.")
                return None
            if transcript is None and not self.env.whisper:
                self.notes.append(f"No subtitles found for “{title}”. Install faster-whisper "
                                  "to transcribe videos without subtitles (see README).")
                return None

        if transcript is None:
            try:
                audio = media or self._download_audio(video, label)
                transcript = self._run_whisper(audio, label, translate=transcriber.base_language(language) == "en")
            except Cancelled:
                raise
            except Exception as exc:
                log.exception("Transcription failed")
                message = str(exc) if isinstance(exc, JobError) else f"{type(exc).__name__}: {exc}"
                self.notes.append(f"Couldn't transcribe “{title}”: {message}")
                return None
            if clip and not media:
                # Only the clip's audio was transcribed; use the video's timeline.
                transcript.segments = transcriber.shift_segments(transcript.segments, clip[0])
            if language and transcriber.base_language(language) != transcriber.base_language(transcript.language):
                self.notes.append(f"Whisper can only translate into English, so the transcript of “{title}” is in "
                                  f"{transcriber.language_name(transcript.language)}.")
        return transcript

    def _fetch_subtitles(self, ydl, video: dict, language: str | None):
        """Returns (transcript or None, error message or None)."""
        self._mute_hooks = True
        try:
            return transcriber.fetch_subtitles(ydl, video, self._tempdir, language), None
        except Cancelled:
            raise
        except Exception as exc:
            log.warning("Couldn't fetch subtitles: %s", exc)
            return None, str(exc)
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
        if self.job.clip:
            options.update(clip_options(self.job.clip))
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.extract_info(url, download=True)
        files = downloaded_files(result)
        if not files:
            raise JobError(self._last_error("the audio couldn't be downloaded."))
        return files[0]

    def _run_whisper(self, path: str, label: str, translate: bool = False):
        name = self.job.whisper_model
        self.reporter.status(f"Loading the Whisper “{name}” model (downloaded on first use)…")
        self.reporter.progress(None)
        model = transcriber.load_whisper_model(name)
        self.check_cancel()
        self.reporter.status(f"{'Translating' if translate else 'Transcribing'} with Whisper{label}…")

        def on_progress(fraction, position, duration):
            detail = f"{fraction:.0%} · {format_duration(position)} of {format_duration(duration)}"
            self.reporter.progress(fraction, detail)

        return transcriber.transcribe_audio(
            model, path, name, ffmpeg=self.env.ffmpeg, translate=translate,
            on_progress=on_progress, check_cancel=self.check_cancel,
        )

    def _embed_subtitles(self, media: str, srt_path: str, language: str | None, title: str, label: str):
        if not self.env.ffmpeg:
            self.notes.append("Embedding subtitles needs ffmpeg, so the subtitles were saved as a separate file.")
            return
        if not transcriber.can_embed_subtitles(media):
            self.notes.append(f"Subtitles can't be embedded in {os.path.splitext(media)[1]} files, "
                              "so they were saved as a separate file.")
            return
        self.reporter.status(f"Adding subtitles to the video{label}…")
        self.reporter.progress(None)
        try:
            transcriber.embed_subtitles(self.env.ffmpeg, media, srt_path, language)
        except Exception as exc:
            log.exception("Embedding subtitles failed")
            self.notes.append(f"Couldn't add subtitles to “{title}”: {exc}")

    def _summarize(self, transcript, video: dict, base: str, title: str, label: str, outcome: JobResult):
        if self.job.ai is None:
            self.notes.append("Set up AI summaries in Settings to get a summary.")
            return
        self.reporter.progress(None)
        try:
            markdown = summarizer.summarize(
                transcript, title, self.job.ai, language=self.job.language or None,
                url=video.get("webpage_url"), on_step=lambda message: self.reporter.status(message + label),
                check_cancel=self.check_cancel,
            )
        except Cancelled:
            raise
        except summarizer.SummaryError as exc:
            self.notes.append(f"Couldn't summarize “{title}”: {exc}")
            return
        except Exception as exc:
            log.exception("Summary failed")
            self.notes.append(f"Couldn't summarize “{title}”: {type(exc).__name__}: {exc}")
            return
        path = base + ".summary.md"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        outcome.summaries.append(path)

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
            # Called right before a download starts. Clips are cut by ffmpeg, which reports no progress.
            self._announce(info)
            what = "clip" if self.job.clip else ("audio" if self.job.quality == "mp3" else "video")
            self.reporter.status(f"Downloading {what}…{self._item_label(info)}")
            self.reporter.progress(None)
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
