import json
import os
import shutil
import subprocess
import tempfile
import unittest

from videodownloader import transcriber
from videodownloader.transcriber import Segment, Transcript

# Trimmed copy of a real YouTube auto-caption file: every cue repeats the
# previous line, and whitespace-only lines belong to the cue.
YOUTUBE_AUTO_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.320 --> 00:00:18.790 align:start position:0%
 
[Music]

00:00:18.790 --> 00:00:18.800 align:start position:0%
 
 

00:00:18.800 --> 00:00:21.790 align:start position:0%
 
We're<00:00:19.039><c> no</c><00:00:19.359><c> strangers</c><00:00:19.840><c> to</c>

00:00:21.790 --> 00:00:21.800 align:start position:0%
We're no strangers to
 

00:00:21.800 --> 00:00:25.950 align:start position:0%
We're no strangers to
love.<00:00:22.800><c> You</c><00:00:23.039><c> know</c><00:00:23.279><c> the</c><00:00:23.600><c> rules</c>
"""

YOUTUBE_JSON3 = json.dumps({"events": [
    {"tStartMs": 0, "dDurationMs": 211879, "id": 1},
    {"tStartMs": 320, "dDurationMs": 14260, "segs": [{"utf8": "[Music]"}]},
    {"tStartMs": 18790, "aAppend": 1, "segs": [{"utf8": "\n"}]},
    {"tStartMs": 18800, "dDurationMs": 7160, "segs": [
        {"utf8": "We're"}, {"utf8": " no", "tOffsetMs": 239}, {"utf8": " strangers"}, {"utf8": " to"}]},
    {"tStartMs": 21800, "dDurationMs": 7319, "segs": [{"utf8": "love."}, {"utf8": " You know"}]},
]})

FMT = [{"ext": "vtt", "url": "https://example.com/subs.vtt"}]


class ParseTimedTextTests(unittest.TestCase):
    def test_youtube_rolling_captions_are_deduplicated(self):
        segments = transcriber.parse_timed_text(YOUTUBE_AUTO_VTT)
        self.assertEqual([s.text for s in segments], ["[Music]", "We're no strangers to", "love. You know the rules"])
        self.assertAlmostEqual(segments[1].start, 18.8)
        self.assertAlmostEqual(segments[1].end, 21.79)

    def test_srt_with_tags_and_entities(self):
        srt = ("1\r\n00:00:01,000 --> 00:00:02,500\r\nHello <i>world</i>\r\n\r\n"
               "2\r\n00:00:03,000 --> 00:00:04,000\r\n{\\an8}Second &amp; last\r\nline\r\n")
        segments = transcriber.parse_timed_text(srt)
        self.assertEqual(
            [(s.start, s.end, s.text) for s in segments],
            [(1.0, 2.5, "Hello world"), (3.0, 4.0, "Second & last line")],
        )

    def test_cues_without_blank_lines_between_them(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\nOne\n2\n00:00:02,000 --> 00:00:03,000\nTwo\n"
        self.assertEqual([s.text for s in transcriber.parse_timed_text(srt)], ["One", "Two"])

    def test_vtt_with_hours_and_cue_identifiers(self):
        vtt = "WEBVTT\n\nNOTE a comment\n\nintro\n01:02:03.500 --> 01:02:05.000\nHi there\n"
        segment = transcriber.parse_timed_text(vtt)[0]
        self.assertEqual((segment.start, segment.end, segment.text), (3723.5, 3725.0, "Hi there"))


class ParseJson3Tests(unittest.TestCase):
    def test_events_become_segments(self):
        segments = transcriber.parse_json3(YOUTUBE_JSON3)
        self.assertEqual([s.text for s in segments], ["[Music]", "We're no strangers to", "love. You know"])
        self.assertAlmostEqual(segments[0].start, 0.32)
        self.assertAlmostEqual(segments[0].end, 14.58)

    def test_dispatch_by_extension(self):
        self.assertEqual(len(transcriber.parse_subtitles(YOUTUBE_JSON3, "json3")), 3)
        self.assertEqual(len(transcriber.parse_subtitles(YOUTUBE_AUTO_VTT, "vtt")), 3)


class TrackSelectionTests(unittest.TestCase):
    def test_prefers_uploaded_subtitles_in_spoken_language(self):
        info = {"language": "en", "subtitles": {"de": FMT, "en-US": FMT},
                "automatic_captions": {"en-orig": FMT, "en": FMT}}
        language, _, automatic = transcriber.pick_subtitle_track(info)
        self.assertEqual((language, automatic), ("en-US", False))

    def test_falls_back_to_original_automatic_captions(self):
        info = {"language": "tr", "subtitles": {"en": FMT}, "automatic_captions": {"en": FMT, "tr-orig": FMT}}
        language, _, automatic = transcriber.pick_subtitle_track(info)
        self.assertEqual((language, automatic), ("tr-orig", True))

    def test_unknown_language_prefers_english_uploads(self):
        info = {"subtitles": {"fr": FMT, "en": FMT}}
        self.assertEqual(transcriber.pick_subtitle_track(info)[0], "en")

    def test_live_chat_and_empty_tracks_are_ignored(self):
        self.assertIsNone(transcriber.pick_subtitle_track({"subtitles": {"live_chat": FMT, "en": []}}))
        self.assertIsNone(transcriber.pick_subtitle_track({}))

    def test_format_preference(self):
        formats = [{"ext": "srv1"}, {"ext": "vtt"}, {"ext": "json3"}]
        self.assertEqual(transcriber.choose_subtitle_format(formats)["ext"], "json3")
        self.assertEqual(transcriber.choose_subtitle_format([{"ext": "srt"}, {"ext": "vtt"}])["ext"], "vtt")
        self.assertIsNone(transcriber.choose_subtitle_format([{"ext": "ttml"}]))


class TranslationTests(unittest.TestCase):
    INFO = {"language": "en", "subtitles": {"en": FMT, "de": FMT},
            "automatic_captions": {"en-orig": FMT, "en": FMT, "tr": FMT, "zh-Hans": FMT}}

    def test_uploaded_subtitles_in_the_requested_language_win(self):
        self.assertEqual(transcriber.pick_subtitle_track(self.INFO, "de")[:1], ("de",))
        self.assertFalse(transcriber.pick_subtitle_track(self.INFO, "de")[2])

    def test_machine_translated_captions(self):
        language, _, automatic = transcriber.pick_subtitle_track(self.INFO, "tr")
        self.assertEqual((language, automatic), ("tr", True))
        self.assertEqual(transcriber.pick_subtitle_track(self.INFO, "zh-Hans")[0], "zh-Hans")

    def test_spoken_language_uses_the_original_track(self):
        self.assertEqual(transcriber.pick_subtitle_track(self.INFO, "en")[0], "en")

    def test_unavailable_language(self):
        self.assertIsNone(transcriber.pick_subtitle_track(self.INFO, "ja"))

    def test_language_helpers(self):
        self.assertEqual(transcriber.language_name("tr"), "Turkish")
        self.assertEqual(transcriber.language_name("pt-BR"), "Portuguese")
        self.assertEqual(transcriber.language_name("xx"), "xx")
        self.assertEqual(transcriber.language_name(""), "the original language")
        self.assertEqual(transcriber.base_language("zh-Hans"), "zh")


class FetchSubtitlesTests(unittest.TestCase):
    def test_inline_subtitle_data_needs_no_download(self):
        info = {"id": "abc", "subtitles": {"en": [{"ext": "vtt", "data": YOUTUBE_AUTO_VTT}]}}
        transcript = transcriber.fetch_subtitles(ydl=None, info=info, workdir=tempfile.gettempdir())
        self.assertEqual(len(transcript.segments), 3)
        self.assertEqual(transcript.language, "en")
        self.assertIn("uploaded subtitles", transcript.source)

    def test_translated_captions_are_labelled(self):
        track = [{"ext": "vtt", "data": YOUTUBE_AUTO_VTT, "url": "https://youtube.com/api/timedtext?tlang=tr",
                  "name": "Turkish"}]
        info = {"id": "abc", "language": "en", "automatic_captions": {"en-orig": FMT, "tr": track}}
        transcript = transcriber.fetch_subtitles(None, info, tempfile.gettempdir(), language="tr")
        self.assertEqual(transcript.language, "tr")
        self.assertEqual(transcript.source, "automatic captions, translated by YouTube (Turkish)")

    def test_rate_limited_downloads_are_retried(self):
        attempts = []

        class FlakyYdl:
            def dl(self, path, request, subtitle):
                attempts.append(path)
                if len(attempts) < 3:
                    raise OSError("HTTP Error 429: Too Many Requests")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(YOUTUBE_AUTO_VTT)

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "subs.vtt")
            transcriber._download_with_retries(FlakyYdl(), path, {}, waits=(0, 0))
            self.assertEqual(len(attempts), 3)
            attempts.clear()
            with self.assertRaises(OSError):
                transcriber._download_with_retries(FlakyYdl(), path, {}, waits=(0,))

        class BrokenYdl:
            def dl(self, path, request, subtitle):
                attempts.append(path)
                raise OSError("HTTP Error 404: Not Found")

        attempts.clear()
        with self.assertRaises(OSError):
            transcriber._download_with_retries(BrokenYdl(), "x", {}, waits=(0, 0))
        self.assertEqual(len(attempts), 1)

    def test_original_track_name_drops_the_orig_suffix(self):
        info = {"id": "abc", "language": "en", "automatic_captions": {"en-orig": [{"ext": "vtt", "data": YOUTUBE_AUTO_VTT}]}}
        transcript = transcriber.fetch_subtitles(None, info, tempfile.gettempdir())
        self.assertEqual((transcript.language, transcript.source), ("en", "automatic captions (English)"))


class ClipTests(unittest.TestCase):
    SEGMENTS = [Segment(0, 10, "a"), Segment(10, 20, "b"), Segment(20, 30, "c"), Segment(30, 40, "d")]

    def test_clip_keeps_overlapping_parts(self):
        clipped = transcriber.clip_segments(self.SEGMENTS, 15, 25, shift=False)
        self.assertEqual([(s.start, s.end, s.text) for s in clipped], [(15, 20, "b"), (20, 25, "c")])

    def test_clip_can_align_with_a_clipped_file(self):
        clipped = transcriber.clip_segments(self.SEGMENTS, 15, None, shift=True)
        self.assertEqual([(s.start, s.end, s.text) for s in clipped], [(0, 5, "b"), (5, 15, "c"), (15, 25, "d")])

    def test_shift(self):
        shifted = transcriber.shift_segments([Segment(0, 2, "x")], 90)
        self.assertEqual((shifted[0].start, shifted[0].end), (90, 92))


class WhisperSourceTests(unittest.TestCase):
    class FakeModel:
        def __init__(self, language):
            self.language, self.calls = language, []

        def transcribe(self, audio, **options):
            self.calls.append(options)
            piece = type("Piece", (), {"start": 0.0, "end": 2.0, "text": " Hello. "})
            info = type("Info", (), {"duration": 2.0, "language": self.language})
            return iter([piece]), info

    def test_transcription(self):
        model = self.FakeModel("tr")
        transcript = transcriber.transcribe_audio(model, "audio.mp3", "small")
        self.assertEqual(model.calls[0]["task"], "transcribe")
        self.assertEqual((transcript.language, transcript.segments[0].text), ("tr", "Hello."))
        self.assertEqual(transcript.source, "Whisper speech recognition (small model, language: Turkish)")

    def test_translation_into_english(self):
        model = self.FakeModel("tr")
        transcript = transcriber.transcribe_audio(model, "audio.mp3", "small", translate=True)
        self.assertEqual(model.calls[0]["task"], "translate")
        self.assertEqual(transcript.language, "en")
        self.assertIn("translated from Turkish", transcript.source)

    def test_progress_and_cancel(self):
        progress = []
        transcriber.transcribe_audio(self.FakeModel("en"), "a.mp3", "tiny",
                                     on_progress=lambda *args: progress.append(args))
        self.assertEqual(progress, [(1.0, 2.0, 2.0)])

        def cancel():
            raise RuntimeError("stop")

        with self.assertRaises(RuntimeError):
            transcriber.transcribe_audio(self.FakeModel("en"), "a.mp3", "tiny", check_cancel=cancel)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
class EmbedSubtitlesTests(unittest.TestCase):
    def test_subtitles_become_a_track_in_the_video(self):
        ffmpeg = shutil.which("ffmpeg")
        with tempfile.TemporaryDirectory() as folder:
            video = os.path.join(folder, "clip.mp4")
            subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=2",
                            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "2", "-c:v", "libx264",
                            "-c:a", "aac", "-shortest", video], check=True)
            srt = os.path.join(folder, "clip.srt")
            with open(srt, "w", encoding="utf-8") as fh:
                fh.write(transcriber.to_srt([Segment(0, 1.5, "Merhaba dünya")]))
            self.assertTrue(transcriber.can_embed_subtitles(video))
            self.assertFalse(transcriber.can_embed_subtitles(os.path.join(folder, "song.mp3")))
            transcriber.embed_subtitles(ffmpeg, video, srt, "tr")
            probe = subprocess.run([ffmpeg, "-hide_banner", "-i", video], capture_output=True, text=True).stderr
            self.assertRegex(probe, r"Stream #0:\d+(\[\w+\])?\(tur\): Subtitle: mov_text")
            self.assertEqual(sorted(os.listdir(folder)), ["clip.mp4", "clip.srt"])

    def test_failure_leaves_the_video_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            video = os.path.join(folder, "broken.mp4")
            with open(video, "wb") as fh:
                fh.write(b"not a video")
            with self.assertRaises(RuntimeError):
                transcriber.embed_subtitles(shutil.which("ffmpeg"), video, video, "en")
            self.assertEqual(os.listdir(folder), ["broken.mp4"])


class OutputTests(unittest.TestCase):
    def test_srt_time(self):
        self.assertEqual(transcriber.format_srt_time(3661.5), "01:01:01,500")
        self.assertEqual(transcriber.format_srt_time(-1), "00:00:00,000")

    def test_overlaps_are_trimmed_and_missing_durations_filled(self):
        segments = transcriber.normalize_segments([
            Segment(2, 6, "b"), Segment(0, 5, "a"), Segment(7, 7, "c"), Segment(8, 8, " ")])
        self.assertEqual([(s.start, s.end, s.text) for s in segments], [(0, 2, "a"), (2, 6, "b"), (7, 9.0, "c")])

    def test_srt_output(self):
        srt = transcriber.to_srt([Segment(0, 1.25, "Hello"), Segment(1.5, 3, "World")])
        self.assertEqual(srt, "1\n00:00:00,000 --> 00:00:01,250\nHello\n\n2\n00:00:01,500 --> 00:00:03,000\nWorld\n")

    def test_paragraphs_break_on_pauses_and_length(self):
        segments = [Segment(0, 1, "One."), Segment(1, 2, "Two."), Segment(10, 11, "After a pause.")]
        self.assertEqual(transcriber.build_paragraphs(segments),
                         [(0, "One. Two."), (10, "After a pause.")])
        long_segments = [Segment(i, i + 1, "A sentence that keeps going.") for i in range(60)]
        paragraphs = transcriber.build_paragraphs(long_segments, size=100)
        self.assertGreater(len(paragraphs), 10)
        self.assertTrue(all(text.endswith(".") for _, text in paragraphs))

    def test_text_output(self):
        transcript = Transcript([Segment(5, 6, "Hi."), Segment(3700, 3701, "Later.")], "uploaded subtitles (English)")
        text = transcriber.to_text(transcript, title="My video", url="https://example.com/v")
        self.assertEqual(text, "My video\nhttps://example.com/v\nTranscript source: uploaded subtitles (English)\n\n"
                               "[0:00:05] Hi.\n\n[1:01:40] Later.\n")

    def test_write_transcript_creates_both_files(self):
        with tempfile.TemporaryDirectory() as folder:
            base = os.path.join(folder, "nested", "Video")
            paths = transcriber.write_transcript(base, Transcript([Segment(0, 1, "Hello")], "test"))
            self.assertEqual(paths, [base + ".txt", base + ".srt"])
            with open(base + ".srt", encoding="utf-8") as fh:
                self.assertIn("Hello", fh.read())


if __name__ == "__main__":
    unittest.main()
