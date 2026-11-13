"""The FocusGate window and tray icon."""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import winutil
from .blocker import Blocker
from .camera import Camera, CameraError, CameraWatcher, FaceFinder
from .config import (MAIN_PORT, PROTECTED_NAMES, WATCHDOG_PORT, Config, data_dir,
                     stop_flag)
from .instance import InstanceLock, is_alive
from .judge import BACKEND_LABELS, JudgeError, get_api_key, make_judge, set_api_key
from .monitor import LIVE_MODES, MonitorThread, SessionTracker, earned_seconds
from .store import Store
from .timelapse import VIDEO_TYPES, verify_timelapse

FONT = "Segoe UI"
GOOD, WARN, BAD = "#3fb27f", "#d9a441", "#e06c5b"
PROOF_LABELS = {"live": "Screen", "camera": "Camera", "both": "Screen + camera",
                "timelapse": "Timelapse"}
STATUS_TEXT = {"on": "On task", "off": "Off task", "unknown": "Not verified yet",
               "idle": "Paused", "paused": "Paused", "starting": "Starting"}
STATUS_COLOUR = {"on": GOOD, "off": BAD, "unknown": WARN, "idle": WARN,
                 "paused": WARN, "starting": WARN}


def fmt_min(seconds: float) -> str:
    minutes = int(max(0, seconds) // 60)
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60:02d} min"
    return f"{minutes} min"


def fmt_clock(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def fmt_wait(seconds: float) -> str:
    minutes = int(max(0, seconds) // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{max(1, minutes)} min"


def lines_of(widget: tk.Text) -> list[str]:
    return [ln.strip() for ln in widget.get("1.0", "end").splitlines() if ln.strip()]


class App:
    def __init__(self, minimized: bool = False):
        winutil.enable_dpi_awareness()
        self.cfg = Config.load()
        self.store = Store(data_dir() / "focusgate.db")
        self.ui_queue: queue.Queue = queue.Queue()
        self.tracker: SessionTracker | None = None
        self.monitor: MonitorThread | None = None
        self.busy = False
        self.tray = None
        self._shown = {}
        self._quitting = False

        self._note_unclean_gap()
        self._recover_session()

        self.root = tk.Tk()
        self.root.title("FocusGate")
        self.root.geometry("780x760")
        self.root.minsize(700, 640)
        self._apply_theme()
        self._build()
        self._start_tray()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self.blocker = Blocker(self.cfg, self.store, notify=self.notify)
        self.blocker.start()

        self.root.after(100, self._pump)
        self.root.after(200, self._refresh)
        self.root.after(1000, self._heartbeat)
        if minimized:
            self.root.after(0, self.hide_window)
        self.log("FocusGate is running. Games on the blocklist are locked.")

    # ---- start-up checks ---------------------------------------------------

    def _note_unclean_gap(self) -> None:
        """If the app was killed while the PC stayed on, say so in the history."""
        try:
            last = float(self.store.kv_get("heartbeat", "0") or 0)
            clean = self.store.kv_get("clean_exit", "1") == "1"
            if last and not clean:
                import psutil
                gap = time.time() - last
                if psutil.boot_time() < last and gap > 120:
                    self.store.add_event("gap", f"FocusGate was not running for {fmt_wait(gap)} "
                                                "while the PC was on")
        except Exception:
            pass
        self.store.kv_set("clean_exit", "0")

    def _recover_session(self) -> None:
        """A live session cannot survive a restart: close it and pay what was verified."""
        row = self.store.active_session()
        if row and row["mode"] in LIVE_MODES:
            earned = earned_seconds(self.cfg, row["verified_sec"], row["target_min"] or 0)
            self.store.finish_session(
                row["id"], verified=row["verified_sec"], off=row["off_sec"],
                unverified=row["unverified_sec"], idle=row["idle_sec"], earned=earned,
                status="interrupted", note="app closed mid-session")

    # ---- cross-thread plumbing --------------------------------------------

    def call_ui(self, fn) -> None:
        self.ui_queue.put(fn)

    def _pump(self) -> None:
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        if not self._quitting:
            self.root.after(100, self._pump)

    def notify(self, title: str, message: str) -> None:
        """Tray toast plus a line in the activity log. Safe from any thread."""
        self.call_ui(lambda: self.log(f"{title}: {message}"))
        if self.tray is not None:
            try:
                self.tray.notify(message, title)
            except Exception:
                pass

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{stamp}  {message}\n")
        if int(self.log_box.index("end-1c").split(".")[0]) > 200:
            self.log_box.delete("1.0", "2.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ---- look --------------------------------------------------------------

    def _apply_theme(self) -> None:
        style = ttk.Style(self.root)
        try:
            import sv_ttk
            sv_ttk.set_theme("dark")
            self.bg, self.fg = "#1c1c1c", "#f2f2f2"
            self.root.configure(bg=self.bg)
        except Exception:
            # No theme package: fall back to the system look.
            self.bg = style.lookup("TFrame", "background") or self.root.cget("bg")
            self.fg = style.lookup("TLabel", "foreground") or "black"
        style.configure("Big.TLabel", font=(FONT, 30, "bold"))
        style.configure("Sub.TLabel", font=(FONT, 10))
        style.configure("Head.TLabel", font=(FONT, 12, "bold"))
        style.configure("State.TLabel", font=(FONT, 11, "bold"))

    def _text(self, parent, height: int, **kw) -> tk.Text:
        return tk.Text(parent, height=height, wrap="word", relief="flat", bg=self.bg,
                       fg=self.fg, insertbackground=self.fg, highlightthickness=1,
                       highlightbackground="#555", font=(FONT, 10), **kw)

    # ---- layout ------------------------------------------------------------

    def _build(self) -> None:
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=10)
        pages = {}
        for name in ("Focus", "Blocklist", "Rules", "Camera", "Settings", "History"):
            pages[name] = ttk.Frame(self.tabs, padding=14)
            self.tabs.add(pages[name], text=name)
        self._build_focus(pages["Focus"])
        self._build_blocklist(pages["Blocklist"])
        self._build_rules(pages["Rules"])
        self._build_camera(pages["Camera"])
        self._build_settings(pages["Settings"])
        self._build_history(pages["History"])
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self._on_tab())

    def _on_tab(self) -> None:
        name = self.tabs.tab(self.tabs.select(), "text")
        if name == "History":
            self._fill_history()
        elif name == "Blocklist":
            self._fill_blocklist()

    # Focus tab ...............................................................

    def _build_focus(self, page) -> None:
        top = ttk.Frame(page)
        top.pack(fill="x")
        self.bank_var, self.lock_var = tk.StringVar(), tk.StringVar()
        ttk.Label(top, textvariable=self.bank_var, style="Big.TLabel").pack(anchor="w")
        ttk.Label(top, text="of game time in the bank", style="Sub.TLabel").pack(anchor="w")
        self.lock_label = ttk.Label(top, textvariable=self.lock_var, style="State.TLabel")
        self.lock_label.pack(anchor="w", pady=(6, 0))

        # -- focus session
        box = ttk.LabelFrame(page, text="Focus session", padding=12)
        box.pack(fill="x", pady=(14, 0))
        last = self.cfg.get("last_task", {})
        self.task_var = tk.StringVar(value=last.get("task", ""))
        self.minutes_var = tk.IntVar(value=int(last.get("minutes", 60)))
        self.preset_var = tk.StringVar(value=last.get("preset", ""))
        self.mode_var = tk.StringVar(value=last.get("mode", "live"))

        self.idle_frame = ttk.Frame(box)
        self.idle_frame.columnconfigure(1, weight=1)
        ttk.Label(self.idle_frame, text="What will you work on?").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.idle_frame, textvariable=self.task_var).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=3)
        ttk.Label(self.idle_frame, text="For how long?").grid(row=1, column=0, sticky="w")
        row1 = ttk.Frame(self.idle_frame)
        row1.grid(row=1, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=3)
        ttk.Spinbox(row1, from_=5, to=600, increment=5, width=6,
                    textvariable=self.minutes_var).pack(side="left")
        ttk.Label(row1, text="minutes").pack(side="left", padx=(6, 18))
        ttk.Label(row1, text="Apps that count:").pack(side="left")
        self.preset_box = ttk.Combobox(row1, textvariable=self.preset_var, state="readonly", width=22)
        self.preset_box.pack(side="left", padx=(6, 0))
        ttk.Label(self.idle_frame, text="Proof").grid(row=2, column=0, sticky="nw", pady=(6, 0))
        row2 = ttk.Frame(self.idle_frame)
        row2.grid(row=2, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=3)
        for i, (value, text) in enumerate((("live", "Watch my screen"),
                                           ("camera", "Watch me on camera"),
                                           ("both", "Screen and camera together"),
                                           ("timelapse", "I will upload a timelapse video"))):
            ttk.Radiobutton(row2, text=text, value=value, variable=self.mode_var).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 18))
        ttk.Button(self.idle_frame, text="Start session", style="Accent.TButton",
                   command=self.start_session).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        self.active_frame = ttk.Frame(box)
        self.session_title = ttk.Label(self.active_frame, style="Head.TLabel", wraplength=680)
        self.session_title.pack(anchor="w")
        self.progress = ttk.Progressbar(self.active_frame, maximum=100)
        self.progress.pack(fill="x", pady=(8, 4))
        self.progress_var, self.state_var, self.detail_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(self.active_frame, textvariable=self.progress_var).pack(anchor="w")
        self.state_label = ttk.Label(self.active_frame, textvariable=self.state_var, style="State.TLabel")
        self.state_label.pack(anchor="w", pady=(6, 0))
        ttk.Label(self.active_frame, textvariable=self.detail_var, wraplength=680).pack(anchor="w")
        buttons = ttk.Frame(self.active_frame)
        buttons.pack(anchor="w", pady=(10, 0))
        self.finish_btn = ttk.Button(buttons, text="Finish and bank time", style="Accent.TButton",
                                     command=self.finish_session)
        self.upload_btn = ttk.Button(buttons, text="Upload timelapse", style="Accent.TButton",
                                     command=self.upload_timelapse)
        self.cancel_btn = ttk.Button(buttons, text="Cancel session", command=self.cancel_session)

        # -- game time
        game = ttk.LabelFrame(page, text="Game time", padding=12)
        game.pack(fill="x", pady=(14, 0))
        self.game_idle = ttk.Frame(game)
        self.spend_var = tk.IntVar(value=30)
        ttk.Label(self.game_idle, text="Unlock games for").pack(side="left")
        self.spend_box = ttk.Spinbox(self.game_idle, from_=1, to=600, width=6, textvariable=self.spend_var)
        self.spend_box.pack(side="left", padx=6)
        ttk.Label(self.game_idle, text="minutes").pack(side="left")
        self.spend_btn = ttk.Button(self.game_idle, text="Start game time", command=self.start_game_time)
        self.spend_btn.pack(side="left", padx=(14, 0))
        self.game_active = ttk.Frame(game)
        self.countdown_var = tk.StringVar()
        ttk.Label(self.game_active, textvariable=self.countdown_var, style="Head.TLabel").pack(side="left")
        ttk.Button(self.game_active, text="End early and refund the rest",
                   command=self.end_game_time).pack(side="left", padx=(14, 0))

        # -- activity
        act = ttk.LabelFrame(page, text="Activity", padding=8)
        act.pack(fill="both", expand=True, pady=(14, 0))
        self.log_box = self._text(act, 6, state="disabled")
        self.log_box.pack(fill="both", expand=True)
        self._load_presets()

    def _load_presets(self) -> None:
        names = list(self.cfg.get("presets", {}).keys())
        self.preset_box["values"] = names
        if self.preset_var.get() not in names and names:
            self.preset_var.set(names[0])
        if hasattr(self, "rule_preset_box"):
            self.rule_preset_box["values"] = names

    def _swap(self, slot: str, frame, **pack) -> None:
        """Show one of several frames sharing a slot, only when it changes."""
        if self._shown.get(slot) is frame:
            return
        if self._shown.get(slot) is not None:
            self._shown[slot].pack_forget()
        frame.pack(**(pack or {"fill": "x"}))
        self._shown[slot] = frame

    # Blocklist tab ...........................................................

    def _build_blocklist(self, page) -> None:
        ttk.Label(page, text="Programs on this list are closed the moment they open, unless "
                             "game time is running.", wraplength=700).pack(anchor="w")
        self.block_list = tk.Listbox(page, height=14, bg=self.bg, fg=self.fg, relief="flat",
                                     highlightthickness=1, highlightbackground="#555",
                                     font=(FONT, 10), activestyle="none")
        self.block_list.pack(fill="both", expand=True, pady=10)
        row = ttk.Frame(page)
        row.pack(fill="x")
        ttk.Button(row, text="Add a game (.exe)", command=self.add_exe).pack(side="left")
        ttk.Button(row, text="Add a folder", command=self.add_folder).pack(side="left", padx=6)
        ttk.Button(row, text="Add a running program", command=self.add_running).pack(side="left")
        ttk.Button(row, text="Remove selected", command=self.remove_block).pack(side="right")
        self.pending_var = tk.StringVar()
        ttk.Label(page, textvariable=self.pending_var, wraplength=700).pack(anchor="w", pady=(10, 0))
        self._block_items: list[tuple[str, str]] = []
        self._fill_blocklist()

    def _fill_blocklist(self) -> None:
        blocklist = self.cfg.get("blocklist", {})
        self._block_items = ([("names", n) for n in sorted(blocklist.get("names", []))]
                             + [("folders", f) for f in sorted(blocklist.get("folders", []))])
        self.block_list.delete(0, "end")
        for kind, value in self._block_items:
            self.block_list.insert("end", value if kind == "names" else f"Everything in {value}")
        self._fill_pending()

    def _fill_pending(self) -> None:
        now = time.time()
        parts = []
        for item in self.cfg.get("pending", []):
            wait = fmt_wait(item.get("at", 0) - now)
            if item.get("op") == "unblock":
                parts.append(f"{item.get('value')} leaves the blocklist in {wait}")
            else:
                parts.append(f"{item.get('key')} changes to {item.get('value')} in {wait}")
        self.pending_var.set("Waiting on the cooldown: " + "; ".join(parts) if parts else "")

    def _add_block(self, kind: str, value: str) -> None:
        error = self.cfg.add_block(kind, value)
        if error:
            messagebox.showwarning("FocusGate", error, parent=self.root)
        self._fill_blocklist()

    def add_exe(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="Pick the game's .exe file",
                                          filetypes=[("Programs", "*.exe"), ("All files", "*.*")])
        if path:
            self._add_block("names", os.path.basename(path))

    def add_folder(self) -> None:
        path = filedialog.askdirectory(parent=self.root, title="Block every program in this folder")
        if path:
            self._add_block("folders", path)

    def add_running(self) -> None:
        import psutil
        names = set()
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name and name not in PROTECTED_NAMES:
                names.add(name)
        win = tk.Toplevel(self.root)
        win.title("Add a running program")
        win.geometry("360x460")
        win.configure(bg=self.bg)
        ttk.Label(win, text="Open the game first, then pick it here.", padding=10).pack(anchor="w")
        box = tk.Listbox(win, bg=self.bg, fg=self.fg, relief="flat", font=(FONT, 10), activestyle="none")
        box.pack(fill="both", expand=True, padx=10)
        ordered = sorted(names)
        for name in ordered:
            box.insert("end", name)

        def choose():
            picked = box.curselection()
            if picked:
                self._add_block("names", ordered[picked[0]])
            win.destroy()

        ttk.Button(win, text="Add to blocklist", command=choose).pack(pady=10)

    def remove_block(self) -> None:
        picked = self.block_list.curselection()
        if not picked:
            return
        kind, value = self._block_items[picked[0]]
        at = self.cfg.remove_block(kind, value)
        if at:
            messagebox.showinfo("FocusGate", f"Strict mode is on, so {value} stays blocked for "
                                             f"another {fmt_wait(at - time.time())}.", parent=self.root)
        self._fill_blocklist()

    # Rules tab ...............................................................

    def _build_rules(self, page) -> None:
        ttk.Label(page, text="Rules settle most windows instantly and for free. Anything they "
                             "cannot place goes to the AI, or stays unverified if no AI is set up.",
                  wraplength=700).pack(anchor="w")
        ttk.Label(page, text="Never on-task (one word or phrase per line, matched against the "
                             "window title)", style="Head.TLabel").pack(anchor="w", pady=(12, 4))
        self.distract_box = self._text(page, 6)
        self.distract_box.pack(fill="x")
        self.distract_box.insert("1.0", "\n".join(self.cfg.get("distractions", [])))

        head = ttk.Frame(page)
        head.pack(fill="x", pady=(14, 4))
        ttk.Label(head, text="On-task list for", style="Head.TLabel").pack(side="left")
        self.rule_preset_var = tk.StringVar(value=self.preset_var.get())
        self.rule_preset_box = ttk.Combobox(head, textvariable=self.rule_preset_var, width=26,
                                            values=list(self.cfg.get("presets", {}).keys()))
        self.rule_preset_box.pack(side="left", padx=8)
        self.rule_preset_box.bind("<<ComboboxSelected>>", lambda e: self._show_preset())
        ttk.Label(head, text="(type a new name to create a list)").pack(side="left")
        cols = ttk.Frame(page)
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=1)
        cols.columnconfigure(1, weight=1)
        ttk.Label(cols, text="Programs (e.g. code.exe)").grid(row=0, column=0, sticky="w")
        ttk.Label(cols, text="Window title words (e.g. lecture)").grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.apps_box = self._text(cols, 9)
        self.apps_box.grid(row=1, column=0, sticky="nsew")
        self.words_box = self._text(cols, 9)
        self.words_box.grid(row=1, column=1, sticky="nsew", padx=(10, 0))
        cols.rowconfigure(1, weight=1)
        ttk.Button(page, text="Save rules", style="Accent.TButton",
                   command=self.save_rules).pack(anchor="w", pady=(10, 0))
        self._show_preset()

    def _show_preset(self) -> None:
        preset = self.cfg.get("presets", {}).get(self.rule_preset_var.get(), {})
        for box, key in ((self.apps_box, "apps"), (self.words_box, "keywords")):
            box.delete("1.0", "end")
            box.insert("1.0", "\n".join(preset.get(key, [])))

    def save_rules(self) -> None:
        self.cfg.set("distractions", [w.lower() for w in lines_of(self.distract_box)])
        name = self.rule_preset_var.get().strip()
        if name:
            presets = self.cfg.get("presets", {})
            presets[name] = {"apps": [a.lower() for a in lines_of(self.apps_box)],
                             "keywords": [w.lower() for w in lines_of(self.words_box)]}
            self.cfg.set("presets", presets)
        self._load_presets()
        self.log("Rules saved. They apply from the next session.")

    # Camera tab .............................................................

    def _build_camera(self, page) -> None:
        cam = self.cfg.get("camera", {})
        ttk.Label(page, wraplength=700, text=(
            "Camera proof checks that you are at your desk. An empty desk pauses the clock. "
            "The check runs on this laptop and pictures are never saved.")).pack(anchor="w")
        grid = ttk.Frame(page)
        grid.pack(anchor="w", pady=(12, 0))
        self.cam_index_var = tk.IntVar(value=int(cam.get("index", 0)))
        self.cam_grace_var = tk.IntVar(value=int(cam.get("face_grace_minutes", 10)))
        self.cam_ai_var = tk.BooleanVar(value=bool(cam.get("ai_checks", False)))
        self.cam_ai_min_var = tk.IntVar(value=int(cam.get("ai_minutes", 5)))
        ttk.Label(grid, text="Which camera").grid(row=0, column=0, sticky="w", pady=3)
        cell = ttk.Frame(grid)
        cell.grid(row=0, column=1, sticky="w", padx=8)
        ttk.Spinbox(cell, from_=0, to=5, width=6, textvariable=self.cam_index_var).pack(side="left")
        ttk.Label(cell, text="0 is the built-in one").pack(side="left", padx=6)
        ttk.Label(grid, text="Head-down allowance").grid(row=1, column=0, sticky="w", pady=3)
        cell = ttk.Frame(grid)
        cell.grid(row=1, column=1, sticky="w", padx=8)
        ttk.Spinbox(cell, from_=1, to=60, width=6, textvariable=self.cam_grace_var).pack(side="left")
        ttk.Label(cell, text="minutes").pack(side="left", padx=6)
        ttk.Label(page, wraplength=700, text=(
            "While you read or write with your head down, the camera may lose your face. "
            "Movement keeps counting for this long after your face was last seen. After that, "
            "time stays unverified until you glance up.")).pack(anchor="w", pady=(2, 10))
        row = ttk.Frame(page)
        row.pack(anchor="w")
        ttk.Checkbutton(row, text="Also send a frame to the AI about every",
                        variable=self.cam_ai_var).pack(side="left")
        ttk.Spinbox(row, from_=2, to=30, width=5, textvariable=self.cam_ai_min_var).pack(side="left", padx=6)
        ttk.Label(row, text="minutes").pack(side="left")
        ttk.Label(page, wraplength=700, text=(
            "The AI checks that you look like you are working, for example not on your phone. "
            "Off means nothing from the camera leaves this laptop. On means pictures of you and "
            "your room go to the AI backend chosen in Settings.")).pack(anchor="w", pady=(2, 10))
        row = ttk.Frame(page)
        row.pack(anchor="w")
        ttk.Button(row, text="Save camera settings", style="Accent.TButton",
                   command=self.save_camera).pack(side="left")
        ttk.Button(row, text="Test camera", command=self.test_camera).pack(side="left", padx=8)
        self.cam_state = tk.StringVar()
        ttk.Label(page, textvariable=self.cam_state, wraplength=700).pack(anchor="w", pady=(10, 6))
        self.cam_preview = ttk.Label(page)
        self.cam_preview.pack(anchor="w")
        self._cam_image = None

    def save_camera(self) -> bool:
        try:
            settings = {"index": max(0, int(self.cam_index_var.get())),
                        "face_grace_minutes": max(1, int(self.cam_grace_var.get())),
                        "ai_checks": bool(self.cam_ai_var.get()),
                        "ai_minutes": max(2, int(self.cam_ai_min_var.get()))}
        except (tk.TclError, ValueError):
            messagebox.showwarning("FocusGate", "Use whole numbers in the camera settings.", parent=self.root)
            return False
        self.cfg.set("camera", settings)
        self.log("Camera settings saved. They apply from the next session.")
        return True

    def test_camera(self) -> None:
        if self.tracker is not None and self.tracker.camera is not None:
            self.cam_state.set("The camera is busy with the current session.")
            return
        if not self.save_camera():
            return
        index = int(self.cfg.get("camera", {}).get("index", 0))
        self.cam_state.set("Opening the camera...")

        def work():
            frame, message = None, ""
            camera = Camera(index)
            try:
                camera.open()
                time.sleep(0.4)
                frame = camera.read()
                if FaceFinder().has_face(frame):
                    message = "Your face was found. You are in frame."
                else:
                    message = ("The camera works, but no face was found. Tilt the screen so your "
                               "face is in the picture and check the lighting.")
            except CameraError as err:
                message = str(err)
            except Exception as err:
                message = f"Camera test failed: {err}"
            finally:
                camera.close()
            self.call_ui(lambda: self._show_camera_test(frame, message))

        threading.Thread(target=work, daemon=True).start()

    def _show_camera_test(self, frame, message: str) -> None:
        self.cam_state.set(message)
        if frame is None:
            self.cam_preview.configure(image="")
            self._cam_image = None
            return
        try:
            from PIL import Image, ImageTk
            picture = Image.fromarray(frame[:, :, ::-1].copy())   # BGR to RGB
            picture.thumbnail((360, 270))
            self._cam_image = ImageTk.PhotoImage(picture)
            self.cam_preview.configure(image=self._cam_image)
        except Exception:
            pass  # the text result is enough if the preview cannot be drawn

    # Settings tab ............................................................

    def _build_settings(self, page) -> None:
        ai = self.cfg.get("ai", {})
        self._labels_to_backend = {v: k for k, v in BACKEND_LABELS.items()}
        page.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(page, text="AI checking", style="Head.TLabel").grid(row=r, column=0, sticky="w", columnspan=3)
        r += 1
        ttk.Label(page, text="Backend").grid(row=r, column=0, sticky="w", pady=3)
        self.backend_var = tk.StringVar(value=BACKEND_LABELS.get(ai.get("backend", "none")))
        box = ttk.Combobox(page, textvariable=self.backend_var, state="readonly", width=26,
                           values=list(BACKEND_LABELS.values()))
        box.grid(row=r, column=1, sticky="w", padx=8)
        box.bind("<<ComboboxSelected>>", lambda e: self._backend_changed())
        r += 1
        ttk.Label(page, text="Model name").grid(row=r, column=0, sticky="w", pady=3)
        self.model_var = tk.StringVar()
        self.model_entry = ttk.Entry(page, textvariable=self.model_var, width=28)
        self.model_entry.grid(row=r, column=1, sticky="w", padx=8)
        r += 1
        ttk.Label(page, text="API key").grid(row=r, column=0, sticky="w", pady=3)
        keyrow = ttk.Frame(page)
        keyrow.grid(row=r, column=1, sticky="w", padx=8)
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(keyrow, textvariable=self.key_var, show="\u2022", width=22)
        self.key_entry.pack(side="left")
        ttk.Button(keyrow, text="Save key", command=self.save_key).pack(side="left", padx=6)
        ttk.Button(keyrow, text="Test", command=self.test_key).pack(side="left")
        r += 1
        self.key_state = tk.StringVar()
        ttk.Label(page, textvariable=self.key_state, wraplength=400).grid(row=r, column=1, sticky="w", padx=8)

        r += 1
        ttk.Label(page, text="Earning", style="Head.TLabel").grid(row=r, column=0, sticky="w", columnspan=3, pady=(16, 0))
        self.ratio_var = tk.IntVar(value=round(float(self.cfg.get("earn_ratio", 0.5)) * 60))
        self.idle_var = tk.IntVar(value=int(self.cfg.get("idle_limit_minutes", 10)))
        self.spot_var = tk.IntVar(value=int(self.cfg.get("spot_check_minutes", 10)))
        self.full_var = tk.BooleanVar(value=bool(self.cfg.get("require_full_target", False)))
        for label, var, low, high, unit in (
                ("Game minutes earned per focus hour", self.ratio_var, 5, 120, ""),
                ("Pause the clock after no input for", self.idle_var, 2, 60, "minutes"),
                ("Random screenshot check about every", self.spot_var, 0, 60, "minutes (0 = never)")):
            r += 1
            ttk.Label(page, text=label).grid(row=r, column=0, sticky="w", pady=3)
            cell = ttk.Frame(page)
            cell.grid(row=r, column=1, sticky="w", padx=8)
            ttk.Spinbox(cell, from_=low, to=high, width=6, textvariable=var).pack(side="left")
            ttk.Label(cell, text=unit).pack(side="left", padx=6)
        r += 1
        ttk.Checkbutton(page, text="Earn nothing unless the session target is reached",
                        variable=self.full_var).grid(row=r, column=0, columnspan=2, sticky="w", pady=3)

        r += 1
        ttk.Label(page, text="Staying honest", style="Head.TLabel").grid(row=r, column=0, sticky="w", columnspan=3, pady=(16, 0))
        self.strict_var = tk.BooleanVar(value=bool(self.cfg.get("strict_mode", False)))
        self.cool_var = tk.IntVar(value=int(self.cfg.get("cooldown_hours", 12)))
        self.auto_var = tk.BooleanVar(value=winutil.autostart_enabled())
        r += 1
        ttk.Checkbutton(page, text="Strict mode", variable=self.strict_var).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=3)
        r += 1
        ttk.Label(page, wraplength=700, text="Removing a game, earning faster, or turning strict mode "
                  "off waits for the cooldown. FocusGate also restarts itself if it is closed, and "
                  "Quit disappears from the tray menu.").grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1
        ttk.Label(page, text="Cooldown length").grid(row=r, column=0, sticky="w", pady=3)
        cell = ttk.Frame(page)
        cell.grid(row=r, column=1, sticky="w", padx=8)
        ttk.Spinbox(cell, from_=1, to=168, width=6, textvariable=self.cool_var).pack(side="left")
        ttk.Label(cell, text="hours").pack(side="left", padx=6)
        r += 1
        auto = ttk.Checkbutton(page, text="Start FocusGate when I sign in to Windows", variable=self.auto_var)
        auto.grid(row=r, column=0, columnspan=2, sticky="w", pady=3)
        if not winutil.IS_WINDOWS:
            auto.state(["disabled"])
        r += 1
        ttk.Button(page, text="Save settings", style="Accent.TButton",
                   command=self.save_settings).grid(row=r, column=0, sticky="w", pady=(14, 0))
        self._backend_changed()

    def _backend(self) -> str:
        return self._labels_to_backend.get(self.backend_var.get(), "none")

    def _backend_changed(self) -> None:
        backend = self._backend()
        ai = self.cfg.get("ai", {})
        self.model_var.set(ai.get(f"{backend}_model", "") if backend != "none" else "")
        state = "disabled" if backend == "none" else "normal"
        self.model_entry.configure(state=state)
        self.key_entry.configure(state=state)
        if backend == "none":
            self.key_state.set("No screenshots leave this laptop. Windows the rules cannot place "
                               "stay unverified, and timelapse proof is unavailable.")
        elif get_api_key(backend):
            self.key_state.set("A key is saved for this backend.")
        else:
            where = ("aistudio.google.com" if backend == "gemini" else "console.anthropic.com")
            self.key_state.set(f"No key saved yet. Create one at {where}, paste it here, then Save key.")

    def _store_ai_choice(self) -> None:
        backend = self._backend()
        ai = self.cfg.get("ai", {})
        ai["backend"] = backend
        if backend != "none" and self.model_var.get().strip():
            ai[f"{backend}_model"] = self.model_var.get().strip()
        self.cfg.set("ai", ai)

    def save_key(self) -> None:
        backend = self._backend()
        if backend == "none":
            return
        try:
            set_api_key(backend, self.key_var.get())
        except Exception as err:
            messagebox.showerror("FocusGate", f"Could not save the key to Windows Credential "
                                              f"Manager: {err}", parent=self.root)
            return
        self.key_var.set("")
        self._store_ai_choice()
        self._backend_changed()

    def test_key(self) -> None:
        self._store_ai_choice()
        judge = make_judge(self.cfg)
        if judge is None:
            self.key_state.set("Pick a backend and save a key first.")
            return
        self.key_state.set("Testing...")

        def work():
            try:
                judge.test()
                message = "It works. The AI answered."
            except JudgeError as err:
                message = f"Test failed: {err}"
            except Exception as err:
                message = f"Test failed: {err}"
            self.call_ui(lambda: self.key_state.set(message))

        threading.Thread(target=work, daemon=True).start()

    def save_settings(self) -> None:
        self._store_ai_choice()
        try:
            ratio = max(5, min(120, int(self.ratio_var.get()))) / 60
            idle = max(2, int(self.idle_var.get()))
            spot = max(0, int(self.spot_var.get()))
            cool = max(1, int(self.cool_var.get()))
        except (tk.TclError, ValueError):
            messagebox.showwarning("FocusGate", "Use whole numbers in the settings.", parent=self.root)
            return
        self.cfg.set("idle_limit_minutes", idle)
        self.cfg.set("spot_check_minutes", spot)
        delayed = []
        # Order matters: decide the delay with the current strict/cooldown values.
        for key, value in (("earn_ratio", ratio), ("require_full_target", bool(self.full_var.get())),
                           ("cooldown_hours", cool), ("strict_mode", bool(self.strict_var.get()))):
            at = self.cfg.request(key, value)
            if at:
                delayed.append(f"{key} (in {fmt_wait(at - time.time())})")
        try:
            winutil.set_autostart(bool(self.auto_var.get()))
        except OSError as err:
            messagebox.showwarning("FocusGate", f"Could not change the start-up setting: {err}",
                                   parent=self.root)
        self._sync_settings()
        self._fill_pending()
        if delayed:
            messagebox.showinfo("FocusGate", "Saved. Strict mode is on, so these wait for the "
                                             "cooldown: " + ", ".join(delayed), parent=self.root)
        else:
            self.log("Settings saved.")

    def _sync_settings(self) -> None:
        self.ratio_var.set(round(float(self.cfg.get("earn_ratio", 0.5)) * 60))
        self.full_var.set(bool(self.cfg.get("require_full_target", False)))
        self.cool_var.set(int(self.cfg.get("cooldown_hours", 12)))
        self.strict_var.set(bool(self.cfg.get("strict_mode", False)))

    # History tab .............................................................

    def _build_history(self, page) -> None:
        cols = ("when", "task", "proof", "verified", "earned", "result")
        self.history = ttk.Treeview(page, columns=cols, show="headings", height=12)
        for col, text, width in (("when", "When", 110), ("task", "Task", 220), ("proof", "Proof", 110),
                                 ("verified", "Verified", 80), ("earned", "Earned", 80),
                                 ("result", "Result", 110)):
            self.history.heading(col, text=text)
            self.history.column(col, width=width, anchor="w")
        self.history.pack(fill="both", expand=True)
        ttk.Label(page, text="Events", style="Head.TLabel").pack(anchor="w", pady=(12, 4))
        self.events_box = self._text(page, 8, state="disabled")
        self.events_box.pack(fill="x")

    def _fill_history(self) -> None:
        self.history.delete(*self.history.get_children())
        for row in self.store.recent_sessions(60):
            when = time.strftime("%d %b %H:%M", time.localtime(row["started_at"]))
            proof = PROOF_LABELS.get(row["mode"], row["mode"])
            result = {"done": "Banked", "active": "Running", "cancelled": "Cancelled",
                      "interrupted": "Interrupted"}.get(row["status"], row["status"])
            self.history.insert("", "end", values=(when, row["task"], proof, fmt_min(row["verified_sec"]),
                                                   fmt_min(row["earned_sec"]), result))
        self.events_box.configure(state="normal")
        self.events_box.delete("1.0", "end")
        for event in self.store.recent_events(40):
            when = time.strftime("%d %b %H:%M", time.localtime(event["ts"]))
            self.events_box.insert("end", f"{when}  {event['kind']}: {event['detail']}\n")
        self.events_box.configure(state="disabled")

    # ---- actions -----------------------------------------------------------

    def start_session(self) -> None:
        if self.store.active_session():
            return
        task = self.task_var.get().strip()
        if len(task) < 3:
            messagebox.showwarning("FocusGate", "Say what you are going to work on first.", parent=self.root)
            return
        try:
            minutes = max(5, int(self.minutes_var.get()))
        except (tk.TclError, ValueError):
            minutes = 60
        if self.store.unlock_remaining() > 0:
            messagebox.showwarning("FocusGate", "Game time is running. End it before starting a "
                                                "focus session.", parent=self.root)
            return
        mode, preset = self.mode_var.get(), self.preset_var.get()
        judge = make_judge(self.cfg)
        if mode == "timelapse" and judge is None:
            messagebox.showwarning("FocusGate", "Timelapse proof needs an AI backend. Add a key "
                                                "in Settings, or choose Watch my screen.", parent=self.root)
            return
        camera = None
        if mode in ("camera", "both"):
            camera = CameraWatcher(self.cfg)
            try:
                camera.start()
            except CameraError as err:
                messagebox.showwarning("FocusGate", f"Could not start the camera. {err}", parent=self.root)
                return
            except Exception as err:
                messagebox.showwarning("FocusGate", f"Could not start the camera: {err}", parent=self.root)
                return
        session_id = self.store.start_session(task, preset, mode, minutes)
        self.cfg.set("last_task", {"task": task, "minutes": minutes, "preset": preset, "mode": mode})
        if mode in LIVE_MODES:
            session = self.store.get_session(session_id)
            self.tracker = SessionTracker(
                self.cfg, self.store, session, judge, winutil.foreground_window,
                winutil.screenshot_jpeg, winutil.idle_seconds, camera=camera,
                on_event=lambda message: self.call_ui(lambda: self.log(message)))
            self.monitor = MonitorThread(self.tracker)
            self.monitor.start()
            note = ""
            if mode != "camera" and not judge:
                note = " No AI is set up, so only the rules can verify screen time."
            if camera is not None:
                note += " The camera is on."
            self.log(f"Session started: {task} ({minutes} min).{note}")
        else:
            self.log(f"Session started: {task}. Record your timelapse, then upload it here.")

    def _stop_monitor(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor.join(timeout=5)
        self.monitor = None

    def finish_session(self) -> None:
        if self.tracker is None:
            return
        self._stop_monitor()
        summary = self.tracker.finish()
        self.tracker = None
        text = f"Verified {fmt_min(summary['verified'])}, banked {fmt_min(summary['earned'])} of game time."
        if summary["note"]:
            text += " The target was not reached, so nothing was earned."
        if summary["unverified"] >= 60:
            text += (f" {fmt_min(summary['unverified'])} could not be verified. Add those apps "
                     "in Rules or set up an AI backend.")
        self.log(text)
        messagebox.showinfo("Session finished", text, parent=self.root)

    def cancel_session(self) -> None:
        row = self.store.active_session()
        if not row or self.busy:
            return
        if not messagebox.askyesno("Cancel session", "Cancel this session? Nothing will be banked.",
                                   parent=self.root):
            return
        self._stop_monitor()
        if self.tracker is not None:
            self.tracker.close()
        self.tracker = None
        self.store.finish_session(row["id"], status="cancelled")
        self.log("Session cancelled. Nothing banked.")

    def upload_timelapse(self) -> None:
        row = self.store.active_session()
        if not row or self.busy:
            return
        pattern = " ".join("*" + ext for ext in VIDEO_TYPES)
        path = filedialog.askopenfilename(parent=self.root, title="Pick your timelapse video",
                                          filetypes=[("Videos", pattern), ("All files", "*.*")])
        if not path:
            return
        judge = make_judge(self.cfg)
        self.busy = True
        self.log("Checking the video. This can take up to a minute.")

        def work():
            try:
                result = verify_timelapse(self.cfg, self.store, row, path, judge)
                message = result.message
                if result.ok:
                    message = (f"Timelapse accepted: {result.fraction:.0%} of frames on-task. Verified "
                               f"{fmt_min(result.verified)}, banked {fmt_min(result.earned)} of game time.")
            except Exception as err:
                message = f"Could not check the video: {err}"

            def done():
                self.busy = False
                self.log(message)
                messagebox.showinfo("Timelapse", message, parent=self.root)

            self.call_ui(done)

        threading.Thread(target=work, daemon=True).start()

    def start_game_time(self) -> None:
        if self.store.active_session():
            messagebox.showwarning("FocusGate", "Finish or cancel the focus session first.", parent=self.root)
            return
        try:
            minutes = int(self.spend_var.get())
        except (tk.TclError, ValueError):
            return
        if self.store.start_unlock(minutes):
            self.log(f"Game time started: {minutes} min.")
        else:
            messagebox.showwarning("FocusGate", f"You only have {fmt_min(self.store.bank())} banked.",
                                   parent=self.root)

    def end_game_time(self) -> None:
        left = self.store.end_unlock()
        self.log(f"Game time ended. {fmt_min(left)} went back in the bank.")

    # ---- periodic ----------------------------------------------------------

    def _refresh(self) -> None:
        if self._quitting:
            return
        now = time.time()
        bank, remaining = self.store.bank(), self.store.unlock_remaining(now)
        self.bank_var.set(fmt_min(bank))
        if remaining > 0:
            self.lock_var.set(f"Games unlocked for {fmt_clock(remaining)}")
            self.lock_label.configure(foreground=GOOD)
            self.countdown_var.set(f"{fmt_clock(remaining)} left")
            self._swap("game", self.game_active)
        else:
            self.lock_var.set("Games are locked")
            self.lock_label.configure(foreground=WARN)
            self._swap("game", self.game_idle)
            can_spend = bank >= 60
            self.spend_btn.state(["!disabled"] if can_spend else ["disabled"])
            self.spend_box.configure(to=max(1, int(bank // 60)))

        row = self.store.active_session()
        if row is None:
            self._swap("session", self.idle_frame)
        else:
            self._swap("session", self.active_frame)
            self._show_session(row, now)
        self.root.after(1000, self._refresh)

    def _show_session(self, row: dict, now: float) -> None:
        target = max(1, (row["target_min"] or 60) * 60)
        self.session_title.configure(text=row["task"])
        for button in (self.finish_btn, self.upload_btn, self.cancel_btn):
            button.pack_forget()
        if self.tracker is not None:
            totals = self.tracker.totals
            self.progress.configure(value=min(100, 100 * totals.verified / target))
            self.progress_var.set(f"{fmt_min(totals.verified)} verified of {row['target_min']} min"
                                  f"   |   off task {fmt_min(totals.off)}"
                                  f"   |   not verified {fmt_min(totals.unverified)}"
                                  f"   |   away {fmt_min(totals.idle)}")
            kind, detail = self.tracker.status
            self.state_var.set(STATUS_TEXT.get(kind, kind))
            self.state_label.configure(foreground=STATUS_COLOUR.get(kind, WARN))
            self.detail_var.set(detail[:110])
            self.finish_btn.pack(side="left")
        else:
            elapsed = now - row["started_at"]
            self.progress.configure(value=min(100, 100 * elapsed / target))
            self.progress_var.set(f"{fmt_clock(elapsed)} since you started (target {row['target_min']} min)")
            self.state_var.set("Checking your video..." if self.busy else "Waiting for your timelapse")
            self.state_label.configure(foreground=WARN)
            self.detail_var.set("Film your desk or screen as a timelapse while you work. When you are "
                                "done, move the video to this laptop and upload it.")
            self.upload_btn.pack(side="left")
            self.upload_btn.state(["disabled"] if self.busy else ["!disabled"])
        self.cancel_btn.pack(side="left", padx=(8, 0))

    def _heartbeat(self) -> None:
        if self._quitting:
            return
        self.store.kv_set("heartbeat", time.time())
        if stop_flag().exists():
            self.store.add_event("stopped", "stopped with the stop script")
            self.quit()
            return
        if self.cfg.get("strict_mode", False) and not is_alive(WATCHDOG_PORT):
            try:
                winutil.spawn_detached(winutil.launcher_command("--watchdog"))
            except OSError:
                pass
        if self.tabs.tab(self.tabs.select(), "text") == "Blocklist":
            self._fill_pending()
        self.root.after(5000, self._heartbeat)

    # ---- tray and window ---------------------------------------------------

    def _start_tray(self) -> None:
        if os.environ.get("FOCUSGATE_NO_TRAY"):
            return
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((6, 26, 58, 60), radius=8, fill=(63, 178, 127, 255))
        draw.arc((18, 6, 46, 44), 180, 360, fill=(63, 178, 127, 255), width=7)
        draw.ellipse((27, 37, 37, 47), fill=(20, 30, 26, 255))

        def status(_item):
            left = self.store.unlock_remaining()
            return (f"Unlocked: {fmt_clock(left)} left" if left > 0
                    else f"Locked. Bank: {fmt_min(self.store.bank())}")

        menu = pystray.Menu(
            pystray.MenuItem("Open FocusGate", lambda: self.call_ui(self.show_window), default=True),
            pystray.MenuItem(status, None, enabled=False),
            pystray.MenuItem("Quit", lambda: self.call_ui(self.quit),
                             visible=lambda _item: not self.cfg.get("strict_mode", False)))
        try:
            self.tray = pystray.Icon("FocusGate", image, "FocusGate", menu)
            self.tray.run_detached()
        except Exception:
            self.tray = None

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
        if self.tray is not None:
            self.root.withdraw()
        else:
            self.root.iconify()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        try:
            self._stop_monitor()
            if self.tracker is not None:
                self.tracker.close()
            self.blocker.stop()
            self.store.kv_set("clean_exit", "1")
            if self.tray is not None:
                self.tray.stop()
        finally:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if "--stop" in argv:
        stop_flag().write_text("stop", encoding="utf-8")
        print("FocusGate will close within a few seconds.")
        return 0
    if "--watchdog" in argv:
        from . import watchdog
        watchdog.run()
        return 0
    lock = InstanceLock(MAIN_PORT)
    if not lock.acquire():
        return 0  # already running
    try:
        stop_flag().unlink(missing_ok=True)
        App(minimized="--minimized" in argv).run()
    finally:
        lock.release()
    return 0
