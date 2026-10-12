"""Native local Qt IPC tests; generated fixtures, no Krita or tablet required."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QThread
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from paperdesk.art_ipc import KritaIPC, probe_krita, windows_server_name
from paperdesk.art_runtime import canvas_rectangle, canvas_uv
from paperdesk.native_pointer import ScreenRect
from paperdesk.vendor.art import transforms as t


@pytest.fixture(scope="session")
def ipc_application():
    application = QApplication.instance() or QApplication([])
    yield application


class LoopbackServer(QThread):
    def __init__(self, address, handler, fragment=False, stall=False):
        super().__init__()
        self.address, self.handler = address, handler
        self.fragment, self.stall = fragment, stall
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.requests = []
        self.error = None
        self.disconnected = threading.Event()

    def run(self):
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        try:
            if not server.listen(self.address):
                raise RuntimeError(server.errorString())
            self.ready.set()
            while not self.stopped.is_set():
                server.waitForNewConnection(50)
                if not server.hasPendingConnections():
                    continue
                client = server.nextPendingConnection()
                pending = bytearray()
                try:
                    while not self.stopped.is_set():
                        if client.bytesAvailable() == 0:
                            client.waitForReadyRead(50)
                        if client.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                            self.disconnected.set()
                            break
                        pending.extend(bytes(client.readAll()))
                        if b"\n" not in pending:
                            continue
                        line, tail = pending.split(b"\n", 1)
                        pending = bytearray(tail)
                        request = json.loads(line)
                        self.requests.append(request)
                        if self.stall:
                            continue
                        packet = self.handler(request)
                        chunks = [packet[i:i + 7] for i in range(0, len(packet), 7)] if self.fragment else [packet]
                        for chunk in chunks:
                            client.write(chunk)
                            while client.bytesToWrite():
                                client.waitForBytesWritten(100)
                finally:
                    client.abort()
        except BaseException as error:
            self.error = error
            self.ready.set()
        finally:
            server.close()
            QLocalServer.removeServer(self.address)

    def stop(self):
        self.stopped.set()
        assert self.wait(3000), "loopback server did not stop"
        assert self.error is None, repr(self.error)


@pytest.fixture
def server_factory(ipc_application):
    servers = []
    with tempfile.TemporaryDirectory(prefix="pd-ipc-") as folder:
        def create(handler, **options):
            token = "pd-" + uuid.uuid4().hex
            address = token if sys.platform == "win32" else str(Path(folder) / token)
            server = LoopbackServer(address, handler, **options)
            servers.append(server)
            server.start()
            assert server.ready.wait(3)
            assert server.error is None, repr(server.error)
            return server
        try:
            yield create
        finally:
            for server in servers:
                server.stop()


class ClientWorker(QThread):
    def __init__(self, work):
        super().__init__()
        self.work, self.result, self.error = work, None, None

    def run(self):
        try:
            self.result = self.work()
        except BaseException as error:
            self.error = error


def run_client(work):
    worker = ClientWorker(work)
    worker.start()
    assert worker.wait(12000), "IPC worker failed to stop"
    if worker.error:
        raise worker.error
    return worker.result


def response(head, body=b""):
    return json.dumps({**head, "size": len(body)}).encode() + b"\n" + body


def test_native_qt_local_ipc_ping(server_factory):
    server = server_factory(lambda request: response({"app": "paperdesk-viewport", "protocol": 1}))
    result = run_client(lambda: probe_krita(server.address))
    assert result["ok"] and result["protocol"] == 1
    assert server.requests == [{"op": "ping"}]


def test_native_qt_frame_binary_and_multiple_requests(server_factory):
    pixels = io.BytesIO()
    Image.new("RGB", (2, 2), (14, 82, 191)).save(pixels, format="PNG")
    png = pixels.getvalue()
    def handle(request):
        if request["op"] == "frame":
            return response({"rect": [10, 20, 100, 75], "screen": [1000, 800], "desktop": [-100, 0, 1000, 800], "hash": "fixture"}, png)
        return response({"changed": request["op"]})
    server = server_factory(handle, fragment=True)
    def client():
        with KritaIPC(server.address) as connection:
            head, body = connection.request({"op": "frame", "w": 2160, "h": 1620})
            changed, empty = connection.request({"op": "view", "scale": 1.2, "at": [0.5, 0.5], "move": [0.1, 0]})
            return head, body, changed, empty
    head, body, changed, empty = run_client(client)
    assert head["hash"] == "fixture" and body == png
    assert changed["changed"] == "view" and empty == b""
    assert server.requests[1]["scale"] == 1.2


def test_native_ipc_rejects_unbounded_reply_and_never_replays(server_factory):
    server = server_factory(lambda _: b'{"size":40000001}\n')
    with pytest.raises(ValueError, match="frame size"):
        run_client(lambda: KritaIPC(server.address).request({"op": "undo"}))
    assert server.requests == [{"op": "undo"}]


def test_native_ipc_timeout_closes_without_replaying_mutation(server_factory):
    server = server_factory(lambda _: b"", stall=True)
    with pytest.raises(TimeoutError):
        run_client(lambda: KritaIPC(server.address, timeout=0.2).request({"op": "undo"}))
    assert server.requests == [{"op": "undo"}]


def test_native_ipc_cancellation_stops_owning_worker_quickly(server_factory):
    server = server_factory(lambda _: b"", stall=True)
    cancel = threading.Event()
    worker = ClientWorker(lambda: KritaIPC(server.address, cancel).request({"op": "frame"}))
    worker.start()
    for _ in range(100):
        if server.requests:
            break
        time.sleep(0.005)
    cancel.set()
    assert worker.wait(1000)
    assert isinstance(worker.error, OSError)
    assert len(server.requests) == 1


def test_windows_pipe_identifier_is_short_and_user_specific():
    name = windows_server_name(r"C:\Users\Test User")
    assert name.startswith("PaperDeskViewport-") and len(name) < 64
    assert name == windows_server_name("c:/users/test user")
    assert name != windows_server_name("c:/users/another-user")
    assert "\\" not in name and "/" not in name


def test_canvas_native_mapping_keeps_pressure_coordinates_separate():
    mapper = t.Mapper("left", None)
    mapper.region = (0.2, 0.25, 0.5, 0.5)
    u, v = canvas_uv(mapper, 0.45, 0.5, ScreenRect(100, 200, 1000, 500))
    assert (u, v) == pytest.approx((0.5, 0.5))
    assert canvas_rectangle("darwin", [-500, 100, 400, 300], [-1000, 0, 2000, 1000], {}) == ScreenRect(-500, 100, 400, 300)


def test_windows_canvas_uses_native_client_origin_and_dpi():
    import ctypes
    from paperdesk.windows_ink import POINT
    class Function:
        def __call__(self, window, point):
            assert window == 123
            value = ctypes.cast(point, ctypes.POINTER(POINT)).contents
            value.x, value.y = -1920, 40
            return 1
    class API:
        ClientToScreen = Function()
    rectangle = canvas_rectangle("win32", [-1600, 200, 800, 600], [-1920, 0, 3840, 2160],
                                 {"native_window": 123, "pixel_ratio": 2.0, "canvas_offset": [50, 80]}, API())
    assert rectangle == ScreenRect(-1820, 200, 1600, 1200)


def test_canvas_mirror_relays_native_qt_frame_and_stops_cleanly(server_factory):
    from paperdesk.vendor.art.canvas import CanvasMirror
    pixels = io.BytesIO()
    Image.new("RGB", (2, 2), (90, 90, 90)).save(pixels, format="PNG")
    png = pixels.getvalue()
    def handle(request):
        if request["op"] == "frame":
            return response({"rect": [-500, 100, 400, 300], "screen": [2000, 1000],
                             "desktop": [-1000, 0, 2000, 1000], "hash": "fixture", "name": "Fixture",
                             "zones": [[100, 100, 40]]}, b"" if request.get("have") == "fixture" else png)
        return response({"changed": request["op"]})
    server = server_factory(handle)
    class Feeder:
        def __init__(self):
            self.stdin = self
            self.data = []
            self.closed = False
        def poll(self): return 0 if self.closed else None
        def write(self, data): self.data.append(data)
        def flush(self): pass
        def close(self): self.closed = True
    feeder = Feeder()
    scripts = []
    def stream(script):
        scripts.append(script)
        return feeder
    stopped = threading.Event()
    mapper = t.Mapper("left", None)
    mirror = CanvasMirror(stream, server.address, stopped, "left", [mapper], lambda: False)
    for _ in range(200):
        if mirror.have == "fixture":
            break
        time.sleep(0.005)
    assert mirror.have == "fixture"
    assert mapper.region == pytest.approx((0.25, 0.1, 0.2, 0.3))
    assert mirror.on_palette(100/2160, 100/1620)
    mirror.view({"scale": 1.1, "at": [0.5, 0.5], "move": [0.01, 0]})
    for _ in range(200):
        if any(request["op"] == "view" for request in server.requests):
            break
        time.sleep(0.005)
    mirror.close()
    assert not mirror.thread.is_alive() and mirror.krita is None
    assert feeder.closed and any(data.startswith(b"f") and png in data for data in feeder.data)
    assert any(data == b"bye 0\n" for data in feeder.data)
    assert all("/tmp/paperdesk-viewport" in script and "systemctl" not in script for script in scripts)


def test_ipc_never_closes_qt_object_from_another_thread():
    connection = KritaIPC("unused-fixture")
    worker = ClientWorker(connection.close)
    worker.start()
    assert worker.wait(1000)
    assert isinstance(worker.error, RuntimeError)
    connection.close()


def test_display_options_are_portable_and_only_krita(tmp_path):
    from paperdesk.art import ArtError, ArtManager, validate_options
    from paperdesk.models import DeviceConfig
    for platform in ("linux", "darwin", "win32"):
        assert validate_options({"display": True, "touch": True}, platform)["display"]
        manager = ArtManager(DeviceConfig(state_dir=tmp_path), simulated=True)
        manager.platform = platform
        with pytest.raises(ArtError, match="Krita"):
            manager.start("blender", {"display": True})
        result = manager.start("krita", {"display": True, "touch": True})
        assert result["simulated"] and result["options"]["display"]
        manager.stop()
