"""Video Downloader: download videos, music and transcripts from YouTube and 1000+ other sites."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter
from tkinter import filedialog, messagebox

import customtkinter as ctk

from downloader import (
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
    detect_environment,
    normalize_url,
)
from settings import Settings, default_download_dir

APP_NAME = "Video Downloader"

# (light mode, dark mode) colors
GREEN = ("#1a7f37", "#3fb950")
RED = ("#cf222e", "#ff7b72")
AMBER = ("#9a6700", "#d29922")
MUTED = ("gray40", "gray62")

if sys.platform == "darwin":
    REVEAL_LABEL = "Show in Finder"
elif os.name == "nt":
    REVEAL_LABEL = "Show in Explorer"
else:
    REVEAL_LABEL = "Open folder"


def key_for(mapping: dict, label: str, default: str) -> str:
    return next((key for key, value in mapping.items() if value == label), default)


def display_path(path: str) -> str:
    """Shorten paths in the home folder to ~/… (except on Windows, where that's unusual)."""
    home = os.path.expanduser("~")
    if os.name != "nt" and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def reveal(path: str) -> None:
    """Show a file, or open a folder, in the system file manager."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if os.path.isfile(path) else ["open", path])
    elif os.name == "nt":
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])


class QueueReporter(Reporter):
    """Passes task updates to the GUI thread. Tk widgets must only be touched from that thread."""

    def __init__(self, events: queue.Queue):
        self.events = events

    def status(self, text):
        self.events.put(("status", text))

    def title(self, title, subtitle=""):
        self.events.put(("title", title, subtitle))

    def progress(self, fraction, detail=""):
        self.events.put(("progress", fraction, detail))


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.settings = Settings.load()
        self.env = detect_environment()
        logging.info(
            "yt-dlp %s · ffmpeg: %s · JavaScript runtimes: %s · Whisper: %s",
            YTDLP_VERSION, self.env.ffmpeg or "not found", ", ".join(self.env.js_runtimes) or "none",
            "installed" if self.env.whisper else "not installed",
        )
        self.events: queue.Queue = queue.Queue()
        self.task: DownloadTask | None = None
        self.last_output: str | None = None
        self._indeterminate = False
        self._wrap = 0

        self.title(APP_NAME)
        self.geometry("760x640")
        self.minsize(700, 600)
        self._build()
        self._set_wrap(760)
        self._load_settings()
        self._show_ready()

        # add="+" keeps customtkinter's own <Configure> handler working.
        self.bind("<Configure>", self._on_resize, add="+")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._process_events)

    # -- layout ----------------------------------------------------------------

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=24, pady=(22, 10), sticky="ew")
        ctk.CTkLabel(header, text=APP_NAME, font=ctk.CTkFont(size=26, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text="Download videos, music and transcripts from YouTube and 1000+ other sites.",
            text_color=MUTED,
        ).pack(anchor="w")

        link_row = ctk.CTkFrame(self, fg_color="transparent")
        link_row.grid(row=1, column=0, padx=24, pady=6, sticky="ew")
        link_row.grid_columnconfigure(0, weight=1)
        self.url_entry = ctk.CTkEntry(link_row, height=42, placeholder_text="Paste a video or playlist link…")
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", self.start_download)
        self.paste_button = ctk.CTkButton(link_row, text="Paste", width=90, height=42, command=self.paste_link)
        self.paste_button.grid(row=0, column=1, padx=(8, 0))

        options = ctk.CTkFrame(self)
        options.grid(row=2, column=0, padx=24, pady=10, sticky="ew")
        options.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(options, text="Format").grid(row=0, column=0, padx=(16, 12), pady=(14, 6), sticky="w")
        self.quality_menu = ctk.CTkOptionMenu(
            options, values=list(QUALITY_PRESETS.values()), width=240, dynamic_resizing=False,
            command=self._on_quality_change,
        )
        self.quality_menu.grid(row=0, column=1, pady=(14, 6), sticky="w")
        self.playlist_check = ctk.CTkCheckBox(options, text="Whole playlist", command=self._save_settings)
        self.playlist_check.grid(row=0, column=2, padx=16, pady=(14, 6), sticky="w")

        ctk.CTkLabel(options, text="Transcript").grid(row=1, column=0, padx=(16, 12), pady=6, sticky="w")
        self.transcript_menu = ctk.CTkOptionMenu(
            options, values=list(TRANSCRIPT_MODES.values()), width=240, dynamic_resizing=False,
            command=self._on_transcript_change,
        )
        self.transcript_menu.grid(row=1, column=1, pady=6, sticky="w")
        model_box = ctk.CTkFrame(options, fg_color="transparent")
        model_box.grid(row=1, column=2, padx=16, pady=6, sticky="w")
        ctk.CTkLabel(model_box, text="AI model").pack(side="left", padx=(0, 8))
        self.model_menu = ctk.CTkOptionMenu(
            model_box, values=list(WHISPER_MODELS), width=110, dynamic_resizing=False,
            command=lambda _: self._save_settings(),
        )
        self.model_menu.pack(side="left")

        ctk.CTkLabel(options, text="Save to").grid(row=2, column=0, padx=(16, 12), pady=(6, 14), sticky="w")
        self.folder_entry = ctk.CTkEntry(options)
        self.folder_entry.grid(row=2, column=1, pady=(6, 14), sticky="ew")
        self.folder_entry.bind("<FocusOut>", lambda _: self._save_settings())
        self.browse_button = ctk.CTkButton(options, text="Browse…", width=110, command=self.choose_folder)
        self.browse_button.grid(row=2, column=2, padx=16, pady=(6, 14), sticky="w")

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=3, column=0, padx=24, pady=(6, 4), sticky="ew")
        actions.grid_columnconfigure(0, weight=1)
        self.download_button = ctk.CTkButton(
            actions, text="Download", height=44, font=ctk.CTkFont(size=15, weight="bold"),
            command=self.start_download,
        )
        self.download_button.grid(row=0, column=0, sticky="ew")
        self.cancel_button = ctk.CTkButton(
            actions, text="Cancel", width=120, height=44, fg_color="transparent", border_width=2,
            text_color=("gray10", "gray90"), state="disabled", command=self.cancel_download,
        )
        self.cancel_button.grid(row=0, column=1, padx=(8, 0))

        activity = ctk.CTkFrame(self, fg_color="transparent")
        activity.grid(row=4, column=0, padx=24, pady=(14, 20), sticky="nsew")
        activity.grid_columnconfigure(0, weight=1)
        self.title_label = ctk.CTkLabel(
            activity, text="", font=ctk.CTkFont(size=16, weight="bold"), anchor="w", justify="left",
        )
        self.title_label.grid(row=0, column=0, sticky="ew")
        self.subtitle_label = ctk.CTkLabel(activity, text="", text_color=MUTED, anchor="w", justify="left")
        self.subtitle_label.grid(row=1, column=0, sticky="ew")
        self.progress_bar = ctk.CTkProgressBar(activity, height=10)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=2, column=0, sticky="ew", pady=(10, 4))
        self.progress_label = ctk.CTkLabel(activity, text="", text_color=MUTED, anchor="w")
        self.progress_label.grid(row=3, column=0, sticky="ew")
        self.status_label = ctk.CTkLabel(activity, text="", anchor="w", justify="left")
        self.status_label.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        self.reveal_button = ctk.CTkButton(activity, text=REVEAL_LABEL, width=150, command=self.reveal_output)
        self.reveal_button.grid(row=5, column=0, sticky="w", pady=(12, 0))
        self.reveal_button.grid_remove()

        self.bind("<Escape>", lambda _: self.cancel_download())

    # -- settings ----------------------------------------------------------------

    def _load_settings(self):
        s = self.settings
        self.quality_menu.set(QUALITY_PRESETS.get(s.quality, QUALITY_PRESETS["best"]))
        self.transcript_menu.set(TRANSCRIPT_MODES.get(s.transcript, TRANSCRIPT_MODES["off"]))
        self.model_menu.set(s.whisper_model if s.whisper_model in WHISPER_MODELS else "small")
        if s.playlist:
            self.playlist_check.select()
        self._set_folder(s.download_dir)
        self._update_model_state()

    def _save_settings(self):
        self.settings.quality = self._quality()
        self.settings.transcript = self._transcript()
        self.settings.whisper_model = self.model_menu.get()
        self.settings.playlist = bool(self.playlist_check.get())
        if os.path.isdir(self._folder()):
            self.settings.download_dir = self._folder()
        self.settings.save()

    def _quality(self) -> str:
        return key_for(QUALITY_PRESETS, self.quality_menu.get(), "best")

    def _transcript(self) -> str:
        return key_for(TRANSCRIPT_MODES, self.transcript_menu.get(), "off")

    def _folder(self) -> str:
        text = self.folder_entry.get().strip()
        return os.path.abspath(os.path.expanduser(text)) if text else default_download_dir()

    def _set_folder(self, path: str):
        self.folder_entry.delete(0, "end")
        self.folder_entry.insert(0, display_path(path))

    # -- user actions --------------------------------------------------------------

    def paste_link(self):
        try:
            text = self.clipboard_get().strip()
        except tkinter.TclError:
            text = ""
        if text:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, text)

    def choose_folder(self):
        current = self._folder()
        folder = filedialog.askdirectory(
            parent=self, title="Choose where to save downloads",
            initialdir=current if os.path.isdir(current) else default_download_dir(),
        )
        if folder:
            self._set_folder(folder)
            self._save_settings()

    def _on_quality_change(self, _label):
        if self._quality() == "none" and self._transcript() == "off":
            self.transcript_menu.set(TRANSCRIPT_MODES["auto"])
        self._update_model_state()
        self._save_settings()

    def _on_transcript_change(self, _label):
        if self._transcript() == "whisper" and not self.env.whisper:
            self._set_status(WHISPER_MISSING, AMBER)
        self._update_model_state()
        self._save_settings()

    def _update_model_state(self):
        uses_ai = self.env.whisper and self._transcript() in ("auto", "whisper")
        self.model_menu.configure(state="normal" if uses_ai else "disabled")

    def start_download(self, _event=None):
        if self.task is not None:
            return
        url = normalize_url(self.url_entry.get())
        if not url:
            self._set_status("Paste a link to a video or playlist first (for example https://youtu.be/…).", RED)
            return
        quality, transcript = self._quality(), self._transcript()
        if quality == "none" and transcript == "off":
            self._set_status("Choose a transcript option, or pick a video or audio format to download.", RED)
            return
        if transcript == "whisper" and not self.env.whisper:
            self._set_status(WHISPER_MISSING, RED)
            return

        folder = self._folder()
        self._set_folder(folder)
        self._save_settings()
        job = DownloadJob(
            url=url, output_dir=folder, quality=quality, transcript=transcript,
            whisper_model=self.model_menu.get(), playlist=bool(self.playlist_check.get()),
        )
        self.task = DownloadTask(job, QueueReporter(self.events), env=self.env)
        self.last_output = None
        self.reveal_button.grid_remove()
        self.title_label.configure(text="")
        self.subtitle_label.configure(text="")
        self._set_progress(None, "")
        self._set_status("Starting…")
        self._set_busy(True)
        threading.Thread(target=self._run_task, args=(self.task,), daemon=True).start()

    def cancel_download(self):
        if self.task is None:
            return
        self.task.cancel()
        self.cancel_button.configure(state="disabled")
        self._set_status("Cancelling…")

    def reveal_output(self):
        if self.last_output and os.path.exists(self.last_output):
            reveal(self.last_output)
        else:
            reveal(self._folder())

    def _on_close(self):
        if self.task is not None:
            if not messagebox.askyesno(APP_NAME, "A download is still running. Stop it and quit?", parent=self):
                return
            self.task.cancel()
        self._save_settings()
        self.destroy()

    # -- background work -------------------------------------------------------------

    def _run_task(self, task: DownloadTask):
        try:
            result = task.run()
        except Cancelled:
            self.events.put(("cancelled",))
        except JobError as exc:
            self.events.put(("failed", str(exc)))
        except Exception as exc:
            logging.exception("Download failed")
            self.events.put(("failed", f"Something went wrong: {exc}"))
        else:
            self.events.put(("finished", result))

    def _process_events(self):
        try:
            while True:
                kind, *args = self.events.get_nowait()
                getattr(self, f"_on_{kind}")(*args)
        except queue.Empty:
            pass
        finally:
            self.after(80, self._process_events)

    def _on_status(self, text: str):
        self._set_status(text)

    def _on_title(self, title: str, subtitle: str):
        self.title_label.configure(text=title)
        self.subtitle_label.configure(text=subtitle)

    def _on_progress(self, fraction: float | None, detail: str):
        self._set_progress(fraction, detail)

    def _on_finished(self, result: JobResult):
        self._task_done()
        self._set_progress(1.0, "")
        parts = []
        if len(result.files) == 1:
            parts.append(f"Saved “{os.path.basename(result.files[0])}”")
        elif result.files:
            parts.append(f"Saved {len(result.files)} files")
        transcripts = sum(1 for path in result.transcripts if path.endswith(".txt"))
        if transcripts:
            parts.append("transcript created" if transcripts == 1 else f"{transcripts} transcripts created")
        message = (" · ".join(parts) + ".") if parts else "Nothing was saved."
        message = message[0].upper() + message[1:]
        notes = result.notes
        for note in notes:
            logging.info(note)
        if len(notes) > 3:
            notes = notes[:2] + [f"…and {len(notes) - 2} more notes (listed in the terminal)."]
        if notes:
            message += "\n" + "\n".join(notes)
        self._set_status(message, GREEN if parts and not result.notes else AMBER)
        outputs = result.files + result.transcripts
        if outputs:
            self.last_output = outputs[0]
            self.reveal_button.grid()

    def _on_failed(self, message: str):
        self._task_done()
        self._set_progress(0.0, "")
        self._set_status(message, RED)

    def _on_cancelled(self):
        self._task_done()
        self._set_progress(0.0, "")
        self._set_status("Download cancelled.", AMBER)

    def _task_done(self):
        self.task = None
        self._set_busy(False)
        self._save_settings()  # the download folder may have just been created

    # -- helpers ------------------------------------------------------------------------

    def _show_ready(self):
        problems = []
        if not self.env.ffmpeg:
            problems.append("ffmpeg wasn't found, so quality is limited and MP3 conversion is off.")
        if not self.env.js_runtimes:
            problems.append("No JavaScript runtime (Deno or Node.js) was found, so YouTube downloads may fail.")
        if problems:
            self._set_status(" ".join(problems) + " See the README for setup help.", AMBER)
        else:
            self._set_status("Ready. Paste a link and press Download.", MUTED)

    def _set_status(self, text: str, color=None):
        self.status_label.configure(text=text, text_color=color or ctk.ThemeManager.theme["CTkLabel"]["text_color"])

    def _set_progress(self, fraction: float | None, detail: str):
        if fraction is None:
            if not self._indeterminate:
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start()
                self._indeterminate = True
        else:
            if self._indeterminate:
                self.progress_bar.stop()
                self.progress_bar.configure(mode="determinate")
                self._indeterminate = False
            self.progress_bar.set(max(0.0, min(1.0, fraction)))
        self.progress_label.configure(text=detail)

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for widget in (self.url_entry, self.paste_button, self.quality_menu, self.transcript_menu,
                       self.playlist_check, self.folder_entry, self.browse_button, self.download_button):
            widget.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")
        if busy:
            self.model_menu.configure(state="disabled")
        else:
            self._update_model_state()

    def _on_resize(self, event):
        if event.widget is self and event.width > 1:
            self._set_wrap(event.width / ctk.ScalingTracker.get_widget_scaling(self))

    def _set_wrap(self, window_width: float):
        """Wrap long labels to the window width (in unscaled pixels)."""
        width = max(300, int(window_width) - 60)
        if width != self._wrap:
            self._wrap = width
            for label in (self.title_label, self.subtitle_label, self.status_label):
                label.configure(wraplength=width)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    App().mainloop()


if __name__ == "__main__":
    main()
