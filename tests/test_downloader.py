import os
import tempfile
import unittest

import yt_dlp

import downloader
from downloader import Cancelled, DownloadJob, DownloadTask, Environment, JobError, Reporter


class RecordingReporter(Reporter):
    def __init__(self):
        self.calls = []

    def status(self, text):
        self.calls.append(("status", text))

    def title(self, title, subtitle=""):
        self.calls.append(("title", title, subtitle))

    def progress(self, fraction, detail=""):
        self.calls.append(("progress", fraction, detail))


ENV = Environment(ffmpeg="/usr/bin/ffmpeg", js_runtimes={"deno": "/usr/bin/deno"}, whisper=False, ejs=True)


class NormalizeUrlTests(unittest.TestCase):
    def test_valid_links(self):
        self.assertEqual(downloader.normalize_url(" https://www.youtube.com/watch?v=x "),
                         "https://www.youtube.com/watch?v=x")
        self.assertEqual(downloader.normalize_url("youtu.be/abc"), "https://youtu.be/abc")

    def test_invalid_links(self):
        for text in ("", "   ", "hello", "not a url.com", "ftp://example.com/video", "https://"):
            self.assertIsNone(downloader.normalize_url(text), text)


class MediaOptionsTests(unittest.TestCase):
    def test_video_with_ffmpeg_merges_best_streams(self):
        options = downloader.media_options("best", has_ffmpeg=True)
        self.assertEqual(options["format"], "bv*+ba/b")
        self.assertEqual(options["format_sort"], ["res", "+codec:avc:m4a"])
        self.assertEqual(options["merge_output_format"], "mp4")

    def test_resolution_cap(self):
        self.assertEqual(downloader.media_options("720", has_ffmpeg=True)["format_sort"][0], "res:720")

    def test_video_without_ffmpeg_uses_single_files(self):
        options = downloader.media_options("1080", has_ffmpeg=False)
        self.assertEqual(options["format"], "b")
        self.assertNotIn("merge_output_format", options)

    def test_mp3(self):
        options = downloader.media_options("mp3", has_ffmpeg=True)
        self.assertEqual(options["postprocessors"][0]["preferredcodec"], "mp3")
        self.assertNotIn("postprocessors", downloader.media_options("mp3", has_ffmpeg=False))

    def test_transcript_only_accepts_any_format(self):
        self.assertEqual(downloader.media_options("none", has_ffmpeg=True), {"format": "ba/b*"})

    def test_every_preset_is_handled(self):
        for quality in downloader.QUALITY_PRESETS:
            for has_ffmpeg in (True, False):
                self.assertIn("format", downloader.media_options(quality, has_ffmpeg))


class OutputTemplateTests(unittest.TestCase):
    def filename(self, template, **info):
        with tempfile.TemporaryDirectory() as home:
            with yt_dlp.YoutubeDL({"outtmpl": template, "paths": {"home": home}, "quiet": True}) as ydl:
                path = ydl.prepare_filename({"id": "abc", "title": "My Video", "ext": "mp4", **info})
            return os.path.relpath(path, home)

    def test_single_video(self):
        self.assertEqual(self.filename(downloader.SINGLE_TEMPLATE), "My Video.mp4")
        self.assertEqual(self.filename(downloader.SINGLE_TEMPLATE, playlist_index=3), "003 - My Video.mp4")

    def test_playlist_gets_its_own_folder(self):
        name = self.filename(downloader.PLAYLIST_TEMPLATE, playlist_title="Mix", playlist_index=12)
        self.assertEqual(name, os.path.join("Mix", "012 - My Video.mp4"))


class FriendlyErrorTests(unittest.TestCase):
    def test_strips_prefixes_and_colors(self):
        message = "\x1b[0;31mERROR:\x1b[0m [youtube] dQw4w9WgXcQ: Video unavailable. This video is private"
        self.assertEqual(downloader.friendly_error(message), "Video unavailable. This video is private")

    def test_strips_command_line_hints(self):
        message = ("ERROR: [youtube] yi50KlsCBio: Private video. If the owner of this video has granted you "
                   "access, please sign in. Use --cookies-from-browser or --cookies for the authentication. "
                   "See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ for how to manually pass cookies")
        self.assertEqual(downloader.friendly_error(message),
                         "Private video. If the owner of this video has granted you access, please sign in.")
        self.assertEqual(
            downloader.friendly_error("ERROR: [generic] x: Unable to download webpage: HTTP Error 404: Not Found "
                                      "(caused by <HTTPError 404: Not Found>)"),
            "Unable to download webpage: HTTP Error 404: Not Found")

    def test_known_problems(self):
        self.assertIn("isn't supported", downloader.friendly_error("ERROR: Unsupported URL: https://example.com"))
        self.assertIn("not a bot", downloader.friendly_error("ERROR: [youtube] x1: Sign in to confirm you're not a bot"))
        self.assertIn("Network error", downloader.friendly_error("ERROR: <urlopen error [Errno 8] nodename nor servname provided>"))
        self.assertIn("ffmpeg", downloader.friendly_error("ERROR: [x] y: Requested format is not available", has_ffmpeg=False))
        self.assertIn("pip install -U", downloader.friendly_error("ERROR: unable to download video data: HTTP Error 403: Forbidden"))


class FormattingTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(downloader.format_bytes(512), "512 B")
        self.assertEqual(downloader.format_bytes(1536), "1.5 KB")
        self.assertEqual(downloader.format_bytes(3 * 1024 ** 3), "3.0 GB")
        self.assertEqual(downloader.format_bytes(None), "?")

    def test_duration(self):
        self.assertEqual(downloader.format_duration(75), "1:15")
        self.assertEqual(downloader.format_duration(3725), "1:02:05")

    def test_progress_text(self):
        self.assertEqual(downloader.describe_progress(50, 100, 2048, 30), "50% · 50 B of 100 B · 2.0 KB/s · 0:30 left")
        self.assertEqual(downloader.describe_progress(1024, None, None, None), "1.0 KB")


class ResultHelperTests(unittest.TestCase):
    def test_iter_videos_flattens_playlists_and_keeps_failures(self):
        result = {"_type": "playlist", "entries": [{"id": "a"}, None, {"_type": "playlist", "entries": [{"id": "b"}]}]}
        self.assertEqual([v and v["id"] for v in downloader.iter_videos(result)], ["a", None, "b"])
        self.assertEqual(list(downloader.iter_videos(None)), [None])

    def test_downloaded_files_only_lists_existing_files(self):
        with tempfile.NamedTemporaryFile() as existing:
            video = {"requested_downloads": [{"filepath": existing.name}, {"filepath": "/missing/file.mp4"}, {}]}
            self.assertEqual(downloader.downloaded_files(video), [existing.name])


class DownloadTaskTests(unittest.TestCase):
    def make_task(self, **job):
        reporter = RecordingReporter()
        job = DownloadJob(url="https://example.com/v", output_dir=tempfile.gettempdir(), **job)
        return DownloadTask(job, reporter, env=ENV), reporter

    def test_nothing_to_do_is_rejected(self):
        task, _ = self.make_task(quality="none", transcript="off")
        with self.assertRaises(JobError):
            task.run()

    def test_whisper_requires_faster_whisper(self):
        task, _ = self.make_task(transcript="whisper")
        with self.assertRaisesRegex(JobError, "faster-whisper"):
            task.run()

    def test_progress_updates(self):
        task, reporter = self.make_task()
        task._on_progress({
            "status": "downloading", "downloaded_bytes": 25, "total_bytes": 100, "speed": None, "eta": 3,
            "info_dict": {"id": "v1", "title": "Clip", "uploader": "Someone", "vcodec": "avc1", "acodec": "none"},
        })
        self.assertIn(("title", "Clip", "Someone"), reporter.calls)
        self.assertIn(("status", "Downloading video…"), reporter.calls)
        self.assertIn(("progress", 0.25, "25% · 25 B of 100 B · 0:03 left"), reporter.calls)

    def test_playlist_items_are_labelled(self):
        task, reporter = self.make_task()
        task._match_filter({"id": "v2", "title": "Song", "playlist_index": 2, "n_entries": 5, "playlist_title": "Mix"})
        self.assertIn(("title", "Mix", "Video 2 of 5 · Song"), reporter.calls)

    def test_cancel_stops_at_next_callback(self):
        task, _ = self.make_task()
        task.cancel()
        with self.assertRaises(Cancelled):
            task._on_progress({"status": "downloading", "info_dict": {}})
        with self.assertRaises(Cancelled):
            task._match_filter({"id": "x"}, incomplete=True)

    def test_options_use_detected_tools(self):
        task, _ = self.make_task()
        task._tempdir = tempfile.gettempdir()
        options = task._base_options()
        self.assertEqual(options["ffmpeg_location"], "/usr/bin/ffmpeg")
        self.assertEqual(options["js_runtimes"], {"deno": {"path": "/usr/bin/deno"}})
        self.assertTrue(options["noplaylist"])
        self.assertNotIn("remote_components", options)


if __name__ == "__main__":
    unittest.main()
