"""Windows-specific helpers: active window, idle time, screenshots, autostart.

Every function degrades to a harmless value on other systems so the rest of
the app (and the tests) can run anywhere.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "run_focusgate.pyw"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "FocusGate"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetTickCount.restype = wintypes.DWORD

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def enable_dpi_awareness() -> None:
    """Crisp text and full-resolution screenshots on scaled displays."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            _user32.SetProcessDPIAware()
        except Exception:
            pass


def foreground_window() -> tuple[str, str]:
    """(process name in lower case, window title) of the window in front."""
    if not IS_WINDOWS:
        return ("", "")
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ("", "")
        length = _user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ""
        if pid.value:
            import psutil
            try:
                name = psutil.Process(pid.value).name().lower()
            except (psutil.Error, OSError):
                name = ""
        return (name, buffer.value)
    except Exception:
        return ("", "")


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input."""
    if not IS_WINDOWS:
        return 0.0
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        elapsed = (_kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return elapsed / 1000.0
    except Exception:
        return 0.0


def screenshot_jpeg(max_edge: int = 1024, quality: int = 60) -> bytes:
    """Grab the main screen, shrink it, and return JPEG bytes (never saved to disk)."""
    from PIL import ImageGrab
    image = ImageGrab.grab()
    image.thumbnail((max_edge, max_edge))
    out = io.BytesIO()
    image.convert("RGB").save(out, format="JPEG", quality=quality)
    return out.getvalue()


def python_gui_exe() -> str:
    """pythonw.exe (no console window) when it sits next to the interpreter."""
    exe = Path(sys.executable)
    if IS_WINDOWS and exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def launcher_command(*extra: str) -> list[str]:
    return [python_gui_exe(), str(LAUNCHER), *extra]


def spawn_detached(args: list[str]) -> None:
    """Start a process that outlives this one."""
    kwargs = {"close_fds": True, "cwd": str(ROOT),
              "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if IS_WINDOWS:
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enable: bool) -> None:
    """Start FocusGate (minimised to the tray) when you sign in to Windows."""
    if not IS_WINDOWS:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            command = subprocess.list2cmdline(launcher_command("--minimized"))
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except OSError:
                pass
