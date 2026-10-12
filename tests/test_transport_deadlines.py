"""Real SSH ACK/version stalls must obey deadlines and main-thread stops."""
import threading
import time
from unittest.mock import Mock

import paramiko
import pytest

from paperdesk.models import DeviceConfig
from paperdesk.transport import SSHTransport, DeviceError, UnknownMutation
from test_tailscale_transport import SSHNoneServer, Authentication


@pytest.fixture
def stalled_device(tmp_path,monkeypatch):
    root = tmp_path / "tablet"; root.mkdir()
    server = SSHNoneServer(root)
    entered,release = threading.Event(),threading.Event()
    mode = {"kind":"exec"}
    original_channel = Authentication.check_channel_request
    original_subsystem = Authentication.check_channel_subsystem_request
    def channel_request(self,kind,identifier):
        if mode["kind"] == "open":
            entered.set(); release.wait(10)
        return original_channel(self,kind,identifier)
    def exec_request(self,channel,command):
        self.owner.commands.append(command)
        entered.set(); release.wait(10)
        return True
    def subsystem_request(self,channel,name):
        entered.set()
        if mode["kind"] == "version":
            # ACK the subsystem but deliberately never send SFTP VERSION.
            return True
        release.wait(10)
        return original_subsystem(self,channel,name)
    monkeypatch.setattr(Authentication,"check_channel_request",channel_request)
    monkeypatch.setattr(Authentication,"check_channel_exec_request",exec_request)
    monkeypatch.setattr(Authentication,"check_channel_subsystem_request",subsystem_request)
    client = paramiko.Transport(("127.0.0.1",server.port)); client.start_client(timeout=2); client.auth_none("root")
    device = SSHTransport(DeviceConfig(host="127.0.0.1",port=server.port,state_dir=tmp_path / "host-state"))
    device._session = Mock(return_value=client)
    try: yield device,server,entered,release,mode
    finally:
        release.set(); device.close(); client.close(); server.close()


@pytest.mark.parametrize("mutating",[False,True])
def test_run_exec_ack_deadline_covers_request_not_only_command_output(stalled_device,mutating):
    device,server,entered,_release,_mode = stalled_device
    script = '# paperdesk-operation: {"action":"activate"}\nsynthetic change' if mutating else "printf synthetic"
    before = time.monotonic()
    with pytest.raises(DeviceError) as failure: device.run(script,timeout=.05)
    assert entered.is_set() and time.monotonic()-before < .35
    assert isinstance(failure.value,UnknownMutation) is mutating
    assert getattr(failure.value,"no_retry",False) is mutating
    assert server.commands == [script.encode()]
    assert not device._opening_requests


def test_input_stream_exec_ack_is_owned_and_deadline_bounded(stalled_device):
    device,_server,entered,_release,_mode = stalled_device
    before = time.monotonic()
    with pytest.raises(DeviceError): device.stream("synthetic input helper",timeout=.05)
    assert entered.is_set() and time.monotonic()-before < .35
    assert not device._streams and not device._opening_requests


@pytest.mark.parametrize("phase",["subsystem","version"])
def test_initial_sftp_subsystem_and_version_handshake_are_deadline_bounded(stalled_device,phase):
    device,_server,entered,_release,mode = stalled_device
    mode["kind"] = phase; device.FILE_OPEN_TIMEOUT = .05
    before = time.monotonic()
    with pytest.raises(DeviceError): device._files()
    assert entered.is_set() and time.monotonic()-before < .35
    assert device._sftp is None and not device._opening_requests


@pytest.mark.parametrize("phase",["exec","stream","subsystem","version"])
def test_main_thread_cancel_unblocks_pending_ack_or_version_without_waiting_timeout(stalled_device,phase):
    device,_server,entered,_release,mode = stalled_device
    mode["kind"] = "exec" if phase == "stream" else phase
    failures = []
    def operation():
        try:
            if phase == "exec": device.run('# paperdesk-operation: {"action":"activate"}\nsynthetic',timeout=30)
            elif phase == "stream": device.stream("synthetic input helper",timeout=30)
            else: device._files()
        except DeviceError as error: failures.append(error)
    worker = threading.Thread(target=operation); worker.start()
    assert entered.wait(1)
    before = time.monotonic(); device.cancel_streams(); worker.join(.5)
    assert not worker.is_alive() and time.monotonic()-before < .5
    assert len(failures) == 1 and isinstance(failures[0],UnknownMutation) is (phase == "exec")
    assert not device._opening_requests


def test_channel_open_ack_stall_does_not_dispatch_command_after_deadline(stalled_device):
    device,server,entered,_release,mode = stalled_device
    mode["kind"] = "open"
    before = time.monotonic()
    with pytest.raises(DeviceError) as failure: device.run('# paperdesk-operation: {"action":"activate"}\nsynthetic',timeout=.05)
    assert entered.is_set() and time.monotonic()-before < .35
    assert not isinstance(failure.value,UnknownMutation)
    assert not server.commands and not device._opening_requests
