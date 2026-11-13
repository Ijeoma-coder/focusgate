"""Free, instant classification of the active window from its app and title."""
from __future__ import annotations

import re

OWN_TITLE = "focusgate"


def normalise_title(title: str) -> str:
    """Stable cache key for a window title: drop counters like '(3) ' and case."""
    title = (title or "").lower().strip()
    title = re.sub(r"^\(\d+\+?\)\s*", "", title)
    return re.sub(r"\s+", " ", title)


def classify(process: str, title: str, preset: dict, distractions: list[str]) -> str:
    """Return 'self', 'off', 'on' or 'unknown'.

    Distractions win over the preset, so a blocked keyword in the title is
    off-task even inside an app the preset allows.
    """
    process = (process or "").lower()
    low = (title or "").lower()
    if low.startswith(OWN_TITLE):
        return "self"
    if not process and not low:
        return "unknown"
    for word in distractions:
        word = word.lower().strip()
        if word and (word in low or word == process):
            return "off"
    if process in {a.lower().strip() for a in preset.get("apps", [])}:
        return "on"
    for word in preset.get("keywords", []):
        word = word.lower().strip()
        if word and word in low:
            return "on"
    return "unknown"
