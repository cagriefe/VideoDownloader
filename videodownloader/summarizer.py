"""AI summaries of transcripts.

Works with a local Ollama model (free and private), the OpenAI API, Anthropic's
Claude API, or any OpenAI-compatible server (LM Studio, llama.cpp, vLLM, …).
HTTP calls use only the Python standard library.
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Callable

from .transcriber import Transcript, build_paragraphs, format_clock, language_name, normalize_segments

PROVIDERS = {
    "ollama": {
        "label": "Ollama (on this computer)", "base_url": "http://localhost:11434",
        "model": "", "key_env": "", "chunk": 8_000,
    },
    "openai": {
        "label": "OpenAI", "base_url": "https://api.openai.com/v1",
        "model": "gpt-6-luna", "key_env": "OPENAI_API_KEY", "chunk": 120_000,
    },
    "anthropic": {
        "label": "Anthropic Claude", "base_url": "https://api.anthropic.com",
        "model": "claude-haiku-5-5", "key_env": "ANTHROPIC_API_KEY", "chunk": 120_000,
    },
    "compatible": {
        "label": "OpenAI-compatible server", "base_url": "http://localhost:1234/v1",
        "model": "", "key_env": "", "chunk": 8_000,
    },
}

SYSTEM_PROMPT = (
    "You summarize video transcripts for busy people. Be accurate and specific, keep the speaker's "
    "meaning, and never add facts that are not in the transcript."
)

SUMMARY_FORMAT = """Use exactly this Markdown structure:

## TL;DR
Two or three sentences that capture the essence of the video.

## Key points
- Five to ten bullet points with the most important ideas, facts, numbers and conclusions.

## Outline
- [mm:ss] A short title for each major part of the video, in order. Use the timestamps from the transcript."""

# Words in model ids that are not chat models (OpenAI lists everything under /models).
_NON_CHAT_MODELS = ("embedding", "tts", "whisper", "dall-e", "image", "audio", "realtime",
                    "transcribe", "moderation", "search", "davinci", "babbage", "sora")


class SummaryError(Exception):
    """A problem with the AI service, with a message that can be shown to the user."""


@dataclass
class AIConfig:
    provider: str = "ollama"
    model: str = ""
    base_url: str = ""
    api_key: str = ""

    @property
    def details(self) -> dict:
        return PROVIDERS.get(self.provider, PROVIDERS["ollama"])

    @property
    def label(self) -> str:
        return self.details["label"]

    def resolved(self) -> AIConfig:
        """Fill in the provider's defaults and API keys from the environment."""
        details = self.details
        env_key = os.environ.get(details["key_env"], "") if details["key_env"] else ""
        return AIConfig(
            provider=self.provider if self.provider in PROVIDERS else "ollama",
            model=self.model.strip() or details["model"],
            base_url=(self.base_url.strip() or details["base_url"]).rstrip("/"),
            api_key=self.api_key.strip() or env_key,
        )


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _error_detail(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:200]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or data)[:200]


def _request(url: str, payload: dict | None = None, headers: dict | None = None, timeout: float = 60) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json", "User-Agent": "VideoDownloader", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc.read().decode("utf-8", "replace"))
        if exc.code in (401, 403):
            raise SummaryError(f"The API key was rejected ({detail}). Check it in Settings.") from exc
        if exc.code == 429:
            raise SummaryError(f"The AI service is limiting requests ({detail}). Try again later.") from exc
        raise SummaryError(f"The AI service returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise SummaryError("The AI service took too long to answer.") from exc
        raise SummaryError(f"Couldn't connect to {url} ({exc.reason}).") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise SummaryError("The AI service took too long to answer.") from exc
    except ValueError as exc:
        raise SummaryError("The AI service sent a response that isn't JSON.") from exc


def _auth_headers(config: AIConfig) -> dict:
    if config.provider == "anthropic":
        return {"x-api-key": config.api_key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}


def _require_key(config: AIConfig) -> None:
    if config.details["key_env"] and not config.api_key:
        raise SummaryError(f"Add your {config.label} API key in Settings (or set {config.details['key_env']}).")


def list_models(config: AIConfig) -> list[str]:
    """Models available from the provider, e.g. to fill a list in the settings."""
    config = config.resolved()
    if config.provider == "ollama":
        try:
            data = _request(f"{config.base_url}/api/tags", timeout=10)
        except SummaryError as exc:
            raise SummaryError(
                f"Couldn't reach Ollama at {config.base_url}. Install it from https://ollama.com, "
                "start it and download a model, e.g. “ollama pull llama3.2”.") from exc
        return [m["name"] for m in data.get("models") or [] if m.get("name")]
    _require_key(config)
    path = "/v1/models?limit=100" if config.provider == "anthropic" else "/models"
    data = _request(config.base_url + path, headers=_auth_headers(config), timeout=20)
    ids = [m["id"] for m in data.get("data") or [] if m.get("id")]
    if config.provider == "openai":
        ids = sorted(i for i in ids if not any(word in i for word in _NON_CHAT_MODELS))
    return ids


def with_model(config: AIConfig) -> AIConfig:
    """Resolve the config and pick the first available model if none is set."""
    config = config.resolved()
    if not config.model:
        models = list_models(config)
        if not models:
            if config.provider == "ollama":
                raise SummaryError("Ollama has no models yet. Download one, e.g. “ollama pull llama3.2”.")
            raise SummaryError("Choose a model for AI summaries in Settings.")
        config = replace(config, model=models[0])
    return config


def chat(config: AIConfig, system: str, prompt: str) -> str:
    """Send one prompt and return the reply text. `config` must be resolved and have a model."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    if config.provider == "ollama":
        data = _request(f"{config.base_url}/api/chat", {
            "model": config.model, "messages": messages, "stream": False,
            "options": {"num_ctx": 16384, "temperature": 0.2},
        }, timeout=900)
        return (data.get("message") or {}).get("content") or ""
    _require_key(config)
    if config.provider == "anthropic":
        data = _request(f"{config.base_url}/v1/messages", {
            "model": config.model, "max_tokens": 8192, "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }, _auth_headers(config), timeout=300)
        # Thinking models return "thinking" blocks before the answer; only text blocks are wanted.
        return "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
    data = _request(f"{config.base_url}/chat/completions", {"model": config.model, "messages": messages},
                    _auth_headers(config), timeout=300)
    choices = data.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content") or ""


# --------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------

def transcript_for_prompt(transcript: Transcript) -> str:
    segments = normalize_segments(transcript.segments)
    with_hours = bool(segments) and segments[-1].end >= 3600
    return "\n".join(f"[{format_clock(start, with_hours)}] {text}"
                     for start, text in build_paragraphs(segments, size=400))


def chunk_text(text: str, size: int) -> list[str]:
    """Split text into chunks of about `size` characters, at line breaks."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.splitlines():
        if current and length + len(line) > size:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _language_instruction(language: str | None) -> str:
    if language:
        return f"Write in {language_name(language)}."
    return "Write in the same language as the transcript."


def summary_prompt(title: str, material: str, language: str | None, from_notes: bool) -> str:
    what = "timestamped notes that cover the whole video" if from_notes else "transcript"
    return (f'Summarize the video "{title}" using its {what} below. {_language_instruction(language)}\n\n'
            f"{SUMMARY_FORMAT}\n\n<material>\n{material}\n</material>")


def notes_prompt(title: str, chunk: str, index: int, count: int, language: str | None) -> str:
    return (f'This is part {index} of {count} of the material for the video "{title}". Write concise notes '
            "about everything important in it as bullet points, each starting with the timestamp where it "
            f'is said, like "- [12:34] ...". {_language_instruction(language)} Reply with the notes only.'
            f"\n\n<material>\n{chunk}\n</material>")


def clean_response(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*)\n```", text, flags=re.S)
    return (fenced.group(1) if fenced else text).strip()


_TIMESTAMP_RE = re.compile(r"\[((?:\d+:)?\d{1,2}:\d{2})\](?!\()")


def timestamp_url(url: str, seconds: int) -> str | None:
    """A link that opens the video at `seconds`, for sites that support it."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host.endswith(("youtube.com", "youtu.be")):
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query) if k != "t"] + [("t", f"{seconds}s")]
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query), fragment=""))
    if host.endswith("vimeo.com"):
        return urllib.parse.urlunparse(parsed._replace(fragment=f"t={seconds}s"))
    return None


def link_timestamps(markdown: str, url: str | None) -> str:
    """Turn "[12:34]" into a link to that moment of the video."""
    if not url:
        return markdown

    def replace(match):
        seconds = 0
        for part in match.group(1).split(":"):
            seconds = seconds * 60 + int(part)
        link = timestamp_url(url, seconds)
        return f"[{match.group(1)}]({link})" if link else match.group(0)

    return _TIMESTAMP_RE.sub(replace, markdown)


def summarize(
    transcript: Transcript,
    title: str,
    config: AIConfig,
    language: str | None = None,
    url: str | None = None,
    on_step: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> str:
    """Return a Markdown summary: TL;DR, key points and a timestamped outline.

    Long transcripts are summarized in parts first (map-reduce), so they fit
    into the model's context window.
    """
    def step(message: str) -> None:
        if check_cancel:
            check_cancel()
        if on_step:
            on_step(message)

    step("Connecting to the AI service…")
    config = with_model(config)
    material = transcript_for_prompt(transcript)
    if not material.strip():
        raise SummaryError("The transcript is empty, so there is nothing to summarize.")

    from_notes = False
    while True:
        chunks = chunk_text(material, config.details["chunk"])
        if len(chunks) == 1:
            break
        notes = []
        for index, chunk in enumerate(chunks, 1):
            step(f"Summarizing part {index} of {len(chunks)}…")
            notes.append(clean_response(chat(config, SYSTEM_PROMPT, notes_prompt(title, chunk, index, len(chunks), language))))
        material, from_notes = "\n".join(notes), True

    step("Writing the summary…")
    body = clean_response(chat(config, SYSTEM_PROMPT, summary_prompt(title, material, language, from_notes)))
    if not body:
        raise SummaryError("The AI service returned an empty summary.")
    body = link_timestamps(body, url)
    header = [f"# {title}"] + ([url] if url else [])
    footer = f"*Summarized by {config.label} ({config.model}) from the {transcript.source}.*"
    return "\n\n".join(header + [body, "---", footer]) + "\n"
