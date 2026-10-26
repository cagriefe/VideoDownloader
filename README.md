# Video Downloader

A simple desktop app for downloading **videos, music and transcripts** from YouTube and 1000+ other sites.
Built with Python, [customtkinter](https://github.com/TomSchimansky/CustomTkinter) and [yt-dlp](https://github.com/yt-dlp/yt-dlp).

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey)
![License: GPL v3](https://img.shields.io/badge/license-GPLv3-green)

## Features

- **Works with today's YouTube** – powered by yt-dlp instead of the abandoned pytube, and also supports
  [1000+ other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) such as Vimeo, X/Twitter,
  TikTok, SoundCloud and Instagram.
- **Choose the quality** – best available (up to 4K and beyond), 1080p, 720p, 480p, 360p, or **audio only as MP3**
  with title, artist and cover art.
- **Transcripts** – get a readable `.txt` transcript and an `.srt` subtitle file for any video. The app uses the
  video's own subtitles when there are any, and can transcribe the audio on your computer with
  [Whisper](https://github.com/SYSTRAN/faster-whisper) AI when there aren't.
- **Playlists** – download a whole playlist into its own folder, numbered in order. Unavailable videos are skipped
  and reported, and the rest keep downloading.
- **Stays responsive** – downloads run in the background with live progress (size, speed, time left) and can be
  cancelled at any time. Unfinished files never end up in your downloads folder.
- **Saves where you want** – your Downloads folder by default, or any folder you pick. Your choices are remembered.
- Light and dark mode, and plain-English error messages.

## Requirements

- **Python 3.10 or newer** with Tk (included in the installers from [python.org](https://www.python.org/downloads/))
- **[ffmpeg](https://ffmpeg.org)** – strongly recommended. YouTube only offers up to 360p without it, and it's needed
  for MP3 conversion.

Everything else is installed with pip.

## Installation

```bash
git clone https://github.com/cagriefe/VideoDownloader.git
cd VideoDownloader
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Install ffmpeg if you don't have it yet:

| System        | Command                     |
| ------------- | --------------------------- |
| macOS         | `brew install ffmpeg`       |
| Windows       | `winget install Gyan.FFmpeg` |
| Debian/Ubuntu | `sudo apt install ffmpeg`   |

**Optional:** AI transcription for videos without subtitles:

```bash
pip install -r requirements-whisper.txt
```

## Usage

```bash
python main.py
```

1. Paste a link to a video or playlist (or click **Paste**).
2. Choose a **Format** and, if you want one, a **Transcript** option.
3. Tick **Whole playlist** to get every video when the link points to a video inside a playlist.
   Playlist links are always downloaded completely.
4. Press **Download** (or <kbd>Enter</kbd>). **Cancel** (or <kbd>Esc</kbd>) stops at any time.
5. When it's done, click **Show in Finder** / **Show in Explorer** to see the files.

### Formats

| Option                     | What you get                                                 |
| -------------------------- | ------------------------------------------------------------ |
| Best available             | The highest quality video, saved as MP4                      |
| 1080p, 720p, 480p, 360p    | The best video up to that resolution                         |
| Audio only (MP3)           | 192 kbps MP3 with title, artist and cover art                |
| No media (transcript only) | Only the transcript, nothing else is downloaded              |

At any given resolution the app prefers H.264 video with AAC audio, so files play everywhere: QuickTime, Windows,
phones and video editors.

### Transcripts

| Option                    | How it works                                                                              |
| ------------------------- | ----------------------------------------------------------------------------------------- |
| Off                       | No transcript                                                                             |
| Auto (subtitles, then AI) | Uses the video's subtitles (uploaded or auto-generated). If it has none, Whisper transcribes the audio. |
| Subtitles only            | Only uses existing subtitles. Fast, with nothing extra to install.                       |
| AI only (Whisper)         | Always transcribes the audio with Whisper                                                 |

Subtitles in the language that's spoken in the video are preferred. Two files are saved next to the video:

- `<title>.txt` – a readable transcript in paragraphs with timestamps, for example `[02:15] And that's when we…`
- `<title>.srt` – subtitles that players like VLC and IINA load automatically

**AI model** sets the size of the Whisper model. `tiny` and `base` are the fastest, `small` (the default) is a good
balance, and `medium`, `large-v3` and `turbo` are the most accurate but slower. A model is downloaded once, the first
time it's used (75 MB – 3 GB). Transcription runs entirely on your computer and nothing is uploaded.

## Where things are saved

- **Downloads** go to the **Save to** folder. Playlists get a subfolder named after the playlist.
- **Settings** are stored in:
  - macOS: `~/Library/Application Support/VideoDownloader/settings.json`
  - Windows: `%APPDATA%\VideoDownloader\settings.json`
  - Linux: `~/.config/VideoDownloader/settings.json`

## Troubleshooting

| Problem | Solution |
| ------- | -------- |
| **HTTP Error 403**, or YouTube suddenly stops working | YouTube changes often and yt-dlp is updated to keep up. Update it with `pip install -U -r requirements.txt`. |
| **"ffmpeg wasn't found"** | Install ffmpeg (see [Installation](#installation)) and restart the app. |
| **"No JavaScript runtime was found"** | YouTube requires one. `requirements.txt` installs Deno automatically. Alternatively install [Deno](https://deno.com) or [Node.js](https://nodejs.org) 22 or newer. |
| **"Confirm you're not a bot"** | YouTube is rate-limiting your connection. Wait a while, or try another network. |
| **AI transcription is slow** | Pick a smaller **AI model** (`base` or `tiny`). |
| **`No module named 'tkinter'`** | Your Python has no Tk. macOS with Homebrew: `brew install python-tk`. Debian/Ubuntu: `sudo apt install python3-tk`. |

## Development

Run the tests:

```bash
python -m unittest
```

| File             | Purpose                                                                       |
| ---------------- | ----------------------------------------------------------------------------- |
| `main.py`        | The app window                                                                |
| `downloader.py`  | Download engine on top of yt-dlp: formats, progress, playlists, cancellation  |
| `transcriber.py` | Subtitle parsing, Whisper transcription and `.txt` / `.srt` output            |
| `settings.py`    | Remembers your choices between sessions                                       |
| `tests/`         | Unit tests                                                                    |

## Legal

Only download content you own or have permission to download, and respect the terms of service of the sites you use.

## License

Released under the [GNU General Public License v3.0](LICENSE).
