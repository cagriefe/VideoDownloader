"""Remembers the user's choices between sessions in a small JSON file."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, fields

log = logging.getLogger(__name__)

APP_DIR_NAME = "VideoDownloader"


def config_dir() -> str:
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_DIR_NAME)


def settings_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def default_download_dir() -> str:
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    return downloads if os.path.isdir(downloads) else os.path.expanduser("~")


@dataclass
class Settings:
    download_dir: str = field(default_factory=default_download_dir)
    quality: str = "best"
    transcript: str = "off"
    whisper_model: str = "small"
    playlist: bool = False

    @classmethod
    def load(cls, path: str | None = None) -> Settings:
        settings = cls()
        path = path or settings_path()
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return settings
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable settings file %s: %s", path, exc)
            return settings
        if not isinstance(data, dict):
            return settings
        for f in fields(cls):
            value = data.get(f.name)
            if type(value) is type(getattr(settings, f.name)):
                setattr(settings, f.name, value)
        if not os.path.isdir(settings.download_dir):
            settings.download_dir = default_download_dir()
        return settings

    def save(self, path: str | None = None) -> None:
        path = path or settings_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            temp_path = path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
            os.replace(temp_path, path)
        except OSError as exc:
            log.warning("Couldn't save settings: %s", exc)
