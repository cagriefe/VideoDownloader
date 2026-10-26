import json
import os
import tempfile
import unittest

from settings import Settings, default_download_dir


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "config", "settings.json")

    def tearDown(self):
        self.folder.cleanup()

    def write(self, content):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(Settings.load(self.path), Settings())

    def test_round_trip(self):
        settings = Settings(download_dir=self.folder.name, quality="720", transcript="auto",
                            whisper_model="base", playlist=True)
        settings.save(self.path)
        self.assertEqual(Settings.load(self.path), settings)

    def test_invalid_values_are_ignored(self):
        self.write(json.dumps({"quality": 720, "playlist": "yes", "transcript": "auto",
                               "download_dir": "/this/folder/does/not/exist"}))
        settings = Settings.load(self.path)
        self.assertEqual(settings.quality, "best")
        self.assertFalse(settings.playlist)
        self.assertEqual(settings.transcript, "auto")
        self.assertEqual(settings.download_dir, default_download_dir())

    def test_corrupt_file_gives_defaults(self):
        self.write("{not json")
        with self.assertLogs("settings", level="WARNING"):
            self.assertEqual(Settings.load(self.path), Settings())


if __name__ == "__main__":
    unittest.main()
