"""A searchable list of everything that was downloaded."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime

from .settings import config_dir

log = logging.getLogger(__name__)


def history_path() -> str:
    return os.path.join(config_dir(), "history.json")


@dataclass
class HistoryEntry:
    title: str
    url: str
    date: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    files: list[str] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    folder: str = ""

    @property
    def outputs(self) -> list[str]:
        return self.files + self.transcripts + self.summaries

    def matches(self, query: str) -> bool:
        query = query.strip().lower()
        return not query or query in self.title.lower() or query in self.url.lower()


class History:
    def __init__(self, path: str | None = None, limit: int = 500):
        self.path = path or history_path()
        self.limit = limit
        self.entries: list[HistoryEntry] = self._load()

    def _load(self) -> list[HistoryEntry]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable history file %s: %s", self.path, exc)
            return []
        known = {f.name for f in fields(HistoryEntry)}
        entries = []
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and isinstance(item.get("title"), str) and isinstance(item.get("url"), str):
                entries.append(HistoryEntry(**{k: v for k, v in item.items() if k in known}))
        return entries

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temp_path = self.path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as fh:
                json.dump([asdict(e) for e in self.entries], fh, indent=1, ensure_ascii=False)
            os.replace(temp_path, self.path)
        except OSError as exc:
            log.warning("Couldn't save the download history: %s", exc)

    def add(self, entry: HistoryEntry) -> None:
        self.entries.insert(0, entry)
        del self.entries[self.limit:]
        self.save()

    def search(self, query: str = "") -> list[HistoryEntry]:
        return [e for e in self.entries if e.matches(query)]

    def clear(self) -> None:
        self.entries = []
        self.save()
