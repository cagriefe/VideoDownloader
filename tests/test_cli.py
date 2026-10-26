import contextlib
import io
import tempfile
import unittest
from unittest import mock

from videodownloader import cli
from videodownloader.downloader import JobError, JobResult
from videodownloader.settings import Settings

RESULTS = [{"url": "https://www.youtube.com/watch?v=a1", "title": "Lofi", "channel": "Chan",
            "duration": 3600, "views": 2_500_000}]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.settings = Settings(download_dir=self.folder.name, whisper_model="base", ai_provider="anthropic",
                                 ai_model="claude-haiku-5-5", ai_api_key="key")
        patches = [
            mock.patch.object(cli.Settings, "load", return_value=self.settings),
            mock.patch.object(cli, "History"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.folder.cleanup)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_defaults_come_from_the_app_settings(self):
        args = cli.build_parser(self.settings).parse_args(["https://youtu.be/x"])
        self.assertEqual((args.output, args.model, args.format, args.transcript),
                         (self.folder.name, "base", "best", "off"))

    def test_ai_config(self):
        args = cli.build_parser(self.settings).parse_args(["u", "--summary"])
        self.assertEqual(cli.ai_config(args, self.settings).api_key, "key")
        args = cli.build_parser(self.settings).parse_args(["u", "--summary", "--ai-provider", "ollama"])
        config = cli.ai_config(args, self.settings)
        self.assertEqual((config.provider, config.model, config.api_key), ("ollama", "", ""))

    def test_search_lists_results(self):
        with mock.patch.object(cli, "search_youtube", return_value=RESULTS) as search:
            code, out, _ = self.run_cli("--search", "lofi beats")
        self.assertEqual(code, 0)
        search.assert_called_once_with("lofi beats", cookies_browser="")
        self.assertIn(" 1. Lofi\n    Chan · 1:00:00 · 2.5M views  https://www.youtube.com/watch?v=a1", out)

    def test_download_with_options(self):
        task = mock.MagicMock()
        task.return_value.run.return_value = JobResult(title="Lofi", files=["/x/Lofi.mp3"], notes=["Heads up"])
        with mock.patch.object(cli, "search_youtube", return_value=RESULTS), mock.patch.object(cli, "DownloadTask", task):
            code, out, _ = self.run_cli("--search", "lofi", "--pick", "1", "-f", "mp3", "--clip", "1:30-2:00",
                                        "--summary", "-l", "tr")
        self.assertEqual(code, 0)
        job = task.call_args.args[0]
        self.assertEqual((job.url, job.quality, job.clip, job.language, job.summary),
                         ("https://www.youtube.com/watch?v=a1", "mp3", (90, 120), "tr", True))
        self.assertEqual(job.ai.provider, "anthropic")
        self.assertIn("✓ /x/Lofi.mp3", out)
        self.assertIn("! Heads up", out)
        cli.History.return_value.add.assert_called_once()

    def test_failures_set_the_exit_code(self):
        task = mock.MagicMock()
        task.return_value.run.side_effect = JobError("This video is unavailable")
        with mock.patch.object(cli, "DownloadTask", task):
            code, _, err = self.run_cli("https://youtu.be/x", "https://youtu.be/y")
        self.assertEqual(code, 1)
        self.assertEqual(task.call_count, 2)
        self.assertIn("✗ This video is unavailable", err)

    def test_usage_errors(self):
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(self.run_cli("not a link")[0], 2)
        self.assertEqual(self.run_cli("https://youtu.be/x", "--clip", "2:00-1:00")[0], 2)
        with mock.patch.object(cli, "search_youtube", return_value=RESULTS):
            self.assertEqual(self.run_cli("--search", "x", "--pick", "5")[0], 2)


if __name__ == "__main__":
    unittest.main()
