"""Timelapse proof: sample frames from a video and have the AI judge them.

Used when the work happens away from the screen (paper notes, a whiteboard).
Guards against the cheap cheats: an old video, the same video twice, and a
still image dressed up as a video.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

from .judge import JudgeError
from .monitor import earned_seconds

VIDEO_TYPES = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp")
MIN_DURATION_SECONDS = 3.0
MIN_FRAMES = 8
MIN_SESSION_SECONDS = 60
MAX_SESSION_SECONDS = 12 * 3600
STATIC_THRESHOLD = 1.0      # mean pixel change (0-255) below this = a still image
MTIME_SLACK_SECONDS = 300


@dataclass
class TimelapseResult:
    ok: bool
    message: str
    fraction: float = 0.0
    verified: float = 0.0
    earned: float = 0.0
    summary: str = ""


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sample_frames(path: str, count: int = 16, max_edge: int = 768):
    """Return (jpeg_frames, info). info has duration, frame_count and motion."""
    import cv2
    import numpy as np

    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        raise ValueError("That file could not be opened as a video.")
    try:
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        frames = []
        if total >= count:
            wanted = [int(i * (total - 1) / (count - 1)) for i in range(count)]
            for index in wanted:
                capture.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, frame = capture.read()
                if ok and frame is not None:
                    frames.append(frame)
        if len(frames) < min(count, MIN_FRAMES):
            # Seeking failed or the frame count was wrong: read straight through.
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            everything = []
            while len(everything) < 20000:
                ok, frame = capture.read()
                if not ok or frame is None:
                    break
                everything.append(cv2.resize(frame, (320, 180)) if len(everything) > 2000 else frame)
            total = len(everything)
            if total:
                step = max(1, total // count)
                frames = everything[::step][:count]
    finally:
        capture.release()

    duration = total / fps if fps > 0 else 0.0
    small = [cv2.cvtColor(cv2.resize(f, (64, 36)), cv2.COLOR_BGR2GRAY).astype("int16")
             for f in frames]
    motion = max((float(np.abs(a - b).mean()) for a, b in zip(small, small[1:])), default=0.0)

    jpegs = []
    for frame in frames:
        height, width = frame.shape[:2]
        scale = max_edge / max(height, width)
        if scale < 1:
            frame = cv2.resize(frame, (int(width * scale), int(height * scale)))
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
        if ok:
            jpegs.append(encoded.tobytes())
    return jpegs, {"duration": duration, "frame_count": total, "fps": fps, "motion": motion}


def verify_timelapse(cfg, store, session: dict, path: str, judge,
                     now: float | None = None) -> TimelapseResult:
    """Check the video and, if it passes, finish the session and bank the time."""
    now = time.time() if now is None else now
    if judge is None:
        return TimelapseResult(False, "Timelapse proof needs an AI backend. Add a key in Settings.")
    if not os.path.isfile(path):
        return TimelapseResult(False, "That file does not exist.")
    if not path.lower().endswith(VIDEO_TYPES):
        return TimelapseResult(False, "Pick a video file (mp4, mov, avi, mkv or webm).")

    elapsed = now - session["started_at"]
    if elapsed < MIN_SESSION_SECONDS:
        return TimelapseResult(False, "The session has only just started. Do the work first.")
    elapsed = min(elapsed, MAX_SESSION_SECONDS)

    modified = os.path.getmtime(path)
    if modified < session["started_at"] - MTIME_SLACK_SECONDS:
        return TimelapseResult(False, "This video was saved before the session started. "
                                      "Record a new one during the session.")
    max_age = float(cfg.get("timelapse_max_age_hours", 24)) * 3600
    if now - modified > max_age:
        return TimelapseResult(False, "This video is too old to count.")

    digest = file_sha256(path)
    if store.video_used(digest):
        return TimelapseResult(False, "This exact video has already been used as proof.")

    try:
        frames, info = sample_frames(path, int(cfg.get("timelapse_frames", 16)))
    except Exception as err:
        return TimelapseResult(False, f"Could not read the video: {err}")
    if len(frames) < MIN_FRAMES:
        return TimelapseResult(False, "The video is too short to judge. Record a longer timelapse.")
    if 0 < info["duration"] < MIN_DURATION_SECONDS:
        return TimelapseResult(False, "The video is under 3 seconds. Record a longer timelapse.")
    if info["motion"] < STATIC_THRESHOLD:
        return TimelapseResult(False, "Nothing changes in this video, so it reads as a still image.")

    try:
        verdict = judge.judge_frames(session["task"], frames)
    except JudgeError as err:
        return TimelapseResult(False, f"The AI could not check the video: {err}")

    fraction = verdict.fraction
    needed = float(cfg.get("timelapse_pass_fraction", 0.6))
    if fraction < needed:
        return TimelapseResult(
            False, f"Only {fraction:.0%} of the sampled frames looked on-task "
                   f"({needed:.0%} needed). {verdict.summary}".strip(),
            fraction=fraction, summary=verdict.summary)

    verified = elapsed * fraction
    earned = earned_seconds(cfg, verified, session.get("target_min") or 0)
    note = f"timelapse {fraction:.0%} on-task. {verdict.summary}".strip()
    store.finish_session(session["id"], verified=verified, off=elapsed - verified,
                         earned=earned, status="done", note=note, now=now)
    store.mark_video(digest, session["id"], now)
    return TimelapseResult(True, "Timelapse accepted.", fraction, verified, earned, verdict.summary)
