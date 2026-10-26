import json
import os
import tempfile
import unittest

import transcriber
from transcriber import Segment, Transcript

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


class FetchSubtitlesTests(unittest.TestCase):
    def test_inline_subtitle_data_needs_no_download(self):
        info = {"id": "abc", "subtitles": {"en": [{"ext": "vtt", "data": YOUTUBE_AUTO_VTT}]}}
        transcript = transcriber.fetch_subtitles(ydl=None, info=info, workdir=tempfile.gettempdir())
        self.assertEqual(len(transcript.segments), 3)
        self.assertEqual(transcript.language, "en")
        self.assertIn("uploaded subtitles", transcript.source)


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
