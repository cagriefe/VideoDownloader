# Video Downloader

A desktop app for downloading **videos, music, transcripts and AI summaries** from YouTube and 1000+ other sites.
Built with Python, [customtkinter](https://github.com/TomSchimansky/CustomTkinter) and [yt-dlp](https://github.com/yt-dlp/yt-dlp).

[![Tests](https://github.com/cagriefe/VideoDownloader/actions/workflows/tests.yml/badge.svg)](https://github.com/cagriefe/VideoDownloader/actions/workflows/tests.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey)
![License: GPL v3](https://img.shields.io/badge/license-GPLv3-green)

## Features

**Downloading**

- **Download queue**: paste one link or many at once. They're downloaded one after another, each with its own
  progress, and you can cancel, retry, play or show any of them.
- **Search YouTube from the app**: type a few words instead of a link and pick the videos you want from the results.
- **Any quality**: best available (4K and beyond), 1080p, 720p, 480p, 360p, or **MP3** with title, artist and cover art.
- **Clips**: download only part of a video, e.g. from 1:30 to 2:45, cut exactly.
- **Playlists** go into their own folder, numbered in order. Unavailable videos are skipped and reported.
- **Works with 1000+ sites**: Vimeo, X/Twitter, TikTok, SoundCloud, Instagram and
  [many more](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

**Transcripts and AI**

- **Transcripts**: a readable `.txt` with timestamps plus an `.srt` subtitle file. They come from the video's own
  subtitles, or from [Whisper](https://github.com/SYSTRAN/faster-whisper) speech recognition running on your computer.
- **Translation**: get the transcript in another language, such as Turkish subtitles for an English video
  (YouTube's translations, or Whisper for English).
- **Embedded subtitles**: add the transcript to the video file as a subtitle track you can switch on in any player.
- **AI summaries**: a TL;DR, key points and a timestamped outline in a `.summary.md` file, where each timestamp
  links to that moment of the video. You can use a free local model with [Ollama](https://ollama.com), or OpenAI,
  Claude, or any OpenAI-compatible server.

**Extras**

- **Remove sponsors**: cuts sponsor reads, self-promotion and "like and subscribe" reminders out of YouTube videos,
  using the community database of [SponsorBlock](https://sponsor.ajay.app).
- **Split into chapters**: turns a full album or a podcast into separate files, one per chapter.
- **Chapters are embedded** in downloaded videos, so you can jump between them in your player.
- **History** of everything you downloaded, searchable, with one click to show, open or download again.
- **Browser cookies** for age-restricted and members-only videos, or when YouTube asks if you're a bot.
- **Skip videos you've already downloaded**, which is handy for keeping a playlist or channel in sync.
- **Notifications** when downloads finish, and links you copy are filled in automatically.
- **Command line** with the same features, for scripts and automation.
- Light and dark mode, and plain-English error messages. Unfinished files never end up in your downloads folder.

## Requirements

- **Python 3.10 or newer** with Tk (included in the installers from [python.org](https://www.python.org/downloads/))
- **[ffmpeg](https://ffmpeg.org)**, strongly recommended. Without it YouTube only offers up to 360p, and MP3s, clips,
  sponsor removal, chapters and embedded subtitles are unavailable.

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

| System        | Command                      |
| ------------- | ---------------------------- |
| macOS         | `brew install ffmpeg`        |
| Windows       | `winget install Gyan.FFmpeg` |
| Debian/Ubuntu | `sudo apt install ffmpeg`    |

**Optional:** to transcribe videos that have no subtitles, add Whisper:

```bash
pip install -r requirements-whisper.txt
```

## Using the app

```bash
python main.py
```

1. Paste one or more links (or click **Paste**), or type words to search YouTube.
2. Choose the options you want (see below). They're remembered for next time.
3. Press **Download** or <kbd>Enter</kbd>. Downloads appear in the queue. <kbd>Esc</kbd> cancels everything.
4. When a download is done, use **Play**, **Summary** or **Show** on its row.

| Option          | What it does                                                                                      |
| --------------- | ------------------------------------------------------------------------------------------------- |
| Format          | Video quality, *Audio only (MP3)*, or *No media* when you only want a transcript or summary         |
| Transcript      | *Auto* uses subtitles, or Whisper if there are none. *Subtitles only* or *AI only (Whisper)* use just one source. |
| Language        | Language of the transcript and summary. *As spoken* keeps the original                            |
| AI model        | Size of the Whisper model: `tiny`/`base` are fastest, `small` is a good balance, `large-v3`/`turbo` are the most accurate |
| Whole playlist  | Get every video when the link points to a video inside a playlist (playlist links always get all of them) |
| Clip            | Start and end time, e.g. `1:30` to `2:45`. Leave empty for the whole video                        |
| Remove sponsors | Cut out sponsor segments (YouTube)                                                                |
| Split into chapters | Also save each chapter as its own file, in a folder named after the video                      |
| Embed subtitles | Put the transcript into the video file as a subtitle track                                        |
| AI summary      | Write `<title>.summary.md` (set up the AI service in **Settings** first)                          |

### Transcripts and translations

For each video the app saves:

- `<title>.txt`: the transcript in paragraphs with timestamps, e.g. `[02:15] And that's when we…`
- `<title>.srt`: subtitles that players like VLC and IINA load automatically

With *Auto*, uploaded subtitles in the spoken language are preferred, then YouTube's automatic captions, then Whisper.
When you choose a **Language**, the app uses subtitles in that language or YouTube's machine translation. Whisper can
translate into English. If a translation isn't available, you get the original language and a note explaining why.

Whisper runs on your computer and nothing is uploaded. A model is downloaded once, the first time it's used
(75 MB – 3 GB).

### AI summaries

Choose the AI service in **Settings → AI summaries**, then tick **AI summary**:

| Service                  | Setup                                                                                          |
| ------------------------ | ---------------------------------------------------------------------------------------------- |
| Ollama (on this computer) | Free and private. Install [Ollama](https://ollama.com), then `ollama pull llama3.2` (or any model) |
| OpenAI                   | Paste an API key from [platform.openai.com](https://platform.openai.com), or set `OPENAI_API_KEY` |
| Anthropic Claude         | Paste an API key from [console.anthropic.com](https://console.anthropic.com), or set `ANTHROPIC_API_KEY` |
| OpenAI-compatible server | LM Studio, llama.cpp, vLLM…: enter the server address, e.g. `http://localhost:1234/v1`         |

**Check connection** verifies the setup and lists the available models. Long videos are summarized in parts first,
so even small local models can handle hour-long videos. With cloud services a summary usually costs well under a
cent. The API key is stored in the settings file, which only your user account can read.

## Command line

Run `python main.py` with a link to download without opening the window. It uses the same settings as the app.

```bash
python main.py https://youtu.be/dQw4w9WgXcQ                    # best quality video
python main.py URL -f mp3 --split-chapters                     # album → MP3 per track
python main.py URL -f none -t auto -l tr --summary             # Turkish transcript + AI summary only
python main.py URL -f 720 --clip 1:30-2:45 -t auto --embed-subs
python main.py URL1 URL2 URL3 --skip-downloaded                # only what's new
python main.py --search "lofi hip hop"                         # list YouTube results
python main.py --search "lofi hip hop" --pick 1 -f mp3         # download the first one
```

Run `python main.py --help` for all options. The exit code is 0 when everything worked and 1 if something failed.

## Where things are saved

- **Downloads** go to the **Save to** folder (your Downloads folder by default). Playlists get a subfolder.
- **Settings, history and the list of downloaded videos** are stored in:
  - macOS: `~/Library/Application Support/VideoDownloader/`
  - Windows: `%APPDATA%\VideoDownloader\`
  - Linux: `~/.config/VideoDownloader/`

## Troubleshooting

| Problem | Solution |
| ------- | -------- |
| **HTTP Error 403**, or YouTube suddenly stops working | YouTube changes often and yt-dlp is updated to keep up. Update it with `pip install -U -r requirements.txt`. |
| **"Confirm you're not a bot"** or an age-restricted video | Choose your browser under **Settings → Browser cookies**. Your browser or keychain may ask for permission once. |
| **The translation came out in the original language** | YouTube limits how often its translations can be downloaded. Wait a while, or sign in with browser cookies. |
| **"ffmpeg wasn't found"** | Install ffmpeg (see [Installation](#installation)) and restart the app. |
| **"No JavaScript runtime was found"** | YouTube requires one. `requirements.txt` installs Deno automatically. Alternatively install [Deno](https://deno.com) or [Node.js](https://nodejs.org) 22 or newer. |
| **AI summaries fail** | Use **Settings → Check connection**. For Ollama, make sure it's running and a model is downloaded. |
| **AI transcription is slow** | Pick a smaller **AI model** (`base` or `tiny`). |
| **`No module named 'tkinter'`** | Your Python has no Tk. macOS with Homebrew: `brew install python-tk`. Debian/Ubuntu: `sudo apt install python3-tk`. |

## Development

```bash
python -m unittest        # run the tests
python -m videodownloader # same as python main.py
```

| Module                          | Purpose                                                                      |
| ------------------------------- | ---------------------------------------------------------------------------- |
| `videodownloader/gui.py`        | The window: queue, search, history and settings                              |
| `videodownloader/cli.py`        | The command line                                                             |
| `videodownloader/downloader.py` | Download engine on top of yt-dlp: formats, clips, playlists, SponsorBlock, chapters |
| `videodownloader/transcriber.py`| Subtitles, translations, Whisper, embedded subtitles and `.txt`/`.srt` output |
| `videodownloader/summarizer.py` | AI summaries with Ollama, OpenAI, Claude or OpenAI-compatible servers        |
| `videodownloader/history.py`    | Download history                                                             |
| `videodownloader/settings.py`   | Remembers your choices                                                       |
| `videodownloader/desktop.py`    | File manager, default apps and notifications                                 |

Tests run on Linux, macOS and Windows with GitHub Actions on every push.

## Legal

Only download content you own or have permission to download, and respect the terms of service of the sites you use.

## License

Released under the [GNU General Public License v3.0](LICENSE).
