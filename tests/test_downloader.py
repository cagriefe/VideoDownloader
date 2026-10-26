import os
import tempfile
import unittest
from unittest import mock

import yt_dlp

from videodownloader import downloader
from videodownloader.downloader import Cancelled, DownloadJob, DownloadTask, Environment, JobError, Reporter


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
NO_FFMPEG = Environment(ffmpeg=None, js_runtimes={}, whisper=False, ejs=False)


def job(**options):
    return DownloadJob(url="https://example.com/v", output_dir=tempfile.gettempdir(), **options)


class LinkTests(unittest.TestCase):
    def test_valid_links(self):
        self.assertEqual(downloader.normalize_url(" https://www.youtube.com/watch?v=x "),
                         "https://www.youtube.com/watch?v=x")
        self.assertEqual(downloader.normalize_url("youtu.be/abc"), "https://youtu.be/abc")
        self.assertEqual(downloader.normalize_url("http://localhost:8000/v.mp4"), "http://localhost:8000/v.mp4")

    def test_invalid_links(self):
        for text in ("", "   ", "hello", "music 2.0", "2.0", "not a url.com", "ftp://example.com/video", "https://"):
            self.assertIsNone(downloader.normalize_url(text), text)

    def test_extract_urls_from_pasted_text(self):
        text = "Watch <https://youtu.be/a> and\nhttps://vimeo.com/1, also https://youtu.be/a again"
        self.assertEqual(downloader.extract_urls(text), ["https://youtu.be/a", "https://vimeo.com/1"])
        self.assertEqual(downloader.extract_urls("lofi hip hop 2.0"), [])


class TimeTests(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(downloader.parse_time("90"), 90)
        self.assertEqual(downloader.parse_time("1:30"), 90)
        self.assertEqual(downloader.parse_time("1:02:03.5"), 3723.5)
        self.assertEqual(downloader.parse_time("1m30s"), 90)
        self.assertEqual(downloader.parse_time("2h"), 7200)
        self.assertIsNone(downloader.parse_time("  "))
        for bad in ("1:75", "abc", "1:2:3:4", "5x"):
            with self.assertRaises(ValueError, msg=bad):
                downloader.parse_time(bad)

    def test_parse_clip(self):
        self.assertIsNone(downloader.parse_clip("", ""))
        self.assertEqual(downloader.parse_clip("1:30", "2:45"), (90, 165))
        self.assertEqual(downloader.parse_clip("", "2:00"), (0, 120))
        self.assertEqual(downloader.parse_clip("90", ""), (90, None))
        with self.assertRaises(ValueError):
            downloader.parse_clip("2:00", "1:00")

    def test_clip_label(self):
        self.assertEqual(downloader.clip_label(90, 165), "1m30s-2m45s")
        self.assertEqual(downloader.clip_label(3723, None), "1h02m03s-end")
        self.assertEqual(downloader.clip_label(5, 30), "5s-30s")


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

    def test_audio_and_transcript_only(self):
        self.assertEqual(downloader.media_options("mp3", has_ffmpeg=True), {"format": "ba/b"})
        self.assertEqual(downloader.media_options("mp3", has_ffmpeg=False), {"format": "ba[ext=m4a]/ba/b"})
        self.assertEqual(downloader.media_options("none", has_ffmpeg=True), {"format": "ba/b*"})


class PostprocessorTests(unittest.TestCase):
    def keys(self, **options):
        return [step["key"] for step in downloader.postprocessors(job(**options), has_ffmpeg=True)]

    def test_video_embeds_chapters(self):
        self.assertEqual(self.keys(), ["FFmpegMetadata"])

    def test_mp3_gets_tags_and_cover(self):
        steps = downloader.postprocessors(job(quality="mp3"), has_ffmpeg=True)
        self.assertEqual([s["key"] for s in steps], ["FFmpegExtractAudio", "FFmpegMetadata", "EmbedThumbnail"])
        self.assertEqual(steps[0]["preferredcodec"], "mp3")
        self.assertTrue(steps[1]["add_metadata"])

    def test_sponsors_and_chapters_in_yt_dlp_order(self):
        self.assertEqual(self.keys(quality="mp3", remove_sponsors=True, split_chapters=True), [
            "SponsorBlock", "FFmpegExtractAudio", "ModifyChapters", "FFmpegMetadata", "EmbedThumbnail",
            "FFmpegSplitChapters"])

    def test_clips_skip_sponsor_removal_and_splitting(self):
        self.assertEqual(self.keys(remove_sponsors=True, split_chapters=True, clip=(10, 20)), ["FFmpegMetadata"])

    def test_nothing_without_ffmpeg_or_media(self):
        self.assertEqual(downloader.postprocessors(job(quality="mp3"), has_ffmpeg=False), [])
        self.assertEqual(downloader.postprocessors(job(quality="none", remove_sponsors=True), has_ffmpeg=True), [])

    def test_clip_options(self):
        options = downloader.clip_options((90, None))
        self.assertTrue(options["force_keyframes_at_cuts"])
        ranges = list(options["download_ranges"]({"id": "x", "duration": 300}, None))
        self.assertEqual(ranges, [{"start_time": 90, "end_time": float("inf")}])


class OutputTemplateTests(unittest.TestCase):
    def filename(self, templates, kind="default", **info):
        with tempfile.TemporaryDirectory() as home:
            with yt_dlp.YoutubeDL({"outtmpl": templates, "paths": {"home": home}, "quiet": True}) as ydl:
                info = {"id": "abc", "title": "My Video", "ext": "mp4", **info}
                path = ydl.prepare_filename(info, "" if kind == "default" else kind)
            return os.path.relpath(path, home)

    def test_single_video(self):
        templates = downloader.output_templates(job(), playlist=False)
        self.assertEqual(self.filename(templates), "My Video.mp4")
        self.assertEqual(self.filename(templates, playlist_index=3), "003 - My Video.mp4")

    def test_playlist_gets_its_own_folder(self):
        templates = downloader.output_templates(job(), playlist=True)
        name = self.filename(templates, playlist_title="Mix", playlist_index=12)
        self.assertEqual(name, os.path.join("Mix", "012 - My Video.mp4"))

    def test_clip_and_chapter_names(self):
        templates = downloader.output_templates(job(clip=(90, 165)), playlist=False)
        self.assertEqual(self.filename(templates), "My Video [1m30s-2m45s].mp4")
        templates = downloader.output_templates(job(), playlist=False)
        chapter = self.filename(templates, "chapter", section_number=2, section_title="Chorus")
        self.assertEqual(chapter, os.path.join("My Video", "02 - Chorus.mp4"))


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
        self.assertIn("Browser cookies", downloader.friendly_error("ERROR: [youtube] x: Sign in to confirm your age"))


class FormattingTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(downloader.format_bytes(512), "512 B")
        self.assertEqual(downloader.format_bytes(1536), "1.5 KB")
        self.assertEqual(downloader.format_bytes(3 * 1024 ** 3), "3.0 GB")
        self.assertEqual(downloader.format_bytes(None), "?")

    def test_duration_and_counts(self):
        self.assertEqual(downloader.format_duration(75), "1:15")
        self.assertEqual(downloader.format_duration(3725), "1:02:05")
        self.assertEqual(downloader.format_count(999), "999")
        self.assertEqual(downloader.format_count(1500), "1.5K")
        self.assertEqual(downloader.format_count(2_000_000), "2M")
        self.assertEqual(downloader.format_count(None), "")

    def test_progress_text(self):
        self.assertEqual(downloader.describe_progress(50, 100, 2048, 30), "50% · 50 B of 100 B · 2.0 KB/s · 0:30 left")
        self.assertEqual(downloader.describe_progress(1024, None, None, None), "1.0 KB")


class ResultHelperTests(unittest.TestCase):
    def test_iter_videos_flattens_playlists_and_keeps_failures(self):
        result = {"_type": "playlist", "entries": [{"id": "a"}, None, {"_type": "playlist", "entries": [{"id": "b"}]}]}
        self.assertEqual([v and v["id"] for v in downloader.iter_videos(result)], ["a", None, "b"])
        self.assertEqual(list(downloader.iter_videos(None)), [None])

    def test_downloaded_and_chapter_files_must_exist(self):
        with tempfile.NamedTemporaryFile() as media, tempfile.NamedTemporaryFile() as chapter:
            video = {"requested_downloads": [
                {"filepath": media.name, "chapters": [{"filepath": chapter.name}, {"filepath": "/missing.mp3"}]},
                {"filepath": "/missing/file.mp4"}, {}]}
            self.assertEqual(downloader.downloaded_files(video), [media.name])
            self.assertEqual(downloader.chapter_files(video), [chapter.name])
            # yt-dlp may only keep the chapters on the video itself.
            video = {"chapters": [{"filepath": chapter.name}], "requested_downloads": [{"filepath": media.name}]}
            self.assertEqual(downloader.chapter_files(video), [chapter.name])


class SearchTests(unittest.TestCase):
    def test_results_are_simplified(self):
        entries = [
            {"id": "a1", "url": "https://www.youtube.com/watch?v=a1", "title": "First", "channel": "Chan",
             "duration": 61, "view_count": 1200},
            None,
            {"id": "b2", "title": "Second", "uploader": "Up • 6.5B views • 5 seconds ago\n\n\n..."},
        ]
        fake = mock.MagicMock()
        fake.__enter__.return_value.extract_info.return_value = {"entries": entries}
        with mock.patch.object(downloader.yt_dlp, "YoutubeDL", return_value=fake) as constructor:
            results = downloader.search_youtube("lofi", limit=5, cookies_browser="firefox")
        self.assertEqual(constructor.call_args.args[0]["cookiesfrombrowser"], ("firefox",))
        fake.__enter__.return_value.extract_info.assert_called_with("ytsearch5:lofi", download=False)
        self.assertEqual(results, [
            {"url": "https://www.youtube.com/watch?v=a1", "title": "First", "channel": "Chan", "duration": 61,
             "views": 1200},
            {"url": "https://www.youtube.com/watch?v=b2", "title": "Second", "channel": "Up", "duration": None,
             "views": None},
        ])


class DownloadTaskTests(unittest.TestCase):
    def make_task(self, env=ENV, **options):
        reporter = RecordingReporter()
        return DownloadTask(job(**options), reporter, env=env), reporter

    def test_nothing_to_do_is_rejected(self):
        task, _ = self.make_task(quality="none", transcript="off")
        with self.assertRaises(JobError):
            task.run()

    def test_whisper_requires_faster_whisper(self):
        task, _ = self.make_task(transcript="whisper")
        with self.assertRaisesRegex(JobError, "faster-whisper"):
            task.run()

    def test_clips_need_ffmpeg(self):
        task, _ = self.make_task(env=NO_FFMPEG, clip=(1, 2))
        with self.assertRaisesRegex(JobError, "ffmpeg"):
            task.run()

    def test_summary_and_embedding_turn_on_transcripts(self):
        self.assertEqual(self.make_task(summary=True)[0].transcript_mode, "auto")
        self.assertEqual(self.make_task(embed_subtitles=True)[0].transcript_mode, "auto")
        self.assertEqual(self.make_task(transcript="subtitles", summary=True)[0].transcript_mode, "subtitles")
        self.assertEqual(self.make_task()[0].transcript_mode, "off")

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

    def test_archive_skips_are_counted(self):
        logger = downloader._YtDlpLogger()
        logger.debug("[download] jNQXAC9IVRw: has already been recorded in the archive")
        logger.debug("[download] jNQXAC9IVRw: Me at the zoo has already been recorded in the archive")
        logger.debug("[download] \x1b[0;94mabc-123_x\x1b[0m: has already been recorded in the archive")
        logger.debug("[youtube] Extracting URL")
        self.assertEqual(logger.archived, {"jNQXAC9IVRw", "abc-123_x"})

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
        self.assertNotIn("cookiesfrombrowser", options)

    def test_download_options_for_extras(self):
        task, _ = self.make_task(quality="mp3", clip=(5, 25), cookies_browser="chrome", archive_file="/tmp/a.txt")
        task._tempdir = tempfile.gettempdir()
        options = task._download_options()
        self.assertEqual(options["cookiesfrombrowser"], ("chrome",))
        self.assertEqual(options["download_archive"], "/tmp/a.txt")
        self.assertTrue(options["writethumbnail"])
        self.assertIn("download_ranges", options)
        self.assertEqual(options["outtmpl"]["default"], "%(playlist_index&{:03d} - |)s%(title)s [5s-25s].%(ext)s")


if __name__ == "__main__":
    unittest.main()
