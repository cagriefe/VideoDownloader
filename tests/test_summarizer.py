import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from videodownloader import summarizer
from videodownloader.summarizer import AIConfig, SummaryError
from videodownloader.transcriber import Segment, Transcript

SUMMARY = "## TL;DR\nShort.\n\n## Key points\n- One\n\n## Outline\n- [00:00] Start"


class FakeAPI(BaseHTTPRequestHandler):
    """Answers like Ollama, OpenAI and Anthropic do, and records every request."""

    requests: list = []
    status = 200

    def log_message(self, *args):
        pass

    def _reply(self, payload, status=None):
        body = json.dumps(payload).encode()
        self.send_response(status or self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body=None):
        headers = {key.lower(): value for key, value in self.headers.items()}
        FakeAPI.requests.append({"path": self.path, "headers": headers, "body": body})

    def do_GET(self):
        self._record()
        if self.status != 200:
            return self._reply({"error": {"message": "invalid x-api-key"}})
        if self.path == "/api/tags":
            return self._reply({"models": [{"name": "llama3.2:latest"}, {"name": "qwen3:8b"}]})
        if self.path.startswith("/v1/models"):
            return self._reply({"data": [{"id": "gpt-6-luna"}, {"id": "text-embedding-3-small"}, {"id": "claude-haiku-5-5"}]})
        self._reply({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._record(body)
        if self.status != 200:
            return self._reply({"error": {"message": "invalid x-api-key"}})
        if self.path == "/api/chat":
            return self._reply({"message": {"role": "assistant", "content": "<think>hmm</think>" + SUMMARY}})
        if self.path == "/v1/chat/completions":
            return self._reply({"choices": [{"message": {"role": "assistant", "content": SUMMARY}}]})
        if self.path == "/v1/messages":
            return self._reply({"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": SUMMARY}]})
        self._reply({"error": "not found"}, 404)


class FakeServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeAPI.requests = []
        FakeAPI.status = 200

    def test_ollama_picks_an_installed_model_and_strips_thinking(self):
        config = summarizer.with_model(AIConfig(provider="ollama", base_url=self.url))
        self.assertEqual(config.model, "llama3.2:latest")
        self.assertEqual(summarizer.clean_response(summarizer.chat(config, "sys", "hello")), SUMMARY)
        body = FakeAPI.requests[-1]["body"]
        self.assertEqual(body["model"], "llama3.2:latest")
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})

    def test_openai_compatible_chat(self):
        config = AIConfig(provider="openai", base_url=self.url + "/v1", api_key="sk-test").resolved()
        self.assertEqual(config.model, "gpt-6-luna")
        self.assertEqual(summarizer.chat(config, "sys", "hello"), SUMMARY)
        request = FakeAPI.requests[-1]
        self.assertEqual(request["headers"]["authorization"], "Bearer sk-test")
        self.assertEqual(request["body"]["model"], "gpt-6-luna")
        self.assertEqual(summarizer.list_models(config), ["claude-haiku-5-5", "gpt-6-luna"])

    def test_anthropic_returns_only_text_blocks(self):
        config = AIConfig(provider="anthropic", base_url=self.url, api_key="key").resolved()
        self.assertEqual(summarizer.chat(config, "sys", "hello"), SUMMARY)
        request = FakeAPI.requests[-1]
        self.assertEqual(request["path"], "/v1/messages")
        self.assertEqual(request["headers"]["x-api-key"], "key")
        self.assertEqual(request["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(request["body"]["system"], "sys")
        self.assertEqual(request["body"]["model"], "claude-haiku-5-5")

    def test_rejected_key(self):
        FakeAPI.status = 401
        config = AIConfig(provider="anthropic", base_url=self.url, api_key="bad").resolved()
        with self.assertRaisesRegex(SummaryError, "API key was rejected.*invalid x-api-key"):
            summarizer.chat(config, "sys", "hello")

    def test_full_summary(self):
        transcript = Transcript([Segment(0, 5, "Hello and welcome."), Segment(65, 70, "Bye.")], "uploaded subtitles (English)")
        markdown = summarizer.summarize(transcript, "My Video", AIConfig(provider="ollama", base_url=self.url),
                                        language="tr", url="https://youtu.be/x")
        self.assertTrue(markdown.startswith("# My Video\n\nhttps://youtu.be/x\n\n## TL;DR"))
        self.assertIn("- [00:00](https://youtu.be/x?t=0s) Start", markdown)
        self.assertIn("Summarized by Ollama (on this computer) (llama3.2:latest) from the uploaded subtitles (English).",
                      markdown)
        prompt = FakeAPI.requests[-1]["body"]["messages"][1]["content"]
        self.assertIn("Write in Turkish.", prompt)
        self.assertIn("[00:00] Hello and welcome.", prompt)


class OfflineTests(unittest.TestCase):
    def test_defaults_and_keys_from_the_environment(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "env-key"}):
            config = AIConfig(provider="anthropic").resolved()
        self.assertEqual((config.model, config.base_url, config.api_key),
                         ("claude-haiku-5-5", "https://api.anthropic.com", "env-key"))
        self.assertEqual(AIConfig(provider="nonsense").resolved().provider, "ollama")
        self.assertEqual(AIConfig(provider="openai", base_url="http://x/v1/", api_key=" k ").resolved().base_url, "http://x/v1")

    def test_missing_key_is_explained(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            config = AIConfig(provider="openai").resolved()
        with self.assertRaisesRegex(SummaryError, "OPENAI_API_KEY"):
            summarizer.chat(config, "sys", "hello")

    def test_unreachable_ollama(self):
        with self.assertRaisesRegex(SummaryError, "Couldn't reach Ollama"):
            summarizer.list_models(AIConfig(provider="ollama", base_url="http://127.0.0.1:9"))

    def test_chunking_keeps_lines_whole(self):
        text = "\n".join(f"[00:{i:02d}] line {i}" for i in range(10))
        chunks = summarizer.chunk_text(text, 40)
        self.assertGreater(len(chunks), 2)
        self.assertEqual("\n".join(chunks), text)

    def test_timestamps_become_links_to_that_moment(self):
        markdown = "- [00:18] Verse\n- [1:02:03] Late\n- [00:05](https://already.linked) Keep"
        linked = summarizer.link_timestamps(markdown, "https://www.youtube.com/watch?v=abc&t=10s")
        self.assertIn("[00:18](https://www.youtube.com/watch?v=abc&t=18s) Verse", linked)
        self.assertIn("[1:02:03](https://www.youtube.com/watch?v=abc&t=3723s) Late", linked)
        self.assertIn("[00:05](https://already.linked) Keep", linked)
        self.assertEqual(summarizer.timestamp_url("https://youtu.be/abc", 90), "https://youtu.be/abc?t=90s")
        self.assertEqual(summarizer.timestamp_url("https://vimeo.com/42", 90), "https://vimeo.com/42#t=90s")
        self.assertIsNone(summarizer.timestamp_url("https://example.com/v", 90))
        self.assertEqual(summarizer.link_timestamps("- [00:18] x", "https://example.com/v"), "- [00:18] x")
        self.assertEqual(summarizer.link_timestamps("- [00:18] x", None), "- [00:18] x")

    def test_clean_response(self):
        self.assertEqual(summarizer.clean_response("<think>x</think>\n```markdown\n## A\n```"), "## A")
        self.assertEqual(summarizer.clean_response("  plain "), "plain")

    def test_long_transcripts_are_summarized_in_parts(self):
        prompts = []

        def fake_chat(config, system, prompt):
            prompts.append(prompt)
            return "- [00:01] note" if "Write concise notes" in prompt else SUMMARY

        segments = [Segment(i * 10, i * 10 + 9, f"Sentence number {i} goes here.") for i in range(400)]
        config = AIConfig(provider="openai", model="m", api_key="k")
        steps = []
        with mock.patch.object(summarizer, "chat", fake_chat), \
                mock.patch.dict(summarizer.PROVIDERS["openai"], {"chunk": 2000}):
            markdown = summarizer.summarize(Transcript(segments, "test"), "Long", config, on_step=steps.append)
        notes = [p for p in prompts if "Write concise notes" in p]
        self.assertGreater(len(notes), 3)
        self.assertIn("timestamped notes that cover the whole video", prompts[-1])
        self.assertIn("Write in the same language as the transcript.", prompts[-1])
        self.assertIn("## Key points", markdown)
        self.assertEqual(steps[-1], "Writing the summary…")

    def test_cancel_between_steps(self):
        def cancel():
            raise RuntimeError("cancelled")

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            summarizer.summarize(Transcript([Segment(0, 1, "x")], "t"), "T", AIConfig(), check_cancel=cancel)

    def test_empty_transcript(self):
        with mock.patch.object(summarizer, "with_model", lambda config: config), \
                self.assertRaisesRegex(SummaryError, "empty"):
            summarizer.summarize(Transcript([], "t"), "T", AIConfig(model="m"))


if __name__ == "__main__":
    unittest.main()
