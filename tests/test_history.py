import json
import os
import tempfile
import unittest

from videodownloader.history import History, HistoryEntry


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "history.json")

    def tearDown(self):
        self.folder.cleanup()

    def test_newest_first_and_persisted(self):
        history = History(self.path)
        history.add(HistoryEntry(title="First", url="https://a.com/1", files=["/x/1.mp4"]))
        history.add(HistoryEntry(title="Second", url="https://a.com/2", summaries=["/x/2.summary.md"]))
        reloaded = History(self.path)
        self.assertEqual([e.title for e in reloaded.entries], ["Second", "First"])
        self.assertEqual(reloaded.entries[0].outputs, ["/x/2.summary.md"])

    def test_search_title_and_url(self):
        history = History(self.path)
        history.add(HistoryEntry(title="Lofi beats", url="https://youtu.be/abc"))
        history.add(HistoryEntry(title="News", url="https://vimeo.com/42"))
        self.assertEqual([e.title for e in history.search("LOFI")], ["Lofi beats"])
        self.assertEqual([e.title for e in history.search("vimeo")], ["News"])
        self.assertEqual(len(history.search("")), 2)

    def test_limit(self):
        history = History(self.path, limit=3)
        for number in range(5):
            history.add(HistoryEntry(title=str(number), url="u"))
        self.assertEqual([e.title for e in History(self.path).entries], ["4", "3", "2"])

    def test_clear(self):
        history = History(self.path)
        history.add(HistoryEntry(title="x", url="u"))
        history.clear()
        self.assertEqual(History(self.path).entries, [])

    def test_bad_files_and_entries_are_ignored(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump([{"title": "ok", "url": "u", "unknown": 1}, {"title": 5}, "junk"], fh)
        self.assertEqual([e.title for e in History(self.path).entries], ["ok"])
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        with self.assertLogs("videodownloader.history", level="WARNING"):
            self.assertEqual(History(self.path).entries, [])


if __name__ == "__main__":
    unittest.main()
