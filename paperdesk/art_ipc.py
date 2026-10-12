# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded, cancellable Krita IPC over Qt's native local sockets/named pipes.

The QLocalSocket is created, used and destroyed in its owning worker thread.
No application event loop is required by the synchronous Qt Network calls.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

PROTOCOL = 1
MAX_HEADER = 65536
MAX_FRAME = 40_000_000


def windows_server_name(home: Path | str | None = None) -> str:
    identity = str(home if home is not None else Path.home()).replace("\\", "/").casefold()
    return "PaperDeskViewport-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


class KritaIPC:
    """One request at a time; no reconnect or mutation replay within a request."""
    def __init__(self, address: str | Path, stop_event=None, timeout=10.0):
        self.address, self.timeout = str(address), float(timeout)
        self.stop_event = stop_event or threading.Event()
        self.owner = threading.get_ident()
        self.socket = None
        self.buffer = bytearray()

    def _owner(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError("Krita IPC must be used in its owning worker thread.")

    def _check(self, deadline):
        if self.stop_event.is_set():
            raise OSError("Art mode was stopped.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Krita did not answer before the request timeout.")
        return min(100, max(1, round(remaining * 1000)))

    def _connect(self, deadline):
        from PySide6.QtNetwork import QLocalSocket
        self.socket = QLocalSocket()
        self.socket.setReadBufferSize(262144)
        self.socket.connectToServer(self.address)
        while self.socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
            self.socket.waitForConnected(self._check(deadline))
            if self.socket.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                raise ConnectionError("Krita's reMarkable Viewport plugin is not listening.")

    def _available(self, deadline):
        from PySide6.QtNetwork import QLocalSocket
        while self.socket.bytesAvailable() == 0:
            self._check(deadline)
            if self.socket.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                raise ConnectionError("Krita closed the viewport connection.")
            self.socket.waitForReadyRead(self._check(deadline))

    def _line(self, deadline):
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_HEADER:
                    raise ValueError("Krita reply header exceeded its limit.")
                line = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                return line
            if len(self.buffer) > MAX_HEADER:
                raise ValueError("Krita reply header exceeded its limit.")
            self._available(deadline)
            self.buffer.extend(bytes(self.socket.read(min(65537 - len(self.buffer), self.socket.bytesAvailable()))))

    def _body(self, size, deadline):
        body = bytearray()
        if self.buffer:
            count = min(size, len(self.buffer))
            body.extend(self.buffer[:count])
            del self.buffer[:count]
        while len(body) < size:
            self._available(deadline)
            data = bytes(self.socket.read(min(size - len(body), self.socket.bytesAvailable(), 262144)))
            if not data:
                raise ConnectionError("Krita returned an incomplete viewport frame.")
            body.extend(data)
        return bytes(body)

    def request(self, request: dict):
        self._owner()
        if not isinstance(request, dict):
            raise ValueError("Krita requests must be objects.")
        packet = json.dumps(request, allow_nan=False).encode("utf-8") + b"\n"
        if len(packet) > MAX_HEADER:
            raise ValueError("Krita request exceeded its limit.")
        deadline = time.monotonic() + self.timeout
        try:
            self._check(deadline)
            if self.socket is None:
                self._connect(deadline)
            if self.socket.write(packet) != len(packet):
                raise ConnectionError("Krita did not accept the complete viewport request.")
            while self.socket.bytesToWrite():
                self.socket.waitForBytesWritten(self._check(deadline))
            head = json.loads(self._line(deadline))
            if not isinstance(head, dict):
                raise ValueError("Invalid Krita viewport reply.")
            size = head.get("size", 0)
            if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_FRAME:
                raise ValueError("Invalid Krita viewport frame size.")
            return head, self._body(size, deadline)
        except BaseException:
            # A request may have changed Krita state. Close and report failure;
            # caller must not automatically replay that request.
            self.close()
            raise

    def close(self):
        self._owner()
        if self.socket is not None:
            self.socket.abort()
            self.socket = None
        self.buffer.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def probe_krita(address, timeout=2.0):
    """A fixed read-only protocol check, including on Windows named pipes."""
    with KritaIPC(address, timeout=timeout) as client:
        head, body = client.request({"op": "ping"})
    if head.get("protocol") != PROTOCOL or head.get("app") != "paperdesk-viewport" or body:
        raise ValueError("The installed Krita viewport plugin is incompatible. Copy the current PaperDesk plugin and restart Krita after saving your work.")
    return {"ok": True, "protocol": PROTOCOL, "transport": "Qt local IPC", "message": "Krita viewport plugin answered."}
