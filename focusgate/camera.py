"""Webcam presence: is someone at the desk?

Everything here runs on the laptop. A small face-detection model (YuNet,
bundled in models/) looks at one frame every few seconds, and a simple
frame-to-frame comparison notices movement. Frames are held in memory only
and are never written to disk.

Presence rule:
  - a face in the frame                          -> present
  - movement, and a face seen in the last N min  -> present (head down, reading)
  - movement, but no face for longer than that   -> unknown (not counted)
  - a still scene for 20 seconds                 -> away (clock paused)
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

MODEL = Path(__file__).resolve().parent / "models" / "yunet_n_640_640.onnx"
DETECT_SIZE = (320, 240)
FACE_SCORE = 0.6
MOTION_LEVELS = 12        # brightness change (0-255) that counts as a changed pixel
MOTION_MIN = 0.004        # share of changed pixels that counts as movement
AWAY_AFTER_SECONDS = 20
DARK_LEVEL = 8            # mean brightness below this = lens covered
REOPEN_EVERY_SECONDS = 30


class CameraError(Exception):
    pass


@dataclass
class Observation:
    state: str                # present | unknown | away | error
    face: bool = False
    motion: float = 0.0
    detail: str = ""


class FaceFinder:
    """YuNet when OpenCV supports it, the older Haar cascade otherwise."""

    def __init__(self):
        import cv2
        self._cv2 = cv2
        self.kind = None
        self._detector = None
        if hasattr(cv2, "FaceDetectorYN") and MODEL.exists():
            try:
                # Loading from bytes avoids trouble with unusual characters in the folder path.
                self._detector = cv2.FaceDetectorYN.create(
                    "onnx", MODEL.read_bytes(), b"", DETECT_SIZE, FACE_SCORE, 0.3, 50)
                self.kind = "yunet"
            except Exception:
                try:
                    self._detector = cv2.FaceDetectorYN.create(
                        str(MODEL), "", DETECT_SIZE, FACE_SCORE, 0.3, 50)
                    self.kind = "yunet"
                except Exception:
                    self._detector = None
        if self._detector is None and hasattr(cv2, "CascadeClassifier"):
            try:
                cascade = cv2.CascadeClassifier(
                    cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
                if not cascade.empty():
                    self._detector, self.kind = cascade, "haar"
            except Exception:
                pass
        if self._detector is None:
            raise CameraError("Face detection is not available in this OpenCV install. "
                              "Run setup.bat again.")

    def has_face(self, frame) -> bool:
        cv2 = self._cv2
        small = cv2.resize(frame, DETECT_SIZE)
        if self.kind == "yunet":
            try:
                _, faces = self._detector.detect(small)
            except Exception:
                return False
            return faces is not None and len(faces) > 0
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return len(self._detector.detectMultiScale(gray, 1.15, 4, minSize=(36, 36))) > 0


def small_gray(frame):
    """Tiny blurred greyscale copy used for movement comparison."""
    import cv2
    gray = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(gray, (5, 5), 0).astype("int16")


def motion_fraction(before, after) -> float:
    """Share of pixels that changed, ignoring an overall brightness shift."""
    import numpy as np
    diff = np.abs((after - after.mean()) - (before - before.mean()))
    return float((diff > MOTION_LEVELS).mean())


def to_jpeg(frame, max_edge: int = 640, quality: int = 65) -> bytes:
    import cv2
    height, width = frame.shape[:2]
    scale = max_edge / max(height, width)
    if scale < 1:
        frame = cv2.resize(frame, (int(width * scale), int(height * scale)))
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise CameraError("Could not encode the camera frame.")
    return encoded.tobytes()


class Camera:
    """Thin wrapper over the webcam."""

    def __init__(self, index: int = 0):
        self.index = index
        self._cap = None

    def open(self) -> None:
        import cv2
        self.close()
        backends = [cv2.CAP_DSHOW, cv2.CAP_ANY] if sys.platform == "win32" else [cv2.CAP_ANY]
        cap = None
        for backend in backends:
            cap = cv2.VideoCapture(self.index, backend)
            if cap.isOpened():
                break
            cap.release()
            cap = None
        if cap is None:
            raise CameraError("No camera was found, or another app is using it. Close other "
                              "apps that use the camera and check Windows camera privacy settings.")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        got = False
        for _ in range(10):          # the first frames are often dark or empty
            ok, frame = cap.read()
            got = got or (ok and frame is not None)
        if not got:
            cap.release()
            raise CameraError("The camera opened but sent no picture.")
        self._cap = cap

    def read(self):
        if self._cap is None:
            raise CameraError("The camera is not open.")
        for _ in range(2):           # skip frames that sat in the buffer
            self._cap.grab()
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise CameraError("The camera stopped sending pictures.")
        return frame

    def close(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


class PresenceLogic:
    """Turns (face?, movement?) readings into present / unknown / away."""

    def __init__(self, grace_seconds: float, away_after: float = AWAY_AFTER_SECONDS):
        self.grace = grace_seconds
        self.away_after = away_after
        self.last_confirmed: float | None = None   # face seen, or the AI said "working"
        self.last_activity: float | None = None
        self.state = "unknown"

    def confirm(self, now: float) -> None:
        self.last_confirmed = now
        self.last_activity = now

    def update(self, now: float, face: bool, moving: bool) -> str:
        if face:
            self.confirm(now)
            self.state = "present"
        elif moving:
            self.last_activity = now
            recent = self.last_confirmed is not None and now - self.last_confirmed <= self.grace
            self.state = "present" if recent else "unknown"
        elif self.last_activity is None or now - self.last_activity >= self.away_after:
            self.state = "away"
        # else: a brief still moment, keep the previous state
        return self.state


class CameraWatcher:
    def __init__(self, cfg, camera=None, face_fn=None, clock=time.time):
        settings = cfg.get("camera", {})
        self.camera = camera or Camera(int(settings.get("index", 0)))
        self._face_fn = face_fn
        self.clock = clock
        self.logic = PresenceLogic(float(settings.get("face_grace_minutes", 10)) * 60)
        self._previous = None
        self._frame = None
        self._retry_at = 0.0
        self.open = False

    def start(self) -> None:
        """Raises CameraError if the camera or the face detector is unavailable."""
        if self._face_fn is None:
            self._face_fn = FaceFinder().has_face
        self.camera.open()
        self.open = True

    def stop(self) -> None:
        self.open = False
        self.camera.close()
        self._frame = None

    def confirm(self, now: float) -> None:
        self.logic.confirm(now)

    def jpeg(self) -> bytes:
        if self._frame is None:
            raise CameraError("No camera frame yet.")
        return to_jpeg(self._frame)

    def observe(self, now: float | None = None) -> Observation:
        now = self.clock() if now is None else now
        try:
            if not self.open:
                if now < self._retry_at:
                    return Observation("error", detail="Camera unavailable. Trying again shortly.")
                self.camera.open()
                self.open = True
            frame = self.camera.read()
        except CameraError as err:
            self.open = False
            self._retry_at = now + REOPEN_EVERY_SECONDS
            self.camera.close()
            return Observation("error", detail=str(err))

        self._frame = frame
        gray = small_gray(frame)
        if float(gray.mean()) < DARK_LEVEL:
            self._previous = gray
            return Observation("error", detail="The camera picture is black. Is the lens covered?")
        face = bool(self._face_fn(frame))
        motion = motion_fraction(self._previous, gray) if self._previous is not None else 0.0
        self._previous = gray
        state = self.logic.update(now, face, motion >= MOTION_MIN)
        detail = ""
        if state == "unknown":
            detail = "Movement, but no face seen for a while. Glance up at the camera."
        return Observation(state, face, motion, detail)
