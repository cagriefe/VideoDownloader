"""The Video Downloader window: a download queue with search, history and settings."""

from __future__ import annotations

import itertools
import logging
import os
import queue
import threading
import tkinter
from dataclasses import dataclass
from datetime import datetime
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import APP_NAME, __version__, desktop
from .downloader import (
    BROWSERS,
    QUALITY_PRESETS,
    TRANSCRIPT_MODES,
    WHISPER_MISSING,
    WHISPER_MODELS,
    YTDLP_VERSION,
    Cancelled,
    DownloadJob,
    DownloadTask,
    JobError,
    JobResult,
    Reporter,
    clip_label,
    detect_environment,
    extract_urls,
    format_count,
    format_duration,
    parse_clip,
    search_youtube,
)
from .history import History, HistoryEntry
from .settings import Settings, archive_path, default_download_dir
from .summarizer import PROVIDERS, AIConfig, list_models
from .transcriber import LANGUAGES, language_name

# (light mode, dark mode) colors
GREEN = ("#1a7f37", "#3fb950")
RED = ("#cf222e", "#ff7b72")
AMBER = ("#9a6700", "#d29922")
MUTED = ("gray40", "gray62")
TEXT = ("gray10", "gray90")

LANGUAGE_OPTIONS = {"": "As spoken", **LANGUAGES}
BROWSER_OPTIONS = {"": "None", **{b: b.capitalize() for b in BROWSERS}}
APPEARANCES = {"system": "System", "light": "Light", "dark": "Dark"}
PROVIDER_HELP = {
    "ollama": "Free and private: runs on your computer. Install Ollama from ollama.com, "
              "then download a model, e.g. “ollama pull llama3.2”.",
    "openai": "Needs an API key from platform.openai.com. A summary usually costs well under a cent.",
    "anthropic": "Needs an API key from console.anthropic.com. A summary usually costs well under a cent.",
    "compatible": "Any server with an OpenAI-style API, such as LM Studio, llama.cpp or vLLM.",
}


def key_for(mapping: dict, label: str, default: str) -> str:
    return next((key for key, value in mapping.items() if value == label), default)


def display_path(path: str) -> str:
    """Shorten paths in the home folder to ~/… (except on Windows, where that's unusual)."""
    home = os.path.expanduser("~")
    if os.name != "nt" and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def outline_button(master, text, command, width=72, height=28):
    return ctk.CTkButton(master, text=text, command=command, width=width, height=height,
                         fg_color="transparent", border_width=1, text_color=TEXT)


def describe_job(job: DownloadJob) -> str:
    """A short summary of a job's options, e.g. "720p · transcript (Turkish) · AI summary"."""
    parts = [QUALITY_PRESETS[job.quality].replace(" (transcript only)", "")]
    if job.clip:
        parts.append(f"clip {clip_label(*job.clip)}")
    if job.transcript != "off":
        parts.append("transcript" + (f" ({language_name(job.language)})" if job.language else ""))
    for enabled, text in ((job.remove_sponsors, "no sponsors"), (job.split_chapters, "chapters"),
                          (job.embed_subtitles, "subtitles"), (job.summary, "AI summary"),
                          (job.playlist, "whole playlist")):
        if enabled:
            parts.append(text)
    return " · ".join(parts)


def describe_result(result: JobResult) -> str:
    parts = []
    if result.files:
        parts.append(f"Saved {len(result.files)} file{'s' if len(result.files) != 1 else ''}")
    transcripts = sum(1 for p in result.transcripts if p.endswith(".txt"))
    if transcripts:
        parts.append("transcript" if transcripts == 1 else f"{transcripts} transcripts")
    if result.summaries:
        parts.append("summary" if len(result.summaries) == 1 else f"{len(result.summaries)} summaries")
    return " · ".join(parts) or "Nothing new was saved"


class ItemReporter(Reporter):
    """Passes a task's updates to the GUI thread. Tk widgets must only be touched from that thread."""

    def __init__(self, events: queue.Queue, item_id: int):
        self.events, self.item_id = events, item_id

    def status(self, text):
        self.events.put(("status", self.item_id, text))

    def title(self, title, subtitle=""):
        self.events.put(("title", self.item_id, title, subtitle))

    def progress(self, fraction, detail=""):
        self.events.put(("progress", self.item_id, fraction, detail))


@dataclass
class QueueItem:
    id: int
    job: DownloadJob
    state: str = "waiting"  # waiting, running, done, failed or cancelled
    title: str = ""
    subtitle: str = ""
    status: str = ""
    error: str = ""
    result: JobResult | None = None
    task: DownloadTask | None = None
    row: QueueRow | None = None

    @property
    def finished(self) -> bool:
        return self.state in ("done", "failed", "cancelled")


class QueueRow(ctk.CTkFrame):
    ICONS = {"waiting": ("•", MUTED), "running": ("↓", TEXT), "done": ("✓", GREEN),
             "failed": ("✕", RED), "cancelled": ("–", AMBER)}

    def __init__(self, master, item: QueueItem, app: App):
        super().__init__(master, corner_radius=10)
        self.item, self.app = item, app
        self._indeterminate = False
        self.grid_columnconfigure(1, weight=1)
        self.icon = ctk.CTkLabel(self, text="", width=24, font=ctk.CTkFont(size=18, weight="bold"))
        self.icon.grid(row=0, column=0, rowspan=3, padx=(12, 2), pady=10, sticky="n")
        self.title_label = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                        font=ctk.CTkFont(size=14, weight="bold"))
        self.title_label.grid(row=0, column=1, sticky="ew", padx=6, pady=(10, 0))
        self.detail_label = ctk.CTkLabel(self, text="", anchor="w", justify="left")
        self.detail_label.grid(row=1, column=1, sticky="ew", padx=6, pady=(0, 10))
        self.bar = ctk.CTkProgressBar(self, height=6)
        self.bar.set(0)
        self.bar.grid(row=2, column=1, sticky="ew", padx=6, pady=(0, 12))
        self.buttons = ctk.CTkFrame(self, fg_color="transparent")
        self.buttons.grid(row=0, column=2, rowspan=3, padx=(4, 10), pady=10, sticky="e")
        self.open_button = outline_button(self.buttons, "Play", self.open_media, width=60)
        self.summary_button = outline_button(self.buttons, "Summary", self.open_summary, width=80)
        self.reveal_button = outline_button(self.buttons, "Show", self.reveal, width=60)
        self.retry_button = outline_button(self.buttons, "Retry", lambda: app.retry(item), width=60)
        self.close_button = ctk.CTkButton(self.buttons, text="✕", width=30, height=28, fg_color="transparent",
                                          text_color=MUTED, hover_color=("gray80", "gray25"),
                                          command=lambda: app.close_item(item))
        self.set_wrap(app.wrap_width - 260)
        self.refresh()

    def set_wrap(self, width: int) -> None:
        self.title_label.configure(wraplength=max(width, 200))
        self.detail_label.configure(wraplength=max(width, 200))

    def refresh(self) -> None:
        item = self.item
        icon, color = self.ICONS[item.state]
        self.icon.configure(text=icon, text_color=color)
        title = item.title or item.job.url
        self.title_label.configure(text=shorten(title, 120))
        result = item.result
        if item.state == "waiting":
            detail, color = f"Waiting · {describe_job(item.job)}", MUTED
        elif item.state == "running":
            detail, color = item.status or "Starting…", MUTED
        elif item.state == "done":
            detail = describe_result(result)
            if result.notes:
                detail += "\n" + "\n".join(result.notes[:3])
                if len(result.notes) > 3:
                    detail += f"\n…and {len(result.notes) - 3} more"
            color = AMBER if result.notes else GREEN
        elif item.state == "failed":
            detail, color = item.error, RED
        else:
            detail, color = "Cancelled", AMBER
        self.detail_label.configure(text=detail, text_color=color)

        if item.state == "running":
            self.bar.grid()
        else:
            self._stop_bar()
            self.bar.grid_remove()

        for button in self.buttons.winfo_children():
            button.pack_forget()
        media = self.media_file()
        visible = [
            (self.open_button, item.state == "done" and media is not None),
            (self.summary_button, item.state == "done" and bool(result and result.summaries)),
            (self.reveal_button, item.state == "done" and bool(result and result.outputs)),
            (self.retry_button, item.state in ("failed", "cancelled")),
            (self.close_button, True),
        ]
        for button, show in visible:
            if show:
                button.pack(side="left", padx=2)

    def set_status(self, text: str) -> None:
        self.detail_label.configure(text=text, text_color=MUTED)

    def set_progress(self, fraction: float | None, detail: str) -> None:
        if fraction is None:
            if not self._indeterminate:
                self.bar.configure(mode="indeterminate")
                self.bar.start()
                self._indeterminate = True
        else:
            self._stop_bar()
            self.bar.set(max(0.0, min(1.0, fraction)))
        status = self.item.status or "Working…"
        self.detail_label.configure(text=f"{status}  {detail}" if detail else status, text_color=MUTED)

    def _stop_bar(self) -> None:
        if self._indeterminate:
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self._indeterminate = False

    def media_file(self) -> str | None:
        result = self.item.result
        files = [f for f in (result.files if result else []) if os.path.exists(f)]
        return files[0] if files else None

    def open_media(self) -> None:
        if self.media_file():
            desktop.open_path(self.media_file())

    def open_summary(self) -> None:
        summaries = [s for s in self.item.result.summaries if os.path.exists(s)]
        if summaries:
            desktop.open_path(summaries[0])

    def reveal(self) -> None:
        outputs = [p for p in self.item.result.outputs if os.path.exists(p)]
        desktop.reveal(outputs[0] if outputs else self.item.job.output_dir)


class App(ctk.CTk):
    def __init__(self):
        self.settings = Settings.load()
        ctk.set_appearance_mode(self.settings.appearance)
        super().__init__()
        self.env = detect_environment()
        logging.info(
            "yt-dlp %s · ffmpeg: %s · JavaScript runtimes: %s · Whisper: %s",
            YTDLP_VERSION, self.env.ffmpeg or "not found", ", ".join(self.env.js_runtimes) or "none",
            "installed" if self.env.whisper else "not installed",
        )
        self.history = History()
        self.events: queue.Queue = queue.Queue()
        self.items: list[QueueItem] = []
        self.active: QueueItem | None = None
        self._ids = itertools.count(1)
        self._session = {"done": 0, "failed": 0}
        self._clipboard = None
        self.wrap_width = 880
        self.dialogs: dict[str, ctk.CTkToplevel] = {}

        self.title(APP_NAME)
        self.geometry("940x800")
        self.minsize(860, 680)
        self._build()
        self._load_settings()
        self._show_ready()

        # add="+" keeps customtkinter's own <Configure> handler working.
        self.bind("<Configure>", self._on_resize, add="+")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._process_events)
        self.after(800, self._watch_clipboard)

    # -- layout ----------------------------------------------------------------

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=24, pady=(20, 8), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text=APP_NAME, font=ctk.CTkFont(size=26, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="Download videos, music, transcripts and AI summaries from YouTube and 1000+ sites.",
                     text_color=MUTED).grid(row=1, column=0, sticky="w")
        outline_button(header, "History", self.open_history, width=90, height=32).grid(row=0, column=1, rowspan=2, padx=4)
        outline_button(header, "Settings", self.open_settings, width=90, height=32).grid(row=0, column=2, rowspan=2, padx=4)

        link_row = ctk.CTkFrame(self, fg_color="transparent")
        link_row.grid(row=1, column=0, padx=24, pady=6, sticky="ew")
        link_row.grid_columnconfigure(0, weight=1)
        self.url_entry = ctk.CTkEntry(link_row, height=42,
                                      placeholder_text="Paste one or more links, or type words to search YouTube…")
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", self.add_from_input)
        outline_button(link_row, "Paste", self.paste_links, width=80, height=42).grid(row=0, column=1, padx=(8, 0))
        self.add_button = ctk.CTkButton(link_row, text="Download", width=130, height=42,
                                        font=ctk.CTkFont(size=15, weight="bold"), command=self.add_from_input)
        self.add_button.grid(row=0, column=2, padx=(8, 0))

        options = ctk.CTkFrame(self)
        options.grid(row=2, column=0, padx=24, pady=10, sticky="ew")
        options.grid_columnconfigure(4, weight=1)
        pad = {"pady": 5}

        def label(text, row, column, first=False):
            ctk.CTkLabel(options, text=text).grid(row=row, column=column, padx=(16 if first else 22, 10), sticky="w",
                                                  pady=(12, 5) if row == 0 else 5)

        label("Format", 0, 0, first=True)
        self.quality_menu = ctk.CTkOptionMenu(options, values=list(QUALITY_PRESETS.values()), width=210,
                                              dynamic_resizing=False, command=self._on_quality_change)
        self.quality_menu.grid(row=0, column=1, sticky="w", pady=(12, 5))
        label("Transcript", 0, 2)
        self.transcript_menu = ctk.CTkOptionMenu(options, values=list(TRANSCRIPT_MODES.values()), width=210,
                                                 dynamic_resizing=False, command=self._on_transcript_change)
        self.transcript_menu.grid(row=0, column=3, sticky="w", pady=(12, 5))

        label("Language", 1, 0, first=True)
        self.language_menu = ctk.CTkOptionMenu(options, values=list(LANGUAGE_OPTIONS.values()), width=210,
                                               dynamic_resizing=False, command=lambda _: self._save_settings())
        self.language_menu.grid(row=1, column=1, sticky="w", **pad)
        label("AI model", 1, 2)
        model_row = ctk.CTkFrame(options, fg_color="transparent")
        model_row.grid(row=1, column=3, columnspan=2, sticky="w", **pad)
        self.model_menu = ctk.CTkOptionMenu(model_row, values=list(WHISPER_MODELS), width=110, dynamic_resizing=False,
                                            command=lambda _: self._save_settings())
        self.model_menu.pack(side="left")
        self.playlist_check = ctk.CTkCheckBox(model_row, text="Whole playlist", command=self._save_settings)
        self.playlist_check.pack(side="left", padx=(24, 0))

        label("Clip", 2, 0, first=True)
        clip_row = ctk.CTkFrame(options, fg_color="transparent")
        clip_row.grid(row=2, column=1, columnspan=4, sticky="w", **pad)
        self.clip_start = ctk.CTkEntry(clip_row, width=80, placeholder_text="start")
        self.clip_start.pack(side="left")
        ctk.CTkLabel(clip_row, text="to").pack(side="left", padx=8)
        self.clip_end = ctk.CTkEntry(clip_row, width=80, placeholder_text="end")
        self.clip_end.pack(side="left")
        ctk.CTkLabel(clip_row, text="e.g. 1:30 to 2:45 · leave empty for the whole video",
                     text_color=MUTED).pack(side="left", padx=12)

        label("Extras", 3, 0, first=True)
        extras = ctk.CTkFrame(options, fg_color="transparent")
        extras.grid(row=3, column=1, columnspan=4, sticky="w", **pad)
        self.sponsors_check = ctk.CTkCheckBox(extras, text="Remove sponsors", command=self._save_settings)
        self.chapters_check = ctk.CTkCheckBox(extras, text="Split into chapters", command=self._save_settings)
        self.embed_check = ctk.CTkCheckBox(extras, text="Embed subtitles", command=self._on_embed_change)
        self.summary_check = ctk.CTkCheckBox(extras, text="AI summary", command=self._on_summary_change)
        for check in (self.sponsors_check, self.chapters_check, self.embed_check, self.summary_check):
            check.pack(side="left", padx=(0, 18))

        label("Save to", 4, 0, first=True)
        folder_row = ctk.CTkFrame(options, fg_color="transparent")
        folder_row.grid(row=4, column=1, columnspan=4, sticky="ew", padx=(0, 16), pady=(5, 14))
        folder_row.grid_columnconfigure(0, weight=1)
        self.folder_entry = ctk.CTkEntry(folder_row)
        self.folder_entry.grid(row=0, column=0, sticky="ew")
        self.folder_entry.bind("<FocusOut>", lambda _: self._save_settings())
        outline_button(folder_row, "Browse…", self.choose_folder, width=100, height=28).grid(row=0, column=1, padx=(8, 0))

        queue_header = ctk.CTkFrame(self, fg_color="transparent")
        queue_header.grid(row=3, column=0, padx=24, pady=(8, 2), sticky="ew")
        queue_header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(queue_header, text="Queue", font=ctk.CTkFont(size=16, weight="bold")).grid(row=0, column=0, sticky="w")
        self.queue_count = ctk.CTkLabel(queue_header, text="", text_color=MUTED)
        self.queue_count.grid(row=0, column=1, sticky="w", padx=10)
        outline_button(queue_header, "Clear finished", self.clear_finished, width=110).grid(row=0, column=2, padx=4)
        outline_button(queue_header, "Cancel all", self.cancel_all, width=90).grid(row=0, column=3, padx=(4, 0))

        self.queue_list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.queue_list.grid(row=4, column=0, padx=16, pady=0, sticky="nsew")
        self.empty_label = ctk.CTkLabel(
            self.queue_list, text_color=MUTED, justify="center",
            text="Your downloads will appear here.\nPaste a link above, or type a few words to search YouTube.")
        self.empty_label.pack(pady=40)

        self.status_label = ctk.CTkLabel(self, text="", anchor="w", justify="left", text_color=MUTED,
                                         wraplength=self.wrap_width)
        self.status_label.grid(row=5, column=0, padx=24, pady=(6, 14), sticky="ew")
        self.bind("<Escape>", lambda _: self.cancel_all())

    # -- settings ----------------------------------------------------------------

    def _load_settings(self):
        s = self.settings
        self.quality_menu.set(QUALITY_PRESETS.get(s.quality, QUALITY_PRESETS["best"]))
        self.transcript_menu.set(TRANSCRIPT_MODES.get(s.transcript, TRANSCRIPT_MODES["off"]))
        self.language_menu.set(LANGUAGE_OPTIONS.get(s.language, LANGUAGE_OPTIONS[""]))
        self.model_menu.set(s.whisper_model if s.whisper_model in WHISPER_MODELS else "small")
        for check, value in ((self.playlist_check, s.playlist), (self.sponsors_check, s.remove_sponsors),
                             (self.chapters_check, s.split_chapters), (self.embed_check, s.embed_subtitles),
                             (self.summary_check, s.summary)):
            check.select() if value else check.deselect()
        self._set_folder(s.download_dir)
        self._update_model_state()

    def _save_settings(self):
        s = self.settings
        s.quality = key_for(QUALITY_PRESETS, self.quality_menu.get(), "best")
        s.transcript = key_for(TRANSCRIPT_MODES, self.transcript_menu.get(), "off")
        s.language = key_for(LANGUAGE_OPTIONS, self.language_menu.get(), "")
        s.whisper_model = self.model_menu.get()
        s.playlist = bool(self.playlist_check.get())
        s.remove_sponsors = bool(self.sponsors_check.get())
        s.split_chapters = bool(self.chapters_check.get())
        s.embed_subtitles = bool(self.embed_check.get())
        s.summary = bool(self.summary_check.get())
        if os.path.isdir(self._folder()):
            s.download_dir = self._folder()
        s.save()

    def _folder(self) -> str:
        text = self.folder_entry.get().strip()
        return os.path.abspath(os.path.expanduser(text)) if text else default_download_dir()

    def _set_folder(self, path: str):
        self.folder_entry.delete(0, "end")
        self.folder_entry.insert(0, display_path(path))

    def ai_config(self) -> AIConfig:
        s = self.settings
        return AIConfig(provider=s.ai_provider, model=s.ai_model, base_url=s.ai_base_url, api_key=s.ai_api_key)

    # -- options --------------------------------------------------------------

    def _ensure_transcript(self):
        if key_for(TRANSCRIPT_MODES, self.transcript_menu.get(), "off") == "off":
            self.transcript_menu.set(TRANSCRIPT_MODES["auto"])

    def _on_quality_change(self, _label):
        if key_for(QUALITY_PRESETS, self.quality_menu.get(), "best") == "none":
            self._ensure_transcript()
        self._update_model_state()
        self._save_settings()

    def _on_transcript_change(self, _label):
        if key_for(TRANSCRIPT_MODES, self.transcript_menu.get(), "off") == "whisper" and not self.env.whisper:
            self.set_status(WHISPER_MISSING, AMBER)
        self._update_model_state()
        self._save_settings()

    def _on_embed_change(self):
        if self.embed_check.get():
            self._ensure_transcript()
            self._update_model_state()
        self._save_settings()

    def _on_summary_change(self):
        if self.summary_check.get():
            self._ensure_transcript()
            self._update_model_state()
            config = self.ai_config().resolved()
            if config.details["key_env"] and not config.api_key:
                self.set_status(f"AI summaries use {config.label}. Add your API key in Settings first.", AMBER)
            else:
                self.set_status(f"AI summaries use {config.label}. You can change this in Settings.")
        self._save_settings()

    def _update_model_state(self):
        mode = key_for(TRANSCRIPT_MODES, self.transcript_menu.get(), "off")
        uses_ai = self.env.whisper and mode in ("auto", "whisper")
        self.model_menu.configure(state="normal" if uses_ai else "disabled")

    def make_job(self, url: str) -> DownloadJob:
        """A job for `url` with the current options. Raises ValueError for an invalid clip."""
        s = self.settings
        self._save_settings()
        clip = parse_clip(self.clip_start.get(), self.clip_end.get())
        return DownloadJob(
            url=url, output_dir=self._folder(), quality=s.quality, transcript=s.transcript,
            language=s.language, whisper_model=s.whisper_model, playlist=s.playlist, clip=clip,
            remove_sponsors=s.remove_sponsors, split_chapters=s.split_chapters,
            embed_subtitles=s.embed_subtitles, summary=s.summary,
            ai=self.ai_config() if s.summary else None, cookies_browser=s.cookies_browser,
            archive_file=archive_path() if s.skip_downloaded else "",
        )

    # -- adding links ------------------------------------------------------------

    def add_from_input(self, _event=None):
        text = self.url_entry.get().strip()
        if not text:
            self.set_status("Paste a link to a video or playlist, or type something to search YouTube.", RED)
            return
        urls = extract_urls(text)
        if urls:
            if self.enqueue_urls(urls):
                self.url_entry.delete(0, "end")
        else:
            self.open_search(text)

    def paste_links(self):
        try:
            text = self.clipboard_get().strip()
        except tkinter.TclError:
            text = ""
        urls = extract_urls(text)
        if urls:
            self.enqueue_urls(urls)
        elif text:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, text)
            self.set_status("That isn't a link. Press Download to search YouTube for it.")
        else:
            self.set_status("The clipboard is empty.", AMBER)

    def enqueue_urls(self, urls: list[str]) -> bool:
        if key_for(QUALITY_PRESETS, self.quality_menu.get(), "best") == "none" and \
                key_for(TRANSCRIPT_MODES, self.transcript_menu.get(), "off") == "off" and not self.summary_check.get():
            self.set_status("Choose a transcript option, or pick a video or audio format to download.", RED)
            return False
        try:
            jobs = [self.make_job(url) for url in urls]
        except ValueError as exc:
            self.set_status(f"Clip: {exc}", RED)
            return False
        for job in jobs:
            item = QueueItem(id=next(self._ids), job=job)
            self.items.append(item)
            self.empty_label.pack_forget()
            item.row = QueueRow(self.queue_list, item, self)
            item.row.pack(fill="x", padx=8, pady=4)
        count = len(jobs)
        self.set_status(f"Added {count} link{'s' if count != 1 else ''} to the queue.")
        self._update_queue_header()
        self._start_next()
        return True

    def _watch_clipboard(self):
        """Fill in a link the user just copied, so pressing Enter is enough."""
        try:
            if self.settings.watch_clipboard and not self.url_entry.get():
                try:
                    text = self.clipboard_get()
                except tkinter.TclError:
                    text = ""
                if text != self._clipboard:
                    self._clipboard = text
                    urls = extract_urls(text)
                    if urls and len(text) < 5000:
                        self.url_entry.insert(0, " ".join(urls))
                        found = "a link" if len(urls) == 1 else f"{len(urls)} links"
                        self.set_status(f"Found {found} in your clipboard. Press Enter to download.")
        finally:
            self.after(1000, self._watch_clipboard)

    # -- queue ---------------------------------------------------------------

    def _start_next(self):
        if self.active is not None:
            return
        item = next((i for i in self.items if i.state == "waiting"), None)
        if item is None:
            self._queue_idle()
            return
        item.state = "running"
        item.status = "Starting…"
        self.active = item
        item.task = DownloadTask(item.job, ItemReporter(self.events, item.id), env=self.env)
        item.row.refresh()
        item.row.set_progress(None, "")
        self._update_queue_header()
        threading.Thread(target=self._run_item, args=(item.id, item.task), daemon=True).start()

    def _run_item(self, item_id: int, task: DownloadTask):
        try:
            result = task.run()
        except Cancelled:
            self.events.put(("cancelled", item_id))
        except JobError as exc:
            self.events.put(("failed", item_id, str(exc)))
        except Exception as exc:
            logging.exception("Download failed")
            self.events.put(("failed", item_id, f"Something went wrong: {exc}"))
        else:
            self.events.put(("finished", item_id, result))

    def _queue_idle(self):
        self.title(APP_NAME)
        finished = self._session["done"] + self._session["failed"]
        if not finished:
            return
        done, failed = self._session["done"], self._session["failed"]
        message = f"{done} download{'s' if done != 1 else ''} finished" + (f", {failed} failed" if failed else "")
        self.set_status(message + ".", RED if failed and not done else (AMBER if failed else GREEN))
        if self.settings.notify and self.focus_displayof() is None:
            desktop.notify(APP_NAME, message)
        self._session = {"done": 0, "failed": 0}

    def _update_queue_header(self):
        waiting = sum(1 for i in self.items if i.state == "waiting")
        running = 1 if self.active else 0
        finished = sum(1 for i in self.items if i.finished)
        parts = []
        if running:
            parts.append("1 downloading")
        if waiting:
            parts.append(f"{waiting} waiting")
        if finished:
            parts.append(f"{finished} finished")
        self.queue_count.configure(text=" · ".join(parts))

    def close_item(self, item: QueueItem):
        if item.state == "running":
            item.task.cancel()
            item.status = "Cancelling…"
            item.row.set_status("Cancelling…")
            return
        self.items.remove(item)
        item.row.destroy()
        if not self.items:
            self.empty_label.pack(pady=40)
        self._update_queue_header()

    def retry(self, item: QueueItem):
        item.state, item.error, item.result, item.status = "waiting", "", None, ""
        item.row.refresh()
        self._update_queue_header()
        self._start_next()

    def clear_finished(self):
        for item in [i for i in self.items if i.finished]:
            self.close_item(item)

    def cancel_all(self):
        for item in self.items:
            if item.state == "waiting":
                item.state = "cancelled"
                item.row.refresh()
        if self.active:
            self.close_item(self.active)
        self._update_queue_header()

    # -- events from the worker thread ------------------------------------------

    def _process_events(self):
        try:
            while True:
                kind, item_id, *args = self.events.get_nowait()
                item = next((i for i in self.items if i.id == item_id), None)
                if item is not None:
                    getattr(self, f"_on_{kind}")(item, *args)
        except queue.Empty:
            pass
        finally:
            self.after(80, self._process_events)

    def _on_status(self, item: QueueItem, text: str):
        if item.status != "Cancelling…":
            item.status = text
            item.row.set_status(text)

    def _on_title(self, item: QueueItem, title: str, subtitle: str):
        item.title, item.subtitle = title, subtitle
        item.row.title_label.configure(text=shorten(title + (f"  ·  {subtitle}" if subtitle else ""), 120))

    def _on_progress(self, item: QueueItem, fraction: float | None, detail: str):
        if item.status == "Cancelling…":
            return
        item.row.set_progress(fraction, detail)
        waiting = sum(1 for i in self.items if i.state == "waiting")
        suffix = f" · {waiting} waiting" if waiting else ""
        self.title(f"{fraction:.0%}{suffix} · {APP_NAME}" if fraction is not None else APP_NAME)

    def _on_finished(self, item: QueueItem, result: JobResult):
        item.state, item.result = "done", result
        for note in result.notes:
            logging.info(note)
        if result.outputs:
            self.history.add(HistoryEntry(
                title=result.title or item.title or item.job.url, url=item.job.url, files=result.files,
                transcripts=result.transcripts, summaries=result.summaries, folder=item.job.output_dir))
        self._session["done"] += 1
        self._item_ended(item)

    def _on_failed(self, item: QueueItem, message: str):
        item.state, item.error = "failed", message
        self._session["failed"] += 1
        self._item_ended(item)

    def _on_cancelled(self, item: QueueItem):
        item.state = "cancelled"
        self._item_ended(item)

    def _item_ended(self, item: QueueItem):
        item.task = None
        if self.active is item:
            self.active = None
        item.row.refresh()
        self._update_queue_header()
        self._start_next()

    # -- dialogs -----------------------------------------------------------------

    def _open_dialog(self, name: str, factory):
        dialog = self.dialogs.get(name)
        if dialog is not None and dialog.winfo_exists():
            dialog.deiconify()
            dialog.lift()
            dialog.focus_force()
            return dialog
        dialog = factory()
        self.dialogs[name] = dialog
        # Toplevels can open behind the main window on some systems.
        dialog.after(150, dialog.lift)
        dialog.after(200, dialog.focus_force)
        return dialog

    def open_search(self, query: str):
        dialog = self._open_dialog("search", lambda: SearchDialog(self, query))
        if dialog.query != query:
            dialog.search(query)

    def open_settings(self):
        self._open_dialog("settings", lambda: SettingsDialog(self))

    def open_history(self):
        dialog = self._open_dialog("history", lambda: HistoryDialog(self))
        dialog.refresh()

    def choose_folder(self):
        current = self._folder()
        folder = filedialog.askdirectory(parent=self, title="Choose where to save downloads",
                                         initialdir=current if os.path.isdir(current) else default_download_dir())
        if folder:
            self._set_folder(folder)
            self._save_settings()

    # -- helpers ------------------------------------------------------------------

    def _show_ready(self):
        problems = []
        if not self.env.ffmpeg:
            problems.append("ffmpeg wasn't found, so quality is limited and MP3, clips and extras are off.")
        if not self.env.js_runtimes:
            problems.append("No JavaScript runtime (Deno or Node.js) was found, so YouTube downloads may fail.")
        if problems:
            self.set_status(" ".join(problems) + " See the README for setup help.", AMBER)
        else:
            self.set_status("Ready. Paste a link and press Download, or type words to search YouTube.")

    def set_status(self, text: str, color=None):
        self.status_label.configure(text=text, text_color=color or MUTED)

    def _on_resize(self, event):
        if event.widget is not self or event.width <= 1:
            return
        width = int(event.width / ctk.ScalingTracker.get_widget_scaling(self)) - 60
        if abs(width - self.wrap_width) > 4:
            self.wrap_width = width
            self.status_label.configure(wraplength=width)
            for item in self.items:
                item.row.set_wrap(width - 260)

    def _on_close(self):
        if self.active is not None:
            if not messagebox.askyesno(APP_NAME, "Downloads are still running. Stop them and quit?", parent=self):
                return
            self.active.task.cancel()
        self._save_settings()
        self.destroy()


class SearchDialog(ctk.CTkToplevel):
    """Search YouTube and add results to the queue."""

    def __init__(self, app: App, query: str):
        super().__init__(app)
        self.app = app
        self.query = ""
        self.results: queue.Queue = queue.Queue()
        self.choices: list[tuple[ctk.CTkCheckBox, str]] = []
        self.title("Search YouTube")
        self.geometry("780x620")
        self.minsize(620, 440)
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, padx=20, pady=(18, 6), sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        self.entry = ctk.CTkEntry(top, height=38)
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<Return>", lambda _: self.search(self.entry.get()))
        self.search_button = ctk.CTkButton(top, text="Search", width=100, height=38,
                                           command=lambda: self.search(self.entry.get()))
        self.search_button.grid(row=0, column=1, padx=(8, 0))
        self.status = ctk.CTkLabel(self, text="", anchor="w", text_color=MUTED)
        self.status.grid(row=1, column=0, padx=22, sticky="ew")
        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list.grid(row=2, column=0, padx=12, pady=6, sticky="nsew")

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=3, column=0, padx=20, pady=(6, 18), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)
        outline_button(bottom, "Close", self.destroy, width=90, height=36).grid(row=0, column=1, padx=(8, 0))
        self.add_button = ctk.CTkButton(bottom, text="Download selected", width=170, height=36, command=self.add_selected)
        self.add_button.grid(row=0, column=2, padx=(8, 0))
        self.search(query)
        self.after(100, self._poll)

    def search(self, query: str):
        query = query.strip()
        if not query:
            return
        self.query = query
        self.entry.delete(0, "end")
        self.entry.insert(0, query)
        for child in self.list.winfo_children():
            child.destroy()
        self.choices = []
        self.status.configure(text=f"Searching YouTube for “{query}”…", text_color=MUTED)
        self.search_button.configure(state="disabled")
        cookies = self.app.settings.cookies_browser
        threading.Thread(target=self._run, args=(query, cookies), daemon=True).start()

    def _run(self, query: str, cookies: str):
        try:
            self.results.put((query, search_youtube(query, limit=20, cookies_browser=cookies), None))
        except Exception as exc:
            self.results.put((query, [], str(exc)))

    def _poll(self):
        if not self.winfo_exists():
            return
        try:
            query, results, error = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            if query == self.query:
                self._show(results, error)
        self.after(100, self._poll)

    def _show(self, results: list[dict], error: str | None):
        self.search_button.configure(state="normal")
        if error:
            self.status.configure(text=f"Search failed: {error}", text_color=RED)
            return
        if not results:
            self.status.configure(text="No results. Try other words.", text_color=AMBER)
            return
        for item in results:
            row = ctk.CTkFrame(self.list, corner_radius=8)
            row.pack(fill="x", padx=6, pady=3)
            row.grid_columnconfigure(0, weight=1)
            check = ctk.CTkCheckBox(row, text=shorten(item["title"], 85), font=ctk.CTkFont(weight="bold"))
            check.grid(row=0, column=0, sticky="w", padx=10, pady=(8, 0))
            details = [item["channel"]]
            if item["duration"]:
                details.append(format_duration(item["duration"]))
            if item["views"] is not None:
                details.append(f"{format_count(item['views'])} views")
            ctk.CTkLabel(row, text=" · ".join(d for d in details if d), text_color=MUTED).grid(
                row=1, column=0, sticky="w", padx=(42, 10), pady=(0, 8))
            outline_button(row, "Preview", lambda url=item["url"]: desktop.open_url(url), width=80).grid(
                row=0, column=1, rowspan=2, padx=10)
            self.choices.append((check, item["url"]))
        self.status.configure(text=f"{len(results)} results. Tick the videos you want, then press Download selected.",
                              text_color=MUTED)

    def add_selected(self):
        urls = [url for check, url in self.choices if check.get()]
        if not urls:
            self.status.configure(text="Tick at least one video first.", text_color=AMBER)
            return
        if self.app.enqueue_urls(urls):
            self.destroy()


class SettingsDialog(ctk.CTkToplevel):
    """Appearance, download preferences and the AI service for summaries."""

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.models: queue.Queue = queue.Queue()
        s = app.settings
        self.title("Settings")
        self.geometry("620x700")
        self.minsize(560, 600)
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)
        self.protocol("WM_DELETE_WINDOW", self.close)

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 0))
        self.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        def section(text, row):
            ctk.CTkLabel(body, text=text, font=ctk.CTkFont(size=16, weight="bold")).grid(
                row=row, column=0, columnspan=2, sticky="w", padx=12, pady=(16, 6))

        def field_label(text, row):
            ctk.CTkLabel(body, text=text).grid(row=row, column=0, sticky="w", padx=(12, 14), pady=5)

        def help_text(text, row):
            label = ctk.CTkLabel(body, text=text, text_color=MUTED, justify="left", anchor="w", wraplength=440)
            label.grid(row=row, column=1, sticky="w", pady=(0, 6))
            return label

        section("Appearance", 0)
        self.appearance = ctk.CTkSegmentedButton(body, values=list(APPEARANCES.values()), command=self._on_appearance)
        self.appearance.set(APPEARANCES.get(s.appearance, "System"))
        self.appearance.grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=5)

        section("Downloads", 2)
        field_label("Browser cookies", 3)
        self.browser_menu = ctk.CTkOptionMenu(body, values=list(BROWSER_OPTIONS.values()), width=180,
                                              dynamic_resizing=False)
        self.browser_menu.set(BROWSER_OPTIONS.get(s.cookies_browser, "None"))
        self.browser_menu.grid(row=3, column=1, sticky="w", pady=5)
        help_text("Uses your browser's login, for age-restricted or members-only videos and YouTube's "
                  "“not a bot” checks. Your browser may ask for permission.", 4)
        self.skip_check = ctk.CTkCheckBox(body, text="Skip videos I've already downloaded")
        self.notify_check = ctk.CTkCheckBox(body, text="Notify me when downloads finish")
        self.clipboard_check = ctk.CTkCheckBox(body, text="Fill in links I copy")
        for row, (check, value) in enumerate(((self.skip_check, s.skip_downloaded), (self.notify_check, s.notify),
                                              (self.clipboard_check, s.watch_clipboard)), start=5):
            check.grid(row=row, column=0, columnspan=2, sticky="w", padx=12, pady=5)
            check.select() if value else check.deselect()

        section("AI summaries", 8)
        field_label("Service", 9)
        self.provider_menu = ctk.CTkOptionMenu(body, values=[p["label"] for p in PROVIDERS.values()], width=260,
                                               dynamic_resizing=False, command=self._on_provider)
        self.provider_menu.set(PROVIDERS.get(s.ai_provider, PROVIDERS["ollama"])["label"])
        self.provider_menu.grid(row=9, column=1, sticky="w", pady=5)
        self.provider_help = help_text("", 10)
        field_label("Model", 11)
        self.model_box = ctk.CTkComboBox(body, values=[], width=260)
        self.model_box.set(s.ai_model)
        self.model_box.grid(row=11, column=1, sticky="w", pady=5)
        field_label("API key", 12)
        self.key_entry = ctk.CTkEntry(body, width=320, show="•")
        self.key_entry.insert(0, s.ai_api_key)
        self.key_entry.grid(row=12, column=1, sticky="w", pady=5)
        field_label("Server", 13)
        self.server_entry = ctk.CTkEntry(body, width=320)
        self.server_entry.insert(0, s.ai_base_url)
        self.server_entry.grid(row=13, column=1, sticky="w", pady=5)
        check_row = ctk.CTkFrame(body, fg_color="transparent")
        check_row.grid(row=14, column=1, sticky="w", pady=(8, 4))
        self.check_button = outline_button(check_row, "Check connection", self.check_connection, width=140, height=30)
        self.check_button.pack(side="left")
        self.check_status = ctk.CTkLabel(check_row, text="", text_color=MUTED, wraplength=280, justify="left")
        self.check_status.pack(side="left", padx=10)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=1, column=0, sticky="ew", padx=20, pady=14)
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(footer, text=f"{APP_NAME} {__version__} · yt-dlp {YTDLP_VERSION}", text_color=MUTED).grid(
            row=0, column=0, sticky="w")
        ctk.CTkButton(footer, text="Done", width=100, command=self.close).grid(row=0, column=1)
        self._on_provider(self.provider_menu.get())
        self.after(100, self._poll)

    def _provider(self) -> str:
        label = self.provider_menu.get()
        return next((key for key, p in PROVIDERS.items() if p["label"] == label), "ollama")

    def _on_provider(self, _label):
        provider = self._provider()
        details = PROVIDERS[provider]
        self.provider_help.configure(text=PROVIDER_HELP[provider])
        self.key_entry.configure(placeholder_text=f"or set {details['key_env']}" if details["key_env"] else "not needed")
        self.server_entry.configure(placeholder_text=details["base_url"])
        self.model_box.configure(values=[details["model"]] if details["model"] else [])
        if provider != self.app.settings.ai_provider:
            self.model_box.set(details["model"])
        self.check_status.configure(text="")

    def _on_appearance(self, label):
        ctk.set_appearance_mode(key_for(APPEARANCES, label, "system"))

    def config(self) -> AIConfig:
        return AIConfig(provider=self._provider(), model=self.model_box.get().strip(),
                        base_url=self.server_entry.get().strip(), api_key=self.key_entry.get().strip())

    def check_connection(self):
        self.check_button.configure(state="disabled")
        self.check_status.configure(text="Checking…", text_color=MUTED)
        config = self.config()
        threading.Thread(target=self._list_models, args=(config,), daemon=True).start()

    def _list_models(self, config: AIConfig):
        try:
            self.models.put((list_models(config), None))
        except Exception as exc:
            self.models.put(([], str(exc)))

    def _poll(self):
        if not self.winfo_exists():
            return
        try:
            models, error = self.models.get_nowait()
        except queue.Empty:
            pass
        else:
            self.check_button.configure(state="normal")
            if error:
                self.check_status.configure(text=error, text_color=RED)
            elif not models:
                self.check_status.configure(text="Connected, but no models are available.", text_color=AMBER)
            else:
                self.model_box.configure(values=models)
                if not self.model_box.get().strip():
                    self.model_box.set(models[0])
                self.check_status.configure(text=f"Connected. {len(models)} models available.", text_color=GREEN)
        self.after(100, self._poll)

    def close(self):
        s = self.app.settings
        config = self.config()
        s.appearance = key_for(APPEARANCES, self.appearance.get(), "system")
        s.cookies_browser = key_for(BROWSER_OPTIONS, self.browser_menu.get(), "")
        s.skip_downloaded = bool(self.skip_check.get())
        s.notify = bool(self.notify_check.get())
        s.watch_clipboard = bool(self.clipboard_check.get())
        s.ai_provider, s.ai_model, s.ai_base_url, s.ai_api_key = (
            config.provider, config.model, config.base_url, config.api_key)
        s.save()
        self.destroy()


class HistoryDialog(ctk.CTkToplevel):
    """Everything downloaded before, searchable."""

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("History")
        self.geometry("780x620")
        self.minsize(620, 440)
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.search = ctk.CTkEntry(self, height=38, placeholder_text="Search your downloads…")
        self.search.grid(row=0, column=0, padx=20, pady=(18, 6), sticky="ew")
        self.search.bind("<KeyRelease>", lambda _: self.refresh())
        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list.grid(row=1, column=0, padx=12, pady=6, sticky="nsew")
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, padx=20, pady=(6, 18), sticky="ew")
        bottom.grid_columnconfigure(1, weight=1)
        outline_button(bottom, "Clear history", self.clear, width=120, height=36).grid(row=0, column=0)
        self.count = ctk.CTkLabel(bottom, text="", text_color=MUTED)
        self.count.grid(row=0, column=1, sticky="w", padx=12)
        ctk.CTkButton(bottom, text="Close", width=100, height=36, command=self.destroy).grid(row=0, column=2)
        self.refresh()

    def refresh(self):
        for child in self.list.winfo_children():
            child.destroy()
        entries = self.app.history.search(self.search.get())
        self.count.configure(text=f"{len(entries)} download{'s' if len(entries) != 1 else ''}")
        if not entries:
            ctk.CTkLabel(self.list, text="Nothing here yet.", text_color=MUTED).pack(pady=40)
            return
        for entry in entries[:150]:
            self._add_row(entry)

    def _add_row(self, entry: HistoryEntry):
        row = ctk.CTkFrame(self.list, corner_radius=8)
        row.pack(fill="x", padx=6, pady=3)
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(row, text=shorten(entry.title, 80), anchor="w", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=12, pady=(8, 0))
        try:
            when = datetime.fromisoformat(entry.date).strftime("%b %d, %Y %H:%M")
        except ValueError:
            when = entry.date
        parts = [when, describe_result(JobResult(files=entry.files, transcripts=entry.transcripts,
                                                 summaries=entry.summaries)), display_path(entry.folder)]
        ctk.CTkLabel(row, text=" · ".join(p for p in parts if p), anchor="w", text_color=MUTED).grid(
            row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        buttons = ctk.CTkFrame(row, fg_color="transparent")
        buttons.grid(row=0, column=1, rowspan=2, padx=8)
        existing = [p for p in entry.outputs if os.path.exists(p)]
        if existing:
            outline_button(buttons, "Show", lambda: desktop.reveal(existing[0]), width=60).pack(side="left", padx=2)
        outline_button(buttons, "Link", lambda: desktop.open_url(entry.url), width=56).pack(side="left", padx=2)
        outline_button(buttons, "Again", lambda: self.app.enqueue_urls([entry.url]), width=60).pack(side="left", padx=2)

    def clear(self):
        if messagebox.askyesno("History", "Clear the whole download history? Your files are kept.", parent=self):
            self.app.history.clear()
            self.refresh()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ctk.set_default_color_theme("blue")
    App().mainloop()
