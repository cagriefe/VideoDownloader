"""Command-line interface with the same features as the app, for terminals and scripts."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys

from . import APP_NAME, __version__
from .downloader import (
    BROWSERS,
    QUALITY_PRESETS,
    TRANSCRIPT_MODES,
    WHISPER_MODELS,
    YTDLP_VERSION,
    Cancelled,
    DownloadJob,
    DownloadTask,
    JobError,
    JobResult,
    Reporter,
    detect_environment,
    extract_urls,
    format_count,
    format_duration,
    parse_clip,
    search_youtube,
)
from .history import History, HistoryEntry
from .settings import Settings, archive_path
from .summarizer import PROVIDERS, AIConfig
from .transcriber import LANGUAGES

EXAMPLES = """examples:
  python main.py https://youtu.be/dQw4w9WgXcQ
  python main.py URL -f mp3 --split-chapters
  python main.py URL -f none -t auto -l tr --summary
  python main.py URL -f 720 --clip 1:30-2:45 --embed-subs -t auto
  python main.py --search "lofi hip hop" --pick 1 -f mp3

Run without arguments to open the app."""


class ConsoleReporter(Reporter):
    """Prints progress on one line in a terminal, and plain status lines otherwise."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.live = self.stream.isatty()
        unicode = (getattr(self.stream, "encoding", "") or "").lower().startswith("utf")
        self.blocks = ("█", "░") if unicode else ("#", "-")
        self._status = ""
        self._line = False

    def _write_live(self, text: str) -> None:
        width = max(shutil.get_terminal_size().columns - 1, 20)
        self.stream.write("\r" + text[:width].ljust(width))
        self.stream.flush()
        self._line = True

    def finish_line(self) -> None:
        if self._line:
            self.stream.write("\n")
            self.stream.flush()
            self._line = False

    def status(self, text):
        if text == self._status:
            return
        self._status = text
        if self.live:
            self._write_live(f"  {text}")
        else:
            print(f"  {text}", file=self.stream)

    def title(self, title, subtitle=""):
        self.finish_line()
        print(f"▶ {title}" + (f"  ({subtitle})" if subtitle else ""), file=self.stream)

    def progress(self, fraction, detail=""):
        if not self.live:
            return
        if fraction is None:
            bar = ""
        else:
            filled = int(fraction * 24)
            bar = self.blocks[0] * filled + self.blocks[1] * (24 - filled)
        self._write_live(f"  {self._status} {bar} {detail}".rstrip())


def build_parser(settings: Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py", description=f"{APP_NAME}: download videos, music, transcripts and AI summaries.",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("urls", nargs="*", metavar="URL", help="links to videos or playlists")
    parser.add_argument("-f", "--format", choices=QUALITY_PRESETS, default="best",
                        help="video quality, mp3 for audio only, or none for transcripts and summaries only "
                             "(default: best)")
    parser.add_argument("-t", "--transcript", choices=TRANSCRIPT_MODES, default="off",
                        help="save a transcript (.txt and .srt): auto uses subtitles, then Whisper (default: off)")
    parser.add_argument("-l", "--language", metavar="CODE", default="",
                        help=f"language for transcripts and summaries, e.g. {', '.join(list(LANGUAGES)[:6])} "
                             "(default: as spoken)")
    parser.add_argument("-m", "--model", choices=WHISPER_MODELS, default=settings.whisper_model,
                        help=f"Whisper model for AI transcription (default: {settings.whisper_model})")
    parser.add_argument("-o", "--output", metavar="FOLDER", default=settings.download_dir,
                        help=f"where to save files (default: {settings.download_dir})")
    parser.add_argument("-p", "--playlist", action="store_true",
                        help="get the whole playlist when a link points to a video inside one")
    parser.add_argument("--clip", metavar="START-END", help="only download part of a video, e.g. 1:30-2:45 or 90-")
    parser.add_argument("--remove-sponsors", action="store_true", help="cut out sponsor segments (SponsorBlock)")
    parser.add_argument("--split-chapters", action="store_true", help="also save each chapter as its own file")
    parser.add_argument("--embed-subs", action="store_true", help="add the transcript to the video as subtitles")
    parser.add_argument("--summary", action="store_true", help="write an AI summary (.summary.md)")
    parser.add_argument("--ai-provider", choices=PROVIDERS, help="AI service for summaries (default: app settings)")
    parser.add_argument("--ai-model", metavar="NAME", help="AI model for summaries")
    parser.add_argument("--cookies-from", choices=BROWSERS, metavar="BROWSER",
                        help=f"use a browser's login for age-restricted videos ({', '.join(BROWSERS)})")
    parser.add_argument("--skip-downloaded", action="store_true",
                        help="skip videos you downloaded before, and remember new ones")
    parser.add_argument("-s", "--search", metavar="QUERY", help="search YouTube and list the results")
    parser.add_argument("--pick", type=int, metavar="N", help="with --search: download result number N")
    parser.add_argument("-v", "--verbose", action="store_true", help="show yt-dlp's messages")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__} (yt-dlp {YTDLP_VERSION})")
    return parser


def ai_config(args, settings: Settings) -> AIConfig:
    provider = args.ai_provider or settings.ai_provider
    same = provider == settings.ai_provider
    return AIConfig(
        provider=provider,
        model=args.ai_model or (settings.ai_model if same else ""),
        base_url=settings.ai_base_url if same else "",
        api_key=settings.ai_api_key if same else "",
    )


def print_result(result: JobResult) -> None:
    for path in result.files:
        print(f"  ✓ {path}")
    for path in result.transcripts + result.summaries:
        print(f"  ✓ {path}")
    for note in result.notes:
        print(f"  ! {note}")


def print_search_results(results: list[dict]) -> None:
    for number, item in enumerate(results, 1):
        details = [item["channel"]]
        if item["duration"]:
            details.append(format_duration(item["duration"]))
        if item["views"] is not None:
            details.append(f"{format_count(item['views'])} views")
        print(f"{number:>2}. {item['title']}\n    {' · '.join(d for d in details if d)}  {item['url']}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Don't crash on symbols like ✓ when output goes to a file with a non-UTF-8 encoding.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    settings = Settings.load()
    parser = build_parser(settings)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.CRITICAL,
                        format="%(levelname)s %(name)s: %(message)s")
    cookies = args.cookies_from or settings.cookies_browser

    if args.search:
        try:
            results = search_youtube(args.search, cookies_browser=cookies)
        except JobError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
        if args.pick is None:
            print_search_results(results)
            if results:
                print("\nDownload one with --pick N, e.g.:  python main.py --search "
                      f"\"{args.search}\" --pick 1")
            return 0 if results else 1
        if not 1 <= args.pick <= len(results):
            parser.error(f"--pick must be between 1 and {len(results)}")
        urls = [results[args.pick - 1]["url"]]
    else:
        urls = extract_urls(" ".join(args.urls))
        if not urls:
            parser.error("give at least one link to a video or playlist, or use --search")

    try:
        clip = parse_clip(*args.clip.partition("-")[::2]) if args.clip else None
    except ValueError as exc:
        parser.error(str(exc))

    env = detect_environment()
    if not env.ffmpeg:
        print("! ffmpeg wasn't found, so quality is limited and MP3 conversion is off.", file=sys.stderr)

    history = History()
    failures = 0
    for number, url in enumerate(urls, 1):
        if len(urls) > 1:
            print(f"[{number}/{len(urls)}] {url}")
        job = DownloadJob(
            url=url, output_dir=args.output, quality=args.format, transcript=args.transcript,
            language=args.language, whisper_model=args.model, playlist=args.playlist, clip=clip,
            remove_sponsors=args.remove_sponsors, split_chapters=args.split_chapters,
            embed_subtitles=args.embed_subs, summary=args.summary,
            ai=ai_config(args, settings) if args.summary else None, cookies_browser=cookies,
            archive_file=archive_path() if args.skip_downloaded else "",
        )
        reporter = ConsoleReporter()
        task = DownloadTask(job, reporter, env=env)
        try:
            result = task.run()
        except (KeyboardInterrupt, Cancelled):
            reporter.finish_line()
            print("Cancelled.", file=sys.stderr)
            return 130
        except JobError as exc:
            reporter.finish_line()
            print(f"  ✗ {exc}", file=sys.stderr)
            failures += 1
            continue
        reporter.finish_line()
        print_result(result)
        if result.outputs:
            history.add(HistoryEntry(title=result.title, url=url, files=result.files,
                                     transcripts=result.transcripts, summaries=result.summaries,
                                     folder=args.output))
    return 1 if failures else 0
