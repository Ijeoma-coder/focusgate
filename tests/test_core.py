"""Tests for everything that does not need Windows or a real AI key."""
import json
import os
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["FOCUSGATE_HOME"] = tempfile.mkdtemp(prefix="focusgate-test-")

from focusgate import judge as judge_mod  # noqa: E402
from focusgate.blocker import Blocker, is_blocked  # noqa: E402
from focusgate.config import Config, unsafe_folder_reason  # noqa: E402
from focusgate.instance import InstanceLock, is_alive  # noqa: E402
from focusgate.judge import (ClaudeJudge, FramesVerdict, GeminiJudge, JudgeError,  # noqa: E402
                             Verdict, extract_json)
from focusgate.monitor import SessionTracker  # noqa: E402
from focusgate.rules import classify, normalise_title  # noqa: E402
from focusgate.store import Store  # noqa: E402
from focusgate.timelapse import sample_frames, verify_timelapse  # noqa: E402


def fresh():
    folder = Path(tempfile.mkdtemp(prefix="fg-"))
    return Config(folder / "config.json"), Store(folder / "db.sqlite"), folder


class Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now


class FakeJudge:
    def __init__(self, answers=None, error=None):
        self.answers, self.error, self.calls = answers or {}, error, []

    def judge_screenshot(self, task, title, process, jpeg):
        self.calls.append(title)
        if self.error:
            raise self.error
        return Verdict(self.answers.get(title, False), 0.9, "test")

    def judge_frames(self, task, frames):
        self.calls.append(len(frames))
        if self.error:
            raise self.error
        return FramesVerdict(self.answers.get("frames", [True] * len(frames)), "desk with notes")


class RulesTest(unittest.TestCase):
    preset = {"apps": ["code.exe"], "keywords": ["lecture"]}

    def test_classify(self):
        d = ["netflix"]
        self.assertEqual(classify("code.exe", "main.py - Visual Studio Code", self.preset, d), "on")
        self.assertEqual(classify("chrome.exe", "Lecture 4 - YouTube", self.preset, d), "on")
        self.assertEqual(classify("chrome.exe", "Netflix", self.preset, d), "off")
        self.assertEqual(classify("code.exe", "netflix clone - VS Code", self.preset, d), "off")
        self.assertEqual(classify("chrome.exe", "Normalisation explained", self.preset, d), "unknown")
        self.assertEqual(classify("pythonw.exe", "FocusGate", self.preset, d), "self")
        self.assertEqual(classify("", "", self.preset, d), "unknown")

    def test_title_key(self):
        self.assertEqual(normalise_title("(3)  Inbox   - Mail"), "inbox - mail")


class ConfigTest(unittest.TestCase):
    def test_roundtrip_and_merge(self):
        cfg, _, folder = fresh()
        cfg.set("earn_ratio", 0.75)
        (folder / "config.json").write_text(json.dumps({"earn_ratio": 0.75, "ai": {"backend": "gemini"}}))
        again = Config.load(folder / "config.json")
        self.assertEqual(again.get("earn_ratio"), 0.75)
        self.assertEqual(again.get("ai")["claude_model"], "claude-haiku-5-5")  # default kept
        self.assertEqual(again.get("ai")["backend"], "gemini")

    def test_broken_file_falls_back(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "config.json").write_text("{not json")
        cfg = Config.load(folder / "config.json")
        self.assertEqual(cfg.get("earn_ratio"), 0.5)

    def test_protected_names_refused(self):
        cfg, _, _ = fresh()
        self.assertIsNotNone(cfg.add_block("names", "explorer.exe"))
        self.assertIsNone(cfg.add_block("names", r"C:\Games\Hades\Hades.exe"))
        self.assertIn("hades.exe", cfg.get("blocklist")["names"])

    def test_unsafe_folders(self):
        self.assertIsNotNone(unsafe_folder_reason(str(Path.home())))
        self.assertIsNotNone(unsafe_folder_reason(os.path.abspath(os.sep)))
        self.assertIsNone(unsafe_folder_reason(str(Path.home() / "Games" / "Hades")))

    def test_no_cooldown_without_strict(self):
        cfg, _, _ = fresh()
        self.assertIsNone(cfg.remove_block("names", "steam.exe", now=100))
        self.assertNotIn("steam.exe", cfg.get("blocklist")["names"])
        self.assertIsNone(cfg.request("earn_ratio", 2.0, now=100))
        self.assertEqual(cfg.get("earn_ratio"), 2.0)

    def test_strict_cooldown(self):
        cfg, _, _ = fresh()
        cfg.set("strict_mode", True)
        cfg.set("cooldown_hours", 2)
        at = cfg.remove_block("names", "steam.exe", now=1000)
        self.assertEqual(at, 1000 + 7200)
        self.assertIn("steam.exe", cfg.get("blocklist")["names"])          # still blocked
        self.assertEqual(cfg.remove_block("names", "steam.exe", now=2000), at)  # no reset
        self.assertEqual(cfg.request("earn_ratio", 1.0, now=1000), 1000 + 7200)  # easier: waits
        self.assertIsNone(cfg.request("cooldown_hours", 6, now=1000))            # stricter: now
        self.assertIsNotNone(cfg.request("strict_mode", False, now=1000))        # turning off waits
        self.assertEqual(cfg.apply_due(now=5000), [])
        applied = cfg.apply_due(now=1000 + 6 * 3600 + 1)
        self.assertEqual(len(applied), 3)
        self.assertNotIn("steam.exe", cfg.get("blocklist")["names"])
        self.assertEqual(cfg.get("earn_ratio"), 1.0)
        self.assertFalse(cfg.get("strict_mode"))

    def test_readding_cancels_pending_removal(self):
        cfg, _, _ = fresh()
        cfg.set("strict_mode", True)
        cfg.remove_block("names", "steam.exe", now=0)
        cfg.add_block("names", "steam.exe")
        self.assertEqual(cfg.get("pending"), [])


class BankTest(unittest.TestCase):
    def test_unlock_and_refund(self):
        _, store, _ = fresh()
        store.add_bank(45 * 60)
        self.assertFalse(store.start_unlock(60, now=0))       # more than banked
        self.assertTrue(store.start_unlock(30, now=0))
        self.assertEqual(store.bank(), 15 * 60)
        self.assertFalse(store.start_unlock(5, now=10))       # already unlocked
        self.assertEqual(store.unlock_remaining(now=600), 20 * 60)
        self.assertEqual(store.end_unlock(now=600), 20 * 60)
        self.assertEqual(store.bank(), 35 * 60)
        self.assertEqual(store.unlock_remaining(now=601), 0)

    def test_finish_pays_once(self):
        _, store, _ = fresh()
        sid = store.start_session("revise", "Coding", "live", 60, now=0)
        store.finish_session(sid, verified=3600, earned=1800, now=3600)
        store.finish_session(sid, verified=3600, earned=1800, now=3601)
        self.assertEqual(store.bank(), 1800)
        self.assertIsNone(store.active_session())


class BlockerTest(unittest.TestCase):
    def make(self, procs):
        cfg, store, _ = fresh()
        cfg.add_block("folders", str(Path.home() / "Games" / "Hades"))
        clock, killed, notes = Clock(), [], []
        blocker = Blocker(cfg, store, notify=lambda t, m: notes.append(t),
                          lister=lambda: iter(procs), killer=lambda pid: killed.append(pid) or True,
                          clock=clock)
        return cfg, store, clock, killed, notes, blocker

    def test_matching(self):
        bl = {"names": ["steam.exe"], "folders": [os.path.normcase(str(Path.home() / "G"))]}
        self.assertTrue(is_blocked("Steam.exe", "", bl))
        self.assertTrue(is_blocked("game.exe", str(Path.home() / "G" / "bin" / "game.exe"), bl))
        self.assertFalse(is_blocked("game.exe", str(Path.home() / "Gx" / "game.exe"), bl))
        self.assertFalse(is_blocked("explorer.exe", str(Path.home() / "G" / "explorer.exe"), bl))
        self.assertFalse(is_blocked("chrome.exe", "", bl))

    def test_kills_only_when_locked(self):
        game = str(Path.home() / "Games" / "Hades" / "Hades.exe")
        procs = [(10, "chrome.exe", ""), (11, "steam.exe", ""), (12, "Hades.exe", game)]
        cfg, store, clock, killed, notes, blocker = self.make(procs)
        self.assertEqual(sorted(blocker.tick()), ["hades.exe", "steam.exe"])
        self.assertEqual(killed, [11, 12])
        blocker.tick()
        self.assertEqual(notes.count("Locked"), 2)            # no toast spam on repeat kills

        killed.clear()
        store.add_bank(600)
        store.start_unlock(10, now=clock.now)
        self.assertEqual(blocker.tick(), [])                  # unlocked: hands off
        clock.now += 9 * 60
        blocker.tick()
        self.assertIn("Game time almost up", notes)
        clock.now += 61
        self.assertEqual(len(blocker.tick()), 2)              # window over: locked again
        self.assertIn("Game time is over", notes)

    def test_cooldown_applied_by_blocker(self):
        cfg, store, clock, killed, notes, blocker = self.make([(11, "steam.exe", "")])
        cfg.set("strict_mode", True)
        cfg.set("cooldown_hours", 1)
        cfg.remove_block("names", "steam.exe", now=clock.now)
        self.assertEqual(blocker.tick(), ["steam.exe"])
        clock.now += 3601
        self.assertEqual(blocker.tick(), [])


class TrackerTest(unittest.TestCase):
    def make(self, judge=None, **settings):
        cfg, store, _ = fresh()
        cfg.set("spot_check_minutes", 0)
        for key, value in settings.items():
            cfg.set(key, value)
        self.clock = Clock()
        self.window = ("code.exe", "main.py - Visual Studio Code")
        self.idle = 0.0
        sid = store.start_session("revise databases", "Coding", "live", 1, now=self.clock.now)
        tracker = SessionTracker(cfg, store, store.get_session(sid), judge,
                                 lambda: self.window, lambda: b"jpeg", lambda: self.idle,
                                 clock=self.clock, rng=random.Random(1))
        return cfg, store, tracker

    def run_for(self, tracker, seconds, step=5):
        for _ in range(int(seconds / step)):
            self.clock.now += step
            tracker.tick()

    def test_rules_only(self):
        cfg, store, tracker = self.make()
        self.run_for(tracker, 60)
        self.window = ("chrome.exe", "Netflix")
        self.run_for(tracker, 30)
        self.window = ("chrome.exe", "Some random page")
        self.run_for(tracker, 20)
        self.window = ("pythonw.exe", "FocusGate")
        self.run_for(tracker, 10)
        t = tracker.totals
        self.assertEqual((t.verified, t.off, t.unverified, t.paused), (60, 30, 20, 10))
        self.assertTrue(tracker.target_reached)
        summary = tracker.finish()
        self.assertEqual(summary["earned"], 30)
        self.assertEqual(store.bank(), 30)

    def test_ai_verdict_is_cached_and_backdated(self):
        judge = FakeJudge({"Normalisation explained - YouTube": True, "Funny cats - YouTube": False})
        cfg, store, tracker = self.make(judge, ai_min_gap_seconds=20)
        self.window = ("chrome.exe", "Normalisation explained - YouTube")
        self.run_for(tracker, 60)
        self.assertEqual(len(judge.calls), 1)                 # one call, then cached
        self.assertEqual(tracker.totals.verified, 60)
        self.window = ("chrome.exe", "Funny cats - YouTube")
        self.run_for(tracker, 40)                             # first 15 s wait for the AI gap
        self.assertEqual(len(judge.calls), 2)
        self.assertEqual(tracker.totals.off, 40)              # waiting time credited after the fact
        self.assertEqual(tracker.totals.unverified, 0)

    def test_ai_failure_pauses_and_leaves_unverified(self):
        judge = FakeJudge(error=JudgeError("quota", rate_limited=True))
        cfg, store, tracker = self.make(judge)
        self.window = ("chrome.exe", "Mystery page")
        self.run_for(tracker, 50)
        self.assertEqual(len(judge.calls), 1)                 # backed off for 60 s
        self.assertEqual(tracker.totals.unverified, 50)
        self.assertEqual(tracker.totals.verified, 0)

    def test_idle_and_sleep_gap(self):
        cfg, store, tracker = self.make()
        self.run_for(tracker, 20)
        self.idle = 11 * 60
        self.run_for(tracker, 30)
        self.idle = 0
        self.clock.now += 3 * 3600                             # laptop slept
        tracker.tick()
        self.assertEqual(tracker.totals.verified, 20)
        self.assertEqual(tracker.totals.idle, 30)

    def test_spot_check_overrides_allowed_app(self):
        judge = FakeJudge({"main.py - Visual Studio Code": False})
        cfg, store, tracker = self.make(judge, spot_check_minutes=1)
        self.run_for(tracker, 300)
        self.assertGreaterEqual(len(judge.calls), 1)
        self.assertGreater(tracker.totals.off, 0)
        self.assertLess(tracker.totals.verified, 300)

    def test_require_full_target(self):
        cfg, store, tracker = self.make(require_full_target=True)
        self.run_for(tracker, 30)                              # target is 60 s
        self.assertEqual(tracker.finish()["earned"], 0)
        self.assertEqual(store.bank(), 0)

    def test_call_cap(self):
        judge = FakeJudge()
        cfg, store, tracker = self.make(judge, max_ai_calls_per_session=2, ai_min_gap_seconds=0)
        for i in range(6):
            self.window = ("chrome.exe", f"Page {i}")
            self.run_for(tracker, 10)
        self.assertEqual(len(judge.calls), 2)


class JudgeTest(unittest.TestCase):
    def tearDown(self):
        judge_mod.post_json = self.original

    def setUp(self):
        self.original = judge_mod.post_json
        self.sent = []

    def fake(self, reply):
        def post(url, headers, body, timeout=40):
            self.sent.append((url, headers, body))
            return reply
        judge_mod.post_json = post

    def test_extract_json(self):
        self.assertEqual(extract_json('```json\n{"on_task": true}\n```'), {"on_task": True})
        with self.assertRaises(JudgeError):
            extract_json("no json here")

    def test_gemini_request_and_reply(self):
        self.fake({"candidates": [{"content": {"parts": [
            {"text": "thinking", "thought": True},
            {"text": '{"on_task": true, "confidence": 0.8, "reason": "SQL notes"}'}]}}]})
        verdict = GeminiJudge("KEY", "some-model").judge_screenshot("revise", "t", "p", b"\xff\xd8")
        self.assertTrue(verdict.on_task)
        url, headers, body = self.sent[0]
        self.assertIn("models/some-model:generateContent", url)
        self.assertEqual(headers["x-goog-api-key"], "KEY")
        self.assertNotIn("KEY", url)
        parts = body["contents"][0]["parts"]
        self.assertEqual(parts[0]["inline_data"]["mime_type"], "image/jpeg")
        self.assertIn("revise", parts[1]["text"])

    def test_claude_request_and_frames(self):
        self.fake({"content": [{"type": "text",
                                "text": '{"frames": [true, "false", true, true], "summary": "desk"}'}]})
        verdict = ClaudeJudge("KEY", "claude-haiku-5-5").judge_frames("revise", [b"a", b"b", b"c", b"d"])
        self.assertEqual(verdict.frames, [True, False, True, True])
        self.assertEqual(verdict.fraction, 0.75)
        url, headers, body = self.sent[0]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "KEY")
        self.assertEqual(body["model"], "claude-haiku-5-5")
        self.assertEqual([c["type"] for c in body["messages"][0]["content"]], ["image"] * 4 + ["text"])

    def test_empty_reply_is_an_error(self):
        self.fake({"candidates": []})
        with self.assertRaises(JudgeError):
            GeminiJudge("KEY", "m").judge_screenshot("t", "t", "p", b"x")


def make_video(path, frames=60, static=False):
    import cv2
    import numpy as np
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 180))
    for i in range(frames):
        frame = np.full((180, 320, 3), 40, dtype=np.uint8)
        if not static:
            cv2.rectangle(frame, (i * 4, 40), (i * 4 + 60, 140), (200, 180, 90), -1)
        writer.write(frame)
    writer.release()


class TimelapseTest(unittest.TestCase):
    def setUp(self):
        self.cfg, self.store, self.folder = fresh()
        self.start = time.time() - 3600
        sid = self.store.start_session("revise on paper", "", "timelapse", 60, now=self.start)
        self.session = self.store.get_session(sid)
        self.video = self.folder / "proof.mp4"
        make_video(self.video)

    def test_sampling(self):
        frames, info = sample_frames(str(self.video), 16)
        self.assertEqual(len(frames), 16)
        self.assertTrue(frames[0].startswith(b"\xff\xd8"))
        self.assertGreater(info["motion"], 1.0)
        self.assertAlmostEqual(info["duration"], 6.0, delta=0.5)

    def test_accept_and_reject_reuse(self):
        judge = FakeJudge({"frames": [True] * 12 + [False] * 4})
        result = verify_timelapse(self.cfg, self.store, self.session, str(self.video), judge)
        self.assertTrue(result.ok, result.message)
        self.assertAlmostEqual(result.fraction, 0.75)
        self.assertAlmostEqual(result.verified, 3600 * 0.75, delta=5)
        self.assertAlmostEqual(self.store.bank(), 3600 * 0.75 * 0.5, delta=5)
        self.assertIsNone(self.store.active_session())
        sid = self.store.start_session("again", "", "timelapse", 60, now=self.start)
        again = verify_timelapse(self.cfg, self.store, self.store.get_session(sid), str(self.video), judge)
        self.assertFalse(again.ok)
        self.assertIn("already been used", again.message)

    def test_low_fraction_keeps_session_open(self):
        judge = FakeJudge({"frames": [True] * 4 + [False] * 12})
        result = verify_timelapse(self.cfg, self.store, self.session, str(self.video), judge)
        self.assertFalse(result.ok)
        self.assertIsNotNone(self.store.active_session())
        self.assertEqual(self.store.bank(), 0)

    def test_old_video_rejected(self):
        os.utime(self.video, (self.start - 7200, self.start - 7200))
        result = verify_timelapse(self.cfg, self.store, self.session, str(self.video), FakeJudge())
        self.assertIn("before the session started", result.message)

    def test_static_video_rejected(self):
        still = self.folder / "still.mp4"
        make_video(still, static=True)
        result = verify_timelapse(self.cfg, self.store, self.session, str(still), FakeJudge())
        self.assertIn("still image", result.message)

    def test_needs_ai(self):
        result = verify_timelapse(self.cfg, self.store, self.session, str(self.video), None)
        self.assertFalse(result.ok)


class InstanceTest(unittest.TestCase):
    def test_lock(self):
        port = 47999
        first, second = InstanceLock(port), InstanceLock(port)
        self.assertFalse(is_alive(port))
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        self.assertTrue(is_alive(port))
        first.release()
        time.sleep(0.1)
        self.assertFalse(is_alive(port))


class WatchdogTest(unittest.TestCase):
    def test_restarts_only_in_strict_mode(self):
        from focusgate import watchdog
        from focusgate.config import stop_flag
        spawned = []
        cfg = Config.load()
        cfg.set("strict_mode", False)
        watchdog.run(check_seconds=0.01, spawn=lambda: spawned.append(1), max_loops=3)
        self.assertEqual(spawned, [])                          # leaves when strict mode is off
        cfg.set("strict_mode", True)
        watchdog.run(check_seconds=0.01, spawn=lambda: spawned.append(1), max_loops=2)
        self.assertEqual(len(spawned), 2)                      # main app is not running
        stop_flag().write_text("stop")
        watchdog.run(check_seconds=0.01, spawn=lambda: spawned.append(1), max_loops=2)
        self.assertEqual(len(spawned), 2)                      # the stop flag wins
        stop_flag().unlink()
        cfg.set("strict_mode", False)


class FakeCamera:
    """Stands in for the webcam: hands out whatever frame the test sets."""

    def __init__(self):
        self.frame, self.fail, self.opened = None, False, 0

    def open(self):
        from focusgate.camera import CameraError
        if self.fail:
            raise CameraError("no camera")
        self.opened += 1

    def read(self):
        from focusgate.camera import CameraError
        if self.fail:
            raise CameraError("camera unplugged")
        return self.frame

    def close(self):
        pass


def scene(shift=0, dark=False):
    """A fake webcam frame: a bright block on a grey wall, movable sideways."""
    import numpy as np
    frame = np.full((480, 640, 3), 0 if dark else 90, dtype=np.uint8)
    if not dark:
        frame[150:400, 200 + shift:380 + shift] = 200
    return frame


class CameraTest(unittest.TestCase):
    def test_presence_logic(self):
        from focusgate.camera import PresenceLogic
        logic = PresenceLogic(grace_seconds=600, away_after=20)
        self.assertEqual(logic.update(0, False, True), "unknown")      # movement, never saw a face
        self.assertEqual(logic.update(5, True, True), "present")
        self.assertEqual(logic.update(300, False, True), "present")    # head down, within allowance
        self.assertEqual(logic.update(610, False, True), "unknown")    # allowance used up
        logic.confirm(615)                                             # the AI vouched for them
        self.assertEqual(logic.update(620, False, True), "present")
        self.assertEqual(logic.update(630, False, False), "present")   # brief stillness
        self.assertEqual(logic.update(645, False, False), "away")      # still for 20 s+
        self.assertEqual(logic.update(650, True, False), "present")

    def test_motion_ignores_brightness_shift(self):
        from focusgate.camera import MOTION_MIN, motion_fraction, small_gray
        base = small_gray(scene())
        self.assertLess(motion_fraction(base, small_gray(scene())), MOTION_MIN)
        brighter = small_gray((scene().astype("int16") + 15).clip(0, 255).astype("uint8"))
        self.assertLess(motion_fraction(base, brighter), MOTION_MIN)
        self.assertGreater(motion_fraction(base, small_gray(scene(shift=30))), MOTION_MIN)

    def test_face_finder_on_real_photos(self):
        try:
            import cv2
            from skimage import data
        except ImportError:
            self.skipTest("sample photos not installed")
        from focusgate.camera import FaceFinder
        finder = FaceFinder()
        self.assertTrue(finder.has_face(cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)))
        self.assertFalse(finder.has_face(cv2.cvtColor(data.coffee(), cv2.COLOR_RGB2BGR)))
        self.assertFalse(finder.has_face(scene()))

    def make_watcher(self, face=lambda frame: False):
        from focusgate.camera import CameraWatcher
        cfg, _, _ = fresh()
        cam = FakeCamera()
        cam.frame = scene()
        watcher = CameraWatcher(cfg, camera=cam, face_fn=face)
        watcher.start()
        return watcher, cam

    def test_watcher_states(self):
        face = {"on": True}
        watcher, cam = self.make_watcher(lambda frame: face["on"])
        self.assertEqual(watcher.observe(0).state, "present")
        face["on"] = False
        cam.frame = scene(shift=30)
        self.assertEqual(watcher.observe(5).state, "present")          # moving, face seen recently
        self.assertEqual(watcher.observe(10).state, "present")         # brief stillness
        self.assertEqual(watcher.observe(40).state, "away")            # nothing moves, nobody there
        self.assertTrue(watcher.jpeg().startswith(b"\xff\xd8"))
        cam.frame = scene(dark=True)
        self.assertEqual(watcher.observe(45).state, "error")           # lens covered

    def test_watcher_recovers_from_camera_loss(self):
        watcher, cam = self.make_watcher(lambda frame: True)
        cam.fail = True
        self.assertEqual(watcher.observe(0).state, "error")
        cam.fail = False
        self.assertEqual(watcher.observe(10).state, "error")           # waits before retrying
        self.assertEqual(watcher.observe(31).state, "present")
        self.assertEqual(cam.opened, 2)

    def test_start_fails_cleanly_without_camera(self):
        from focusgate.camera import CameraError, CameraWatcher
        cfg, _, _ = fresh()
        cam = FakeCamera()
        cam.fail = True
        with self.assertRaises(CameraError):
            CameraWatcher(cfg, camera=cam, face_fn=lambda f: True).start()


class ScriptedCamera:
    """A camera watcher whose answers the test dictates."""

    def __init__(self):
        from focusgate.camera import Observation
        self.Observation = Observation
        self.state, self.confirmed, self.stopped = "present", [], False

    def observe(self, now):
        return self.Observation(self.state, detail="scripted")

    def confirm(self, now):
        self.confirmed.append(now)

    def jpeg(self):
        return b"jpeg"

    def stop(self):
        self.stopped = True


class CamJudge(FakeJudge):
    def __init__(self, on_task=True, **kw):
        super().__init__(**kw)
        self.on_task, self.cam_calls = on_task, 0

    def judge_camera(self, task, jpeg):
        self.cam_calls += 1
        return Verdict(self.on_task, 0.0, "on phone" if not self.on_task else "writing")


class CameraSessionTest(unittest.TestCase):
    def make(self, mode, judge=None, camera_cfg=None, **settings):
        cfg, store, _ = fresh()
        cfg.set("spot_check_minutes", 0)
        if camera_cfg:
            cfg.set("camera", {**cfg.get("camera"), **camera_cfg})
        for key, value in settings.items():
            cfg.set(key, value)
        self.clock, self.cam = Clock(), ScriptedCamera()
        self.window, self.idle = ("", ""), 0.0
        sid = store.start_session("past papers", "Coding", mode, 60, now=self.clock.now)
        tracker = SessionTracker(cfg, store, store.get_session(sid), judge, lambda: self.window,
                                 lambda: b"jpeg", lambda: self.idle, clock=self.clock,
                                 rng=random.Random(2), camera=self.cam)
        return store, tracker

    def run_for(self, tracker, seconds, step=5):
        for _ in range(int(seconds / step)):
            self.clock.now += step
            tracker.tick()

    def test_camera_only(self):
        store, tracker = self.make("camera")
        self.run_for(tracker, 60)                       # at the desk
        self.cam.state = "away"
        self.run_for(tracker, 30)                       # went to make food
        self.cam.state = "unknown"
        self.run_for(tracker, 20)                       # movement, no face for too long
        self.cam.state = "present"
        self.window = ("chrome.exe", "Netflix")
        self.run_for(tracker, 10)                       # at the desk, but on Netflix
        t = tracker.totals
        self.assertEqual((t.verified, t.idle, t.unverified, t.off), (60, 30, 20, 10))
        self.idle = 3600                                # no keyboard use is fine on paper
        self.window = ("", "")
        self.run_for(tracker, 10)
        self.assertEqual(tracker.totals.verified, 70)
        summary = tracker.finish()
        self.assertTrue(self.cam.stopped)               # camera released
        self.assertEqual(summary["earned"], 35)

    def test_both_presence_replaces_keyboard_idle(self):
        store, tracker = self.make("both")
        self.window = ("code.exe", "main.py - Visual Studio Code")
        self.idle = 3600                                # watching, hands off the keyboard
        self.run_for(tracker, 30)
        self.assertEqual(tracker.totals.verified, 30)   # camera sees you, so it counts
        self.cam.state = "unknown"
        self.run_for(tracker, 20)
        self.assertEqual(tracker.totals.idle, 20)       # camera unsure: keyboard rule is back
        self.cam.state = "away"
        self.idle = 0
        self.run_for(tracker, 10)
        self.assertEqual(tracker.totals.idle, 30)       # empty desk beats an active window
        self.cam.state = "present"
        self.window = ("chrome.exe", "Netflix")
        self.run_for(tracker, 10)
        self.assertEqual(tracker.totals.off, 10)        # present, but the screen is off-task

    def test_camera_ai_off_by_default(self):
        judge = CamJudge()
        store, tracker = self.make("camera", judge)
        self.run_for(tracker, 1800)
        self.assertEqual(judge.cam_calls, 0)            # nothing leaves the laptop

    def test_camera_ai_catches_phone(self):
        judge = CamJudge(on_task=False)
        store, tracker = self.make("camera", judge, camera_cfg={"ai_checks": True, "ai_minutes": 2})
        self.run_for(tracker, 600)
        self.assertGreaterEqual(judge.cam_calls, 2)
        self.assertGreater(tracker.totals.off, 200)
        self.assertLess(tracker.totals.verified, 400)

    def test_camera_ai_vouches_when_face_lost(self):
        judge = CamJudge(on_task=True)
        store, tracker = self.make("camera", judge, camera_cfg={"ai_checks": True, "ai_minutes": 2})
        self.cam.state = "unknown"
        self.run_for(tracker, 300)
        self.assertGreaterEqual(len(self.cam.confirmed), 1)   # AI said working: allowance renewed


class CameraJudgeTest(JudgeTest):
    def test_camera_verdict_needs_presence(self):
        self.fake({"content": [{"type": "text", "text": '{"present": false, "on_task": true, "reason": "empty chair"}'}]})
        self.assertFalse(ClaudeJudge("K", "m").judge_camera("revise", b"x").on_task)
        self.fake({"content": [{"type": "text", "text": '{"present": true, "on_task": true, "reason": "writing"}'}]})
        self.assertTrue(ClaudeJudge("K", "m").judge_camera("revise", b"x").on_task)
        self.assertIn("webcam", self.sent[-1][2]["messages"][0]["content"][1]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
