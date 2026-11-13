"""AI judging for the cases the rules cannot settle.

Two interchangeable backends, both called over plain HTTPS so there are no
SDKs to install:
  - Gemini (Google AI Studio key, has a free tier)
  - Claude (Anthropic API key, pay per use)

API keys are read from an environment variable first, then from the system
keyring (Windows Credential Manager). They are never written to config.json.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

KEYRING_SERVICE = "FocusGate"
ENV_KEYS = {"gemini": "FOCUSGATE_GEMINI_KEY", "claude": "FOCUSGATE_ANTHROPIC_KEY"}
BACKEND_LABELS = {"none": "Rules only (no AI)", "gemini": "Gemini (free tier)",
                  "claude": "Claude (paid, pennies)"}


class JudgeError(Exception):
    """The AI could not give a verdict (network, key, quota, bad reply)."""

    def __init__(self, message: str, rate_limited: bool = False):
        super().__init__(message)
        self.rate_limited = rate_limited


@dataclass
class Verdict:
    on_task: bool
    confidence: float = 0.0
    reason: str = ""


@dataclass
class FramesVerdict:
    frames: list[bool] = field(default_factory=list)
    summary: str = ""

    @property
    def fraction(self) -> float:
        return sum(self.frames) / len(self.frames) if self.frames else 0.0


# ---- API key storage -------------------------------------------------------

def get_api_key(backend: str) -> str | None:
    env = os.environ.get(ENV_KEYS.get(backend, ""), "").strip()
    if env:
        return env
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, backend) or None
    except Exception:
        return None


def set_api_key(backend: str, key: str) -> None:
    import keyring
    key = key.strip()
    if key:
        keyring.set_password(KEYRING_SERVICE, backend, key)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, backend)
        except Exception:
            pass


# ---- HTTP ------------------------------------------------------------------

def post_json(url: str, headers: dict, body: dict, timeout: float = 40) -> dict:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            payload = json.loads(err.read().decode("utf-8", "replace"))
            detail = (payload.get("error") or {}).get("message", "")
        except Exception:
            pass
        if err.code == 429:
            raise JudgeError("Rate limit reached. Pausing AI checks for a minute.",
                             rate_limited=True) from None
        if err.code in (401, 403):
            raise JudgeError(f"The API key was rejected ({err.code}). {detail}".strip()) from None
        if err.code == 404:
            raise JudgeError(f"Model not found. Check the model name in Settings. {detail}".strip()) from None
        raise JudgeError(f"AI service error {err.code}. {detail}".strip()) from None
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise JudgeError(f"Could not reach the AI service: {err}") from None
    except ValueError:
        raise JudgeError("The AI service sent a reply that was not JSON.") from None


def extract_json(text: str) -> dict:
    """Pull the JSON object out of a model reply, tolerating code fences."""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise JudgeError("The AI reply had no JSON in it.")
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        raise JudgeError("The AI reply was not valid JSON.") from None
    if not isinstance(value, dict):
        raise JudgeError("The AI reply was not a JSON object.")
    return value


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


# ---- prompts ---------------------------------------------------------------

SCREENSHOT_PROMPT = """You are checking a self-declared focus session. The person said they would work on:
"{task}"

The image is their screen right now. The active window is "{title}" (program: {process}).

Decide whether what is on screen is plausibly part of that task. Be fair: reference material, documentation, lecture or tutorial videos on the topic, note-taking, and AI assistants being used for the task all count. Entertainment, social media, games, shopping, and unrelated browsing or chatting do not.

Reply with JSON only, no other text:
{{"on_task": true or false, "confidence": number from 0 to 1, "reason": "at most 12 words"}}"""

FRAMES_PROMPT = """You are checking a timelapse video that a person recorded as proof of a focus session. They said they would work on:
"{task}"

You are given {count} frames sampled evenly across the video, in order. For each frame, decide whether it plausibly shows the person doing that task (for example at a desk with notes, books or a laptop showing relevant material, or a screen recording of relevant work). A frame showing an empty chair, a phone being scrolled, a game, a TV show, or something unrelated does not count.

Reply with JSON only, no other text:
{{"frames": [true or false for each of the {count} frames, in order], "summary": "one short sentence on what the video shows"}}"""

CAMERA_PROMPT = """You are checking a self-declared focus session through the person's webcam. They said they would work on:
"{task}"

The image is a webcam frame taken just now. Decide two things:
1. Is a person at the desk?
2. Do they plausibly look like they are working? Looking at the screen, reading, writing, typing and thinking all count. Using a phone for leisure, sleeping, watching something off to the side, or an empty seat do not.

You cannot see their screen, so be fair: when a person is present and nothing in the picture contradicts work, answer on_task true.

Reply with JSON only, no other text:
{{"present": true or false, "on_task": true or false, "reason": "at most 12 words"}}"""

TEST_PROMPT = 'Reply with JSON only: {"ok": true}'


# ---- backends --------------------------------------------------------------

class BaseJudge:
    name = "base"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def _call(self, prompt: str, images: list[bytes], max_tokens: int) -> str:
        raise NotImplementedError

    def judge_screenshot(self, task: str, title: str, process: str, jpeg: bytes) -> Verdict:
        prompt = SCREENSHOT_PROMPT.format(task=task, title=title[:200], process=process or "unknown")
        data = extract_json(self._call(prompt, [jpeg], 300))
        if "on_task" not in data:
            raise JudgeError("The AI reply was missing a verdict.")
        try:
            confidence = float(data.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        return Verdict(_as_bool(data["on_task"]), confidence, str(data.get("reason", ""))[:120])

    def judge_camera(self, task: str, jpeg: bytes) -> Verdict:
        data = extract_json(self._call(CAMERA_PROMPT.format(task=task), [jpeg], 300))
        if "on_task" not in data:
            raise JudgeError("The AI reply was missing a verdict.")
        working = _as_bool(data.get("present", True)) and _as_bool(data["on_task"])
        return Verdict(working, 0.0, str(data.get("reason", ""))[:120])

    def judge_frames(self, task: str, frames: list[bytes]) -> FramesVerdict:
        prompt = FRAMES_PROMPT.format(task=task, count=len(frames))
        data = extract_json(self._call(prompt, frames, 800))
        flags = data.get("frames")
        if not isinstance(flags, list) or not flags:
            raise JudgeError("The AI reply was missing per-frame verdicts.")
        return FramesVerdict([_as_bool(f) for f in flags][:len(frames)],
                             str(data.get("summary", ""))[:200])

    def test(self) -> None:
        """Raises JudgeError if the key or model does not work."""
        extract_json(self._call(TEST_PROMPT, [], 100))


class GeminiJudge(BaseJudge):
    name = "gemini"

    def _call(self, prompt: str, images: list[bytes], max_tokens: int) -> str:
        parts = [{"inline_data": {"mime_type": "image/jpeg",
                                  "data": base64.b64encode(img).decode("ascii")}}
                 for img in images]
        parts.append({"text": prompt})
        body = {
            "contents": [{"parts": parts}],
            # Generous output room: some Gemini models spend output tokens on
            # internal thinking before they write the answer.
            "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens + 2000,
                                 "responseMimeType": "application/json"},
        }
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{self.model}:generateContent")
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        reply = post_json(url, headers, body)
        candidates = reply.get("candidates") or []
        if not candidates:
            raise JudgeError("Gemini returned no answer (the request may have been blocked).")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise JudgeError("Gemini returned an empty answer.")
        return text


class ClaudeJudge(BaseJudge):
    name = "claude"

    def _call(self, prompt: str, images: list[bytes], max_tokens: int) -> str:
        content = [{"type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg",
                               "data": base64.b64encode(img).decode("ascii")}}
                   for img in images]
        content.append({"type": "text", "text": prompt})
        body = {"model": self.model, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": content}]}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        reply = post_json("https://api.anthropic.com/v1/messages", headers, body)
        text = "".join(block.get("text", "") for block in reply.get("content", [])
                       if block.get("type") == "text")
        if not text:
            raise JudgeError("Claude returned an empty answer.")
        return text


def make_judge(cfg, backend: str | None = None) -> BaseJudge | None:
    """Build the judge chosen in Settings, or None for rules-only mode."""
    ai = cfg.get("ai", {})
    backend = backend or ai.get("backend", "none")
    if backend == "none":
        return None
    key = get_api_key(backend)
    if not key:
        return None
    if backend == "gemini":
        return GeminiJudge(key, ai.get("gemini_model", "gemini-flash-lite-latest"))
    if backend == "claude":
        return ClaudeJudge(key, ai.get("claude_model", "claude-haiku-5-5"))
    return None
