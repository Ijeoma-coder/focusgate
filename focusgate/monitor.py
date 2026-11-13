"""Live focus sessions: turn what the screen and camera show into verified minutes.

Screen: every few seconds the tracker looks at the active window. The rules
settle most windows for free. A window the rules cannot place is
screenshotted once and sent to the AI; the verdict is remembered for that
window title, and time already spent there is credited (or not) after the
fact. A random spot check keeps "allowed" apps honest.

Camera: a local presence check (see camera.py) says whether someone is at the
desk. An empty desk pauses the clock. Optionally, an occasional frame goes to
the AI to check the person looks like they are working.

Proof modes: "live" = screen, "camera" = camera, "both" = screen and camera.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass

from .judge import JudgeError
from .rules import classify, normalise_title

LIVE_MODES = ("live", "camera", "both")
OFF_HOLD_SECONDS = 120


@dataclass
class Totals:
    verified: float = 0.0     # on-task, counts toward game time
    off: float = 0.0          # off-task
    unverified: float = 0.0   # nobody could say; does not count
    idle: float = 0.0         # away from the desk, or no keyboard/mouse input
    paused: float = 0.0       # looking at FocusGate itself


class SessionTracker:
    def __init__(self, cfg, store, session: dict, judge, window_provider,
                 screenshot_provider, idle_provider, clock=time.time,
                 rng: random.Random | None = None, on_event=None, camera=None):
        self.cfg, self.store, self.session, self.judge = cfg, store, session, judge
        self.window_provider = window_provider
        self.screenshot_provider = screenshot_provider
        self.idle_provider = idle_provider
        self.clock = clock
        self.rng = rng or random.Random()
        self.on_event = on_event or (lambda message: None)
        self.camera = camera
        self.mode = session.get("mode") or "live"
        self.use_screen = self.mode in ("live", "both")

        presets = cfg.get("presets", {})
        self.preset = presets.get(session.get("preset") or "", {"apps": [], "keywords": []})
        self.distractions = cfg.get("distractions", [])
        self.poll = float(cfg.get("poll_seconds", 5))
        self.idle_limit = float(cfg.get("idle_limit_minutes", 10)) * 60
        self.ai_gap = float(cfg.get("ai_min_gap_seconds", 20))
        self.ai_max = int(cfg.get("max_ai_calls_per_session", 60))
        self.spot_minutes = float(cfg.get("spot_check_minutes", 10))
        cam = cfg.get("camera", {})
        self.cam_ai_minutes = float(cam.get("ai_minutes", 5)) if cam.get("ai_checks") else 0.0

        self.totals = Totals()
        self.cache: dict[tuple, bool] = {}        # AI verdict per (process, title)
        self.pending: dict[tuple, float] = {}     # unverified seconds per window
        self.override_until: dict[tuple, float] = {}
        self.ai_calls = 0
        self.last_ai = 0.0
        self.ai_paused_until = 0.0
        self.camera_off_until = 0.0
        self.camera_reason = ""
        self.status = ("starting", "")
        self.target_reached = False
        self._last_logged = None
        self._lock = threading.Lock()

        now = self.clock()
        self.last_tick = now
        self.next_spot = self._jittered(now, self.spot_minutes)
        self.next_cam_ai = self._jittered(now, self.cam_ai_minutes)

    # ---- helpers -----------------------------------------------------------

    def _jittered(self, now: float, minutes: float) -> float:
        if minutes <= 0:
            return float("inf")
        return now + minutes * 60 * self.rng.uniform(0.6, 1.4)

    def _ai_ready(self, now: float) -> bool:
        return (self.judge is not None and self.ai_calls < self.ai_max
                and now >= self.ai_paused_until)

    def _ask(self, now: float, source: str, label: str, call):
        """One image to the AI. Returns a Verdict, or None if it failed."""
        self.last_ai = now
        try:
            verdict = call()
        except JudgeError as err:
            self.ai_paused_until = now + (60 if err.rate_limited else 30)
            self.on_event(f"AI check skipped: {err}")
            return None
        except Exception as err:
            self.ai_paused_until = now + 30
            self.on_event(f"AI check failed: {err}")
            return None
        self.ai_calls += 1
        kind = "on" if verdict.on_task else "off"
        process, title = label if isinstance(label, tuple) else ("camera", label)
        self.store.add_check(self.session["id"], now, process, title, kind, source, verdict.reason)
        where = "Camera" if source == "camera" else "AI"
        self.on_event(f"{where}: {'on task' if verdict.on_task else 'off task'}"
                      f" ({verdict.reason or title[:50]})")
        return verdict

    def _ask_screen(self, process: str, title: str, now: float, source: str):
        task = self.session["task"]
        return self._ask(now, source, (process, title), lambda: self.judge.judge_screenshot(
            task, title, process, self.screenshot_provider()))

    def _log_change(self, now: float, process: str, title: str, kind: str, source: str) -> None:
        marker = (process, normalise_title(title), kind)
        if marker != self._last_logged:
            self._last_logged = marker
            if not source.startswith("ai-call"):
                self.store.add_check(self.session["id"], now, process, title, kind, source)

    def _credit(self, kind: str, dt: float, detail: str) -> None:
        if kind == "on":
            self.totals.verified += dt
        elif kind == "off":
            self.totals.off += dt
        elif kind == "idle":
            self.totals.idle += dt
        elif kind == "paused":
            self.totals.paused += dt
        else:
            kind = "unknown"
            self.totals.unverified += dt
        self.status = (kind, detail)

    def _wrap_up(self) -> None:
        target = (self.session.get("target_min") or 0) * 60
        if target and not self.target_reached and self.totals.verified >= target:
            self.target_reached = True
            self.on_event("Target reached. Finish to bank your game time, or keep going.")
        self.store.save_progress(self.session["id"], self.totals.verified, self.totals.off,
                                 self.totals.unverified, self.totals.idle)

    # ---- the loop body -----------------------------------------------------

    def tick(self) -> None:
        with self._lock:
            self._tick()
            self._wrap_up()

    def _tick(self) -> None:
        now = self.clock()
        dt = now - self.last_tick
        self.last_tick = now
        # A long gap means the laptop slept or the app stalled: count nothing.
        if dt < 0 or dt > max(30.0, self.poll * 6):
            dt = 0.0

        seen_at_desk = False
        if self.camera is not None:
            verdict = self._camera_step(now, dt)
            if verdict == "done":
                return
            seen_at_desk = verdict == "present"

        if not self.use_screen:
            # Camera-only proof. The one free screen check: an obvious distraction
            # in the active window still counts against you.
            process, title = self.window_provider()
            if classify(process, title, self.preset, self.distractions) == "off":
                self._credit("off", dt, title or process)
                self._log_change(now, process, title, "off", "rule")
            else:
                self._credit("on", dt, "At your desk")
            return

        # With the camera confirming you are there, a quiet keyboard is fine
        # (watching a lecture, reading). Without it, no input pauses the clock.
        if not seen_at_desk and self.idle_provider() >= self.idle_limit:
            self._credit("idle", dt, "No keyboard or mouse input, so the clock is paused")
            return
        self._screen_step(now, dt)

    def _camera_step(self, now: float, dt: float) -> str:
        """Returns 'done' if this tick is settled, else 'present' or 'unsure'."""
        seen = self.camera.observe(now)
        if seen.state == "away":
            self._credit("idle", dt, "No one at the desk, so the clock is paused")
            return "done"

        if (seen.state in ("present", "unknown") and now >= self.next_cam_ai
                and now >= self.camera_off_until and self._ai_ready(now)):
            task = self.session["task"]
            verdict = self._ask(now, "camera", "webcam frame",
                                lambda: self.judge.judge_camera(task, self.camera.jpeg()))
            if verdict is not None:
                self.next_cam_ai = self._jittered(now, self.cam_ai_minutes)
                if verdict.on_task:
                    self.camera.confirm(now)
                    seen.state = "present"
                else:
                    # Hold off-task briefly, then look again.
                    self.camera_off_until = now + OFF_HOLD_SECONDS
                    self.camera_reason = verdict.reason
                    self.next_cam_ai = now + OFF_HOLD_SECONDS

        if now < self.camera_off_until:
            self._credit("off", dt, f"Camera: {self.camera_reason or 'not working'}")
            return "done"
        if seen.state == "present":
            return "present"
        if not self.use_screen:
            self._credit("unknown", dt, seen.detail or "The camera could not confirm you are there")
            return "done"
        return "unsure"

    def _screen_step(self, now: float, dt: float) -> None:
        process, title = self.window_provider()
        key = (process, normalise_title(title))
        kind = classify(process, title, self.preset, self.distractions)
        source = "rule"

        if kind == "self":
            self._credit("paused", dt, "Looking at FocusGate")
            return

        if kind == "on" and self.override_until.get(key, 0) > now:
            kind, source = "off", "ai"
        elif kind == "unknown" and key in self.cache:
            kind, source = ("on" if self.cache[key] else "off"), "ai"

        if kind == "on" and now >= self.next_spot and self._ai_ready(now):
            verdict = self._ask_screen(process, title, now, "spot-check")
            if verdict is not None:
                self.next_spot = self._jittered(now, self.spot_minutes)
                if not verdict.on_task:
                    kind = "off"
                    if source == "ai":
                        self.cache[key] = False
                    else:
                        # The rules allow this app, but the screen said otherwise.
                        self.override_until[key] = now + OFF_HOLD_SECONDS
                        self.next_spot = now + OFF_HOLD_SECONDS
                source = "ai-call"
        elif kind == "unknown" and self._ai_ready(now) and now - self.last_ai >= self.ai_gap:
            verdict = self._ask_screen(process, title, now, "ai")
            if verdict is not None:
                self.cache[key] = verdict.on_task
                kind, source = ("on" if verdict.on_task else "off"), "ai-call"
                # Credit the time already spent on this window while waiting.
                waited = self.pending.pop(key, 0.0)
                self.totals.unverified -= waited
                if verdict.on_task:
                    self.totals.verified += waited
                else:
                    self.totals.off += waited

        if kind == "unknown":
            self.pending[key] = self.pending.get(key, 0.0) + dt
        self._credit(kind, dt, title or process)
        self._log_change(now, process, title, kind, source)

    # ---- ending ------------------------------------------------------------

    def close(self) -> None:
        """Release the camera."""
        if self.camera is not None:
            try:
                self.camera.stop()
            except Exception:
                pass

    def finish(self, status: str = "done") -> dict:
        """Close the session and pay out. Returns a summary."""
        with self._lock:
            self.close()
            earned = earned_seconds(self.cfg, self.totals.verified,
                                    self.session.get("target_min") or 0)
            note = ""
            if earned == 0 and self.totals.verified > 0:
                note = "target not reached"
            self.store.finish_session(
                self.session["id"], verified=self.totals.verified, off=self.totals.off,
                unverified=self.totals.unverified, idle=self.totals.idle,
                earned=earned, status=status, note=note, now=self.clock())
            return {"verified": self.totals.verified, "off": self.totals.off,
                    "unverified": self.totals.unverified, "idle": self.totals.idle,
                    "earned": earned, "ai_calls": self.ai_calls, "note": note}


def earned_seconds(cfg, verified: float, target_min: float) -> float:
    if cfg.get("require_full_target", False) and verified + 1 < target_min * 60:
        return 0.0
    return max(0.0, verified * float(cfg.get("earn_ratio", 0.5)))


class MonitorThread(threading.Thread):
    def __init__(self, tracker: SessionTracker, on_tick=None):
        super().__init__(daemon=True, name="focusgate-monitor")
        self.tracker = tracker
        self.on_tick = on_tick or (lambda: None)
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        while not self._halt.wait(self.tracker.poll):
            try:
                self.tracker.tick()
            except Exception as err:
                self.tracker.on_event(f"Monitor error: {err}")
            self.on_tick()
