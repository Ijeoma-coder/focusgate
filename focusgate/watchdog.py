"""Strict-mode watchdog: restarts the main app if it is killed.

Runs as its own small process. It leaves on its own when strict mode is off
or when the STOP flag appears (see stop.bat), so there is always a clean way
out.
"""
from __future__ import annotations

import time

from . import winutil
from .config import MAIN_PORT, WATCHDOG_PORT, Config, stop_flag
from .instance import InstanceLock, is_alive

CHECK_SECONDS = 5
GRACE_AFTER_RESTART = 20


def run(check_seconds: float = CHECK_SECONDS, spawn=None, max_loops: int | None = None) -> None:
    lock = InstanceLock(WATCHDOG_PORT)
    if not lock.acquire():
        return  # another watchdog is already running
    spawn = spawn or (lambda: winutil.spawn_detached(winutil.launcher_command("--minimized")))
    loops = 0
    try:
        while max_loops is None or loops < max_loops:
            loops += 1
            time.sleep(check_seconds)
            if stop_flag().exists():
                return
            if not Config.load().get("strict_mode", False):
                return
            if not is_alive(MAIN_PORT):
                spawn()
                time.sleep(GRACE_AFTER_RESTART if max_loops is None else 0)
    finally:
        lock.release()
