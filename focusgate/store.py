"""SQLite storage: the game-time bank, focus sessions, checks and events."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL, preset TEXT, mode TEXT NOT NULL, target_min INTEGER,
    started_at REAL NOT NULL, ended_at REAL,
    verified_sec REAL DEFAULT 0, off_sec REAL DEFAULT 0,
    unverified_sec REAL DEFAULT 0, idle_sec REAL DEFAULT 0,
    earned_sec REAL DEFAULT 0, status TEXT DEFAULT 'active', note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts REAL,
    process TEXT, title TEXT, verdict TEXT, source TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS videos (hash TEXT PRIMARY KEY, session_id INTEGER, ts REAL);
"""


class Store:
    def __init__(self, path: Path | str):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _run(self, sql: str, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _all(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # ---- key/value ---------------------------------------------------------

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        rows = self._all("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def kv_set(self, key: str, value) -> None:
        self._run("INSERT INTO kv(key, value) VALUES(?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def _kv_float(self, key: str) -> float:
        try:
            return float(self.kv_get(key, "0") or 0)
        except ValueError:
            return 0.0

    # ---- bank and unlock window -------------------------------------------

    def bank(self) -> float:
        """Seconds of game time earned and not yet spent."""
        return max(0.0, self._kv_float("bank_sec"))

    def add_bank(self, seconds: float) -> None:
        with self._lock:
            self.kv_set("bank_sec", max(0.0, self.bank() + seconds))

    def unlock_remaining(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, self._kv_float("unlock_until") - now)

    def start_unlock(self, minutes: float, now: float | None = None) -> bool:
        """Spend banked time on an unlock window. False if the bank is short."""
        now = time.time() if now is None else now
        seconds = minutes * 60
        with self._lock:
            if seconds <= 0 or seconds > self.bank() + 0.5:
                return False
            if self.unlock_remaining(now) > 0:
                return False
            self.kv_set("bank_sec", max(0.0, self.bank() - seconds))
            self.kv_set("unlock_until", now + seconds)
            self.kv_set("unlock_warned", "0")
        self.add_event("unlock", f"{minutes:g} min of game time started", now)
        return True

    def end_unlock(self, now: float | None = None) -> float:
        """End the unlock window early and refund what is left."""
        now = time.time() if now is None else now
        with self._lock:
            left = self.unlock_remaining(now)
            self.kv_set("unlock_until", 0)
            if left > 0:
                self.add_bank(left)
        if left > 0:
            self.add_event("unlock_end", f"ended early, {left / 60:.0f} min refunded", now)
        return left

    # ---- sessions ----------------------------------------------------------

    def start_session(self, task: str, preset: str, mode: str, target_min: int,
                      now: float | None = None) -> int:
        now = time.time() if now is None else now
        cur = self._run("INSERT INTO sessions(task, preset, mode, target_min, started_at) "
                        "VALUES(?,?,?,?,?)", (task, preset, mode, target_min, now))
        return cur.lastrowid

    def get_session(self, session_id: int) -> dict | None:
        rows = self._all("SELECT * FROM sessions WHERE id=?", (session_id,))
        return rows[0] if rows else None

    def active_session(self) -> dict | None:
        rows = self._all("SELECT * FROM sessions WHERE status='active' ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def save_progress(self, session_id: int, verified: float, off: float,
                      unverified: float, idle: float) -> None:
        self._run("UPDATE sessions SET verified_sec=?, off_sec=?, unverified_sec=?, idle_sec=? "
                  "WHERE id=?", (verified, off, unverified, idle, session_id))

    def finish_session(self, session_id: int, *, verified: float = 0, off: float = 0,
                       unverified: float = 0, idle: float = 0, earned: float = 0,
                       status: str = "done", note: str = "", now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            row = self.get_session(session_id)
            if not row or row["status"] != "active":
                return
            self._run("UPDATE sessions SET ended_at=?, verified_sec=?, off_sec=?, "
                      "unverified_sec=?, idle_sec=?, earned_sec=?, status=?, note=? WHERE id=?",
                      (now, verified, off, unverified, idle, earned, status, note, session_id))
            if earned > 0:
                self.add_bank(earned)

    def recent_sessions(self, limit: int = 50) -> list[dict]:
        return self._all("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))

    # ---- checks, events, videos -------------------------------------------

    def add_check(self, session_id: int, ts: float, process: str, title: str,
                  verdict: str, source: str, reason: str = "") -> None:
        self._run("INSERT INTO checks(session_id, ts, process, title, verdict, source, reason) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (session_id, ts, process, title[:200], verdict, source, reason[:200]))

    def recent_checks(self, session_id: int, limit: int = 20) -> list[dict]:
        return self._all("SELECT * FROM checks WHERE session_id=? ORDER BY id DESC LIMIT ?",
                         (session_id, limit))

    def add_event(self, kind: str, detail: str = "", now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._run("INSERT INTO events(ts, kind, detail) VALUES(?,?,?)", (now, kind, detail[:300]))

    def recent_events(self, limit: int = 50) -> list[dict]:
        return self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def video_used(self, digest: str) -> bool:
        return bool(self._all("SELECT 1 FROM videos WHERE hash=?", (digest,)))

    def mark_video(self, digest: str, session_id: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._run("INSERT OR IGNORE INTO videos(hash, session_id, ts) VALUES(?,?,?)",
                  (digest, session_id, now))
