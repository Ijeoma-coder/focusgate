"""Single-instance lock built on a loopback-only socket.

Holding the port means "I am running". The watchdog uses is_alive() to see
whether the main app is still up, and a second copy of either process exits
straight away because it cannot take the port.
"""
from __future__ import annotations

import socket
import sys
import threading


class InstanceLock:
    def __init__(self, port: int):
        self.port = port
        self._sock: socket.socket | None = None

    def acquire(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Windows: leave the defaults. A second plain bind to a held port fails,
        # which is the behaviour we want, and SO_REUSEADDR there would let two
        # processes share the port. Elsewhere SO_REUSEADDR only allows a quick
        # restart after the previous holder has gone.
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", self.port))
            sock.listen(4)
        except OSError:
            sock.close()
            return False
        # A short timeout lets the accept loop notice release() promptly.
        sock.settimeout(0.3)
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, args=(sock,), daemon=True)
        self._thread.start()
        return True

    def _serve(self, sock: socket.socket) -> None:
        while self._sock is sock:
            try:
                conn, _ = sock.accept()
                conn.close()
            except socket.timeout:
                continue
            except OSError:
                break
        try:
            sock.close()
        except OSError:
            pass

    def release(self) -> None:
        """Give the port back. Returns once the listener has really closed."""
        thread = getattr(self, "_thread", None)
        self._sock = None
        if thread is not None:
            thread.join(timeout=2)


def is_alive(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False
