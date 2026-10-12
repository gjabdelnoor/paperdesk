"""Actual SSH handshakes against an ephemeral localhost-only test server."""
import socket
import threading
import time

import paramiko
import pytest

from paperdesk.models import DeviceConfig
from paperdesk.transport import SSHTransport, HostTrustRequired, fingerprint, probe_host_key, trust_host_key


class Server(paramiko.ServerInterface):
    def __init__(self, owner):
        self.owner = owner
        self.command = threading.Event()

    def check_auth_password(self, username, password):
        self.owner.auth_attempts.append((username, password))
        return paramiko.AUTH_SUCCESSFUL if password == "fixture-only" else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.owner.commands.append(command)
        self.command.set()
        return True


class LoopbackSSH:
    def __init__(self):
        self.key = paramiko.RSAKey.generate(2048)
        self.auth_attempts = []
        self.commands = []
        self.stopped = threading.Event()
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(4)
        self.socket.settimeout(0.1)
        self.port = self.socket.getsockname()[1]
        self.transports = []
        self.thread = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()

    def _listen(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection):
        transport = paramiko.Transport(connection)
        self.transports.append(transport)
        transport.add_server_key(self.key)
        handler = Server(self)
        try:
            transport.start_server(server=handler)
            channel = transport.accept(3)
            if channel and handler.command.wait(3):
                channel.sendall(b"paperdesk-loopback\n")
                channel.send_exit_status(0)
                channel.shutdown_write()
                time.sleep(0.05)
                channel.close()
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def close(self):
        self.stopped.set()
        self.socket.close()
        for transport in self.transports:
            transport.close()
        self.thread.join(2)


@pytest.fixture
def server():
    instance = LoopbackSSH()
    try:
        yield instance
    finally:
        instance.close()


def test_pairing_observes_without_auth_then_authenticates_exact_key(server, tmp_path):
    config = DeviceConfig(host="127.0.0.1", port=server.port, password="fixture-only", state_dir=tmp_path)
    device = SSHTransport(config)
    with pytest.raises(HostTrustRequired):
        device.connect()
    observed = probe_host_key(config)
    assert observed["fingerprint"] == fingerprint(server.key)
    assert server.auth_attempts == []
    trust_host_key(config, observed["fingerprint"])
    device.connect()
    try:
        assert device.run("echo test-only") == "paperdesk-loopback\n"
        assert server.auth_attempts == [("root", "fixture-only")]
    finally:
        device.close()
    assert not device.connected


def test_changed_host_key_is_rejected_before_credentials(server, tmp_path):
    config = DeviceConfig(host="127.0.0.1", port=server.port, password="fixture-only", state_dir=tmp_path)
    trust_host_key(config, fingerprint(server.key))
    server.key = paramiko.RSAKey.generate(2048)
    device = SSHTransport(config)
    with pytest.raises(HostTrustRequired, match="changed"):
        device.connect()
    assert server.auth_attempts == []
    assert not device.connected
