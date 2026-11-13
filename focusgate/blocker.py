"""The lock: closes blocklisted programs unless an unlock window is running."""
from __future__ import annotations

import os
import threading
import time

from .config import PROTECTED_NAMES

WARN_BEFORE_SECONDS = 120
NOTIFY_EVERY_SECONDS = 300


def list_processes():
    """Yield (pid, name, exe_path) for running processes."""
    import psutil
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        info = proc.info
        yield info.get("pid"), info.get("name") or "", info.get("exe") or ""


def kill_process(pid: int) -> bool:
    import psutil
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except psutil.TimeoutExpired:
            proc.kill()
        return True
    except psutil.NoSuchProcess:
        return True
    except (psutil.AccessDenied, OSError):
        return False


def is_blocked(name: str, exe: str, blocklist: dict) -> bool:
    name = (name or "").lower()
    if not name or name in PROTECTED_NAMES:
        return False
    if name in blocklist.get("names", []):
        return True
    if exe:
        path = os.path.normcase(exe)
        for folder in blocklist.get("folders", []):
            folder = os.path.normcase(folder).rstrip("\\/")
            if folder and path.startswith(folder + os.sep):
                return True
    return False


class Blocker(threading.Thread):
    def __init__(self, cfg, store, notify=None, lister=list_processes,
                 killer=kill_process, clock=time.time, interval: float = 1.5):
        super().__init__(daemon=True, name="focusgate-blocker")
        self.cfg, self.store = cfg, store
        self.notify = notify or (lambda title, message: None)
        self.lister, self.killer, self.clock = lister, killer, clock
        self.interval = interval
        self._halt = threading.Event()
        self._last_notice: dict[str, float] = {}
        self._was_unlocked = False
        self.own_pid = os.getpid()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        while not self._halt.wait(self.interval):
            try:
                self.tick()
            except Exception as err:  # never let the lock die quietly
                self.store.add_event("error", f"blocker: {err}")

    def tick(self) -> list[str]:
        """One pass. Returns the names of programs closed."""
        now = self.clock()
        for change in self.cfg.apply_due(now):
            self.store.add_event("cooldown", change, now)

        remaining = self.store.unlock_remaining(now)
        if remaining > 0:
            self._was_unlocked = True
            if remaining <= WARN_BEFORE_SECONDS and self.store.kv_get("unlock_warned") != "1":
                self.store.kv_set("unlock_warned", "1")
                self.notify("Game time almost up",
                            "About 2 minutes left. Save your game now.")
            return []
        if self._was_unlocked:
            self._was_unlocked = False
            self.notify("Game time is over", "Games are locked again.")

        blocklist = self.cfg.get("blocklist", {})
        closed = []
        for pid, name, exe in self.lister():
            if pid == self.own_pid or not is_blocked(name, exe, blocklist):
                continue
            low = name.lower()
            if self.killer(pid):
                closed.append(low)
            else:
                self._notice(low, now, "FocusGate could not close it",
                             f"{name} is running with higher rights. "
                             "Run FocusGate as administrator to block it.", "blocked_failed")
        for low in sorted(set(closed)):
            self._notice(low, now, "Locked", f"{low} was closed. Bank some focus time "
                         "to unlock games.", "blocked")
        return closed

    def _notice(self, name: str, now: float, title: str, message: str, kind: str) -> None:
        if now - self._last_notice.get(name, 0) < NOTIFY_EVERY_SECONDS:
            return
        self._last_notice[name] = now
        self.store.add_event(kind, name, now)
        self.notify(title, message)
