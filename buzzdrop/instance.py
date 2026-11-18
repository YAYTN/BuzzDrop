"""Single-instance handling.

The first GUI process listens on ``127.0.0.1:<random port>``; later launches
(a clicked BZ:// link, "Send with BuzzDrop" on 20 selected files, …) hand
their request to it and exit immediately, so 20 selected files make one window.

A startup file lock makes this race-free when Explorer launches many processes
at once, and every message must carry a random per-session token.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import secrets
import socket
import sys
import threading
from typing import Any, Dict, Optional

from .config import data_dir, read_json, write_json
from .util import FileLock

log = logging.getLogger("buzz")


def _info_path():
    return data_dir() / "instance.json"


def _allow_foreground(pid: int) -> None:
    """Let the running instance bring its window to the front (Windows focus-stealing rules)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.WinDLL("user32").AllowSetForegroundWindow(int(pid))
        except Exception:
            pass


def forward(payload: Dict[str, Any], timeout: float = 3.0) -> bool:
    """Send *payload* to a running instance. True if it accepted it."""
    info = read_json(_info_path(), None)
    if not isinstance(info, dict) or not info.get("port"):
        return False
    try:
        _allow_foreground(info.get("pid", 0))
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=timeout) as sock:
            msg = dict(payload, token=info.get("token", ""))
            sock.sendall(json.dumps(msg).encode("utf-8") + b"\n")
            reply = sock.makefile("rb").readline(64)
            return reply.strip() == b"ok"
    except (OSError, ValueError):
        return False


class InstanceServer:
    """Receives payloads from later launches; they appear on :attr:`messages`."""

    def __init__(self) -> None:
        self.messages: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.token = secrets.token_hex(16)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        path = _info_path()
        write_json(path, {"port": self.port, "token": self.token, "pid": os.getpid()})
        if sys.platform != "win32":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        self._closed = False
        threading.Thread(target=self._serve, daemon=True, name="instance-server").start()

    def _serve(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(3)
                line = conn.makefile("rb").readline(1_000_000)
                msg = json.loads(line.decode("utf-8"))
                if not isinstance(msg, dict) or not hmac.compare_digest(str(msg.pop("token", "")), self.token):
                    conn.sendall(b"denied\n")
                    return
                if self._closed:  # shutting down → the sender starts its own window instead
                    conn.sendall(b"closing\n")
                    return
                self.messages.put(msg)
                conn.sendall(b"ok\n")
            except (OSError, ValueError) as exc:
                log.debug("instance message rejected: %s", exc)

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass
        info = read_json(_info_path(), None)
        if isinstance(info, dict) and info.get("pid") == os.getpid():
            try:
                _info_path().unlink()
            except OSError:
                pass


def acquire_or_forward(payload: Dict[str, Any]) -> Optional[InstanceServer]:
    """Become the primary instance (returns a server) or forward *payload* (returns None)."""
    with FileLock(data_dir() / "instance.lock", timeout=15):
        if forward(payload):
            return None
        return InstanceServer()
