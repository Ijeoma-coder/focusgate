"""Settings: defaults, load/save, and delayed changes for strict mode.

Everything lives in one JSON file in the data folder. API keys are NOT stored
here; they go in Windows Credential Manager (see judge.py).
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
from pathlib import Path

APP_NAME = "FocusGate"
MAIN_PORT = 47631        # loopback-only, used as a single-instance lock
WATCHDOG_PORT = 47632

# Process names the blocker will never touch, whatever ends up in the blocklist.
PROTECTED_NAMES = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
    "dwm.exe", "explorer.exe", "taskmgr.exe", "fontdrvhost.exe", "sihost.exe",
    "ctfmon.exe", "conhost.exe", "runtimebroker.exe", "searchhost.exe",
    "startmenuexperiencehost.exe", "shellexperiencehost.exe", "audiodg.exe",
    "python.exe", "pythonw.exe", "py.exe", "cmd.exe", "powershell.exe",
    "pwsh.exe", "windowsterminal.exe", "msmpeng.exe", "securityhealthservice.exe",
}

DEFAULTS = {
    "blocklist": {
        "names": [
            "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe",
            "riotclientservices.exe", "riotclientux.exe", "leagueclient.exe",
            "valorant.exe", "valorant-win64-shipping.exe", "battle.net.exe",
            "eadesktop.exe", "origin.exe", "ubisoftconnect.exe", "upc.exe",
            "galaxyclient.exe", "minecraftlauncher.exe", "minecraft.exe",
            "robloxplayerbeta.exe", "fortniteclient-win64-shipping.exe",
            "cs2.exe", "gta5.exe", "xboxpcapp.exe",
        ],
        "folders": [],
    },
    # Window-title keywords / process names that are never on-task.
    "distractions": [
        "netflix", "twitch", "tiktok", "instagram", "facebook", "twitter",
        "disney+", "prime video", "crunchyroll", "9gag", "pinterest",
        "snapchat",
    ],
    # What counts as on-task without asking the AI. Anything else is "unknown".
    "presets": {
        "Study and reading": {
            "apps": ["acrord32.exe", "acrobat.exe", "sumatrapdf.exe", "winword.exe",
                     "onenote.exe", "powerpnt.exe", "excel.exe", "obsidian.exe",
                     "notion.exe", "anki.exe", "zotero.exe"],
            "keywords": [".pdf", "lecture", "blackboard", "google docs",
                         "google slides", "overleaf", "past paper"],
        },
        "Coding": {
            "apps": ["code.exe", "pycharm64.exe", "idea64.exe", "webstorm64.exe",
                     "devenv.exe", "windowsterminal.exe", "cmd.exe",
                     "powershell.exe", "pwsh.exe", "postman.exe", "dbeaver.exe",
                     "pgadmin4.exe"],
            "keywords": ["stack overflow", "github", "gitlab", "localhost",
                         "127.0.0.1", "documentation"],
        },
        "Writing": {
            "apps": ["winword.exe", "obsidian.exe", "notion.exe", "zotero.exe",
                     "texstudio.exe"],
            "keywords": ["google docs", "overleaf", "google scholar", "grammarly"],
        },
        "Let the AI decide": {"apps": [], "keywords": []},
    },
    "ai": {
        "backend": "none",                         # none | gemini | claude
        "gemini_model": "gemini-flash-lite-latest",
        "claude_model": "claude-haiku-5-5",
    },
    "camera": {
        "index": 0,                  # which webcam (0 is the built-in one)
        "face_grace_minutes": 10,    # movement counts this long after a face was last seen
        "ai_checks": False,          # send an occasional frame to the AI
        "ai_minutes": 5,
    },
    "earn_ratio": 0.5,               # game minutes earned per verified minute
    "require_full_target": False,    # True: earn nothing unless the target is hit
    "poll_seconds": 5,               # how often the active window is checked
    "ai_min_gap_seconds": 20,        # minimum spacing between AI calls
    "max_ai_calls_per_session": 60,
    "spot_check_minutes": 10,        # random screenshot checks; 0 turns them off
    "idle_limit_minutes": 10,        # no input for this long pauses the count
    "timelapse_frames": 16,
    "timelapse_pass_fraction": 0.6,
    "timelapse_max_age_hours": 24,
    "strict_mode": False,
    "cooldown_hours": 12,
    "pending": [],                   # delayed changes waiting for the cooldown
    "last_task": {"task": "", "minutes": 60, "preset": "Study and reading",
                  "mode": "live"},
}

# Settings where a change in the "easier" direction waits for the cooldown.
_LOOSENING = {
    "earn_ratio": lambda old, new: new > old,
    "cooldown_hours": lambda old, new: new < old,
    "strict_mode": lambda old, new: bool(old) and not new,
    "require_full_target": lambda old, new: bool(old) and not new,
}


def data_dir() -> Path:
    override = os.environ.get("FOCUSGATE_HOME")
    if override:
        base = Path(override)
    else:
        base = Path(os.environ.get("APPDATA") or (Path.home() / ".config")) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def stop_flag() -> Path:
    return data_dir() / "STOP"


def _merge(defaults, loaded):
    """Loaded values win; keys missing from an older file fall back to defaults."""
    out = copy.deepcopy(defaults)
    for key, value in (loaded or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "presets":
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def unsafe_folder_reason(folder: str) -> str | None:
    """Refuse folders where killing every program inside would be dangerous."""
    path = os.path.normcase(os.path.abspath(folder)).rstrip("\\/")
    if not path or len(Path(path).parts) <= 1:
        return "That is a whole drive. Pick the game's own folder."
    risky = [os.environ.get("SystemRoot", r"C:\Windows"),
             os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.environ.get("ProgramData", r"C:\ProgramData"),
             str(Path.home()), os.path.dirname(sys.executable)]
    for item in risky:
        item = os.path.normcase(os.path.abspath(item)).rstrip("\\/")
        if path == item or (item + os.sep).startswith(path + os.sep):
            return "That folder holds far more than games. Pick the game's own folder."
    windows = os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows")).rstrip("\\/")
    if (path + os.sep).startswith(windows + os.sep):
        return "Folders inside Windows cannot be blocked."
    return None


class Config:
    def __init__(self, path: Path | None = None, data: dict | None = None):
        self.path = Path(path) if path else data_dir() / "config.json"
        self.data = _merge(DEFAULTS, data)
        self._lock = threading.RLock()

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = Path(path) if path else data_dir() / "config.json"
        data = None
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # Keep the broken file for inspection and start from defaults.
                try:
                    path.replace(path.with_suffix(".broken.json"))
                except OSError:
                    pass
        cfg = cls(path, data)
        if data is None:
            cfg.save()
        return cfg

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def get(self, key: str, default=None):
        with self._lock:
            return copy.deepcopy(self.data.get(key, default))

    def set(self, key: str, value) -> None:
        with self._lock:
            self.data[key] = value
            self.save()

    # ---- strict mode: delayed changes -------------------------------------

    def _delay(self) -> float:
        if not self.data.get("strict_mode"):
            return 0.0
        return float(self.data.get("cooldown_hours", 0)) * 3600

    def request(self, key: str, value, now: float | None = None) -> float | None:
        """Change a setting. Returns the time it takes effect if it was delayed."""
        now = time.time() if now is None else now
        with self._lock:
            old = self.data.get(key)
            if old == value:
                self._drop_pending(lambda p: p.get("op") == "set" and p.get("key") == key)
                self.save()
                return None
            check = _LOOSENING.get(key)
            delay = self._delay() if (check and check(old, value)) else 0.0
            self._drop_pending(lambda p: p.get("op") == "set" and p.get("key") == key)
            if delay <= 0:
                self.data[key] = value
                self.save()
                return None
            at = now + delay
            self.data["pending"].append({"op": "set", "key": key, "value": value, "at": at})
            self.save()
            return at

    def add_block(self, kind: str, value: str) -> str | None:
        """Add a process name or folder. Returns an error message, or None."""
        value = value.strip()
        if not value:
            return "Nothing to add."
        if kind == "names":
            value = value.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if value in PROTECTED_NAMES:
                return f"{value} is a system or shared program and cannot be blocked."
        else:
            reason = unsafe_folder_reason(value)
            if reason:
                return reason
            value = os.path.normcase(os.path.abspath(value))
        with self._lock:
            items = self.data["blocklist"][kind]
            if value not in items:
                items.append(value)
            self._drop_pending(lambda p: p.get("op") == "unblock"
                               and p.get("kind") == kind and p.get("value") == value)
            self.save()
        return None

    def remove_block(self, kind: str, value: str, now: float | None = None) -> float | None:
        """Remove a blocklist entry. Returns when it takes effect if delayed."""
        now = time.time() if now is None else now
        with self._lock:
            delay = self._delay()
            same = lambda p: (p.get("op") == "unblock" and p.get("kind") == kind
                              and p.get("value") == value)
            if delay <= 0:
                self._drop_pending(same)
                items = self.data["blocklist"][kind]
                if value in items:
                    items.remove(value)
                self.save()
                return None
            for p in self.data["pending"]:
                if same(p):
                    return p["at"]
            at = now + delay
            self.data["pending"].append({"op": "unblock", "kind": kind, "value": value, "at": at})
            self.save()
            return at

    def cancel_pending(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self.data["pending"]):
                del self.data["pending"][index]
                self.save()

    def apply_due(self, now: float | None = None) -> list[str]:
        """Apply delayed changes whose cooldown has run out."""
        now = time.time() if now is None else now
        applied = []
        with self._lock:
            keep = []
            for p in self.data["pending"]:
                if p.get("at", 0) > now:
                    keep.append(p)
                    continue
                if p.get("op") == "set":
                    self.data[p["key"]] = p["value"]
                    applied.append(f"{p['key']} is now {p['value']}")
                elif p.get("op") == "unblock":
                    items = self.data["blocklist"].get(p.get("kind"), [])
                    if p.get("value") in items:
                        items.remove(p["value"])
                    applied.append(f"{p.get('value')} left the blocklist")
            if applied:
                self.data["pending"] = keep
                self.save()
        return applied

    def _drop_pending(self, match) -> None:
        self.data["pending"] = [p for p in self.data["pending"] if not match(p)]
