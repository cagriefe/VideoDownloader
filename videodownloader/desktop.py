"""Small helpers for working with the operating system: file manager, default apps and notifications."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import webbrowser

log = logging.getLogger(__name__)

if sys.platform == "darwin":
    FILE_MANAGER = "Finder"
elif os.name == "nt":
    FILE_MANAGER = "Explorer"
else:
    FILE_MANAGER = "file manager"


def reveal(path: str) -> None:
    """Show a file, or open a folder, in the system file manager."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if os.path.isfile(path) else ["open", path])
    elif os.name == "nt":
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])


def open_path(path: str) -> None:
    """Open a file with its default app (e.g. play a video)."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif os.name == "nt":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path])


def open_url(url: str) -> None:
    webbrowser.open(url)


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


_WINDOWS_TOAST = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName("text")
$texts.Item(0).AppendChild($xml.CreateTextNode($env:VD_TITLE)) > $null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:VD_MESSAGE)) > $null
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def notify(title: str, message: str) -> bool:
    """Show a desktop notification. Returns False if it couldn't be shown; never raises."""
    try:
        if sys.platform == "darwin":
            script = f"display notification {_applescript_string(message)} with title {_applescript_string(title)}"
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            # The text goes through environment variables, so nothing needs escaping.
            env = {**os.environ, "VD_TITLE": title, "VD_MESSAGE": message}
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", _WINDOWS_TOAST],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", title, message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            return False
        return True
    except OSError as exc:
        log.debug("Couldn't show a notification: %s", exc)
        return False
