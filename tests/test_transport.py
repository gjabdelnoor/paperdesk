"""Trust, path confinement, bounded I/O and ambiguous command checks."""
import io
import json
import stat
import socket
from contextlib import contextmanager
from unittest.mock import Mock

import paramiko
import pytest

from paperdesk.models import DeviceConfig
from paperdesk.transport import (SSHTransport, DeviceError, HostTrustRequired, UnknownMutation,
                                 probe_host_key, trust_host_key, fingerprint, _FingerprintPolicy,
                                 validate_remote_path, RemoteStream)


@pytest.mark.parametrize("host",["-oProxyCommand=evil", "root@tablet", "https://tablet", "tablet;rm", "tablet\n", "$(command)", "../../tablet", ""])
def test_malicious_hosts_are_refused_before_network(host,tmp_path,monkeypatch):
    connect = Mock(side_effect=AssertionError("Unexpected network"))
    monkeypatch.setattr("socket.create_connection",connect)
    with pytest.raises(DeviceError):
        probe_host_key(DeviceConfig(host=host,state_dir=tmp_path))
    assert not connect.called


@pytest.mark.parametrize("path",["relative", "/tmp/../etc/passwd", "/tmp/./file", "/tmp//file", "/tmp/evil\n", "/tmp/evil\x00", "/tmp\\evil"])
def test_path_traversal_and_controls_refused(path):
    with pytest.raises(DeviceError):
        validate_remote_path(path)


def test_no_saved_trust_means_no_authentication(tmp_path,monkeypatch):
    client = Mock()
    monkeypatch.setattr(paramiko,"SSHClient",client)
    transport = SSHTransport(DeviceConfig(state_dir=tmp_path,password="fixture-secret"))
    with pytest.raises(HostTrustRequired):
        transport.connect()
    assert not client.called


def test_exact_fingerprint_accepts_and_changed_key_refuses():
    first = paramiko.RSAKey.generate(1024)
    second = paramiko.RSAKey.generate(1024)
    policy = _FingerprintPolicy(fingerprint(first))
    client = Mock()
    policy.missing_host_key(client,"tablet",first)
    with pytest.raises(HostTrustRequired,match="changed"):
        policy.missing_host_key(client,"tablet",second)
    assert not client.get_host_keys.called  # no global OpenSSH trust mutation


def test_trust_file_never_contains_password_or_private_key(tmp_path):
    key = paramiko.RSAKey.generate(1024)
    config = DeviceConfig(state_dir=tmp_path,password="fixture-secret",key_path="/private/key")
    trust_host_key(config,fingerprint(key))
    content = (tmp_path / "trusted-hosts.json").read_text()
    assert "fixture-secret" not in content and "/private/key" not in content
    assert json.loads(content)["10.11.99.1:22"] == fingerprint(key)


def test_streamed_download_preserves_existing_file(tmp_path):
    transport = SSHTransport(DeviceConfig(state_dir=tmp_path))
    files = Mock()
    transport._files = Mock(return_value=files)
    output = tmp_path / "original.pdf"; output.write_bytes(b"original")
    with pytest.raises(DeviceError,match="preserve"):
        transport.download("/home/root/document.pdf",output)
    assert output.read_bytes() == b"original"
    assert not files.open.called


def test_streamed_download_reads_bounded_chunks_and_saves_bytes(tmp_path):
    transport = SSHTransport(DeviceConfig(state_dir=tmp_path))
    files = Mock()
    data = b"synthetic\x00" * 140000
    reader = io.BytesIO(data)
    sizes = []
    class Source:
        def read(self,size):
            sizes.append(size); return reader.read(size)
        def __enter__(self): return self
        def __exit__(self,*args): pass
    files.open.return_value = Source()
    transport._files = Mock(return_value=files)
    target = tmp_path / "copy.pdf"
    assert transport.download("/home/root/synthetic.pdf",target) == len(data)
    assert target.read_bytes() == data and all(size == 1024*1024 for size in sizes)


def test_write_restricted_and_symlink_escape_rejected(tmp_path):
    transport = SSHTransport(DeviceConfig(state_dir=tmp_path))
    files = Mock()
    files.lstat.return_value = Mock(st_mode=stat.S_IFDIR)
    transport._files = Mock(return_value=files)
    with pytest.raises(DeviceError,match="restricted"):
        transport.write_bytes("/etc/ssh/sshd_config",b"synthetic")
    assert not files.open.called
    files.lstat.return_value = Mock(st_mode=stat.S_IFLNK)
    with pytest.raises(DeviceError,match="symlink"):
        transport.write_bytes("/home/root/.local/lib/paperdesk/current/helper",b"synthetic")
    assert not files.open.called


@pytest.mark.parametrize("failure", [EOFError("lost"), socket.timeout("timeout"), paramiko.SSHException("broken session")])
def test_lost_atomic_write_receipt_reports_unknown_even_if_cleanup_fails(tmp_path,failure):
    transport = SSHTransport(DeviceConfig(state_dir=tmp_path))
    files = Mock()
    files.lstat.return_value = Mock(st_mode=stat.S_IFDIR)
    files.open.return_value.__enter__ = Mock(return_value=Mock())
    files.open.return_value.__exit__ = Mock(return_value=False)
    files.posix_rename.side_effect = failure
    files.remove.side_effect = EOFError("cleanup link also lost")
    transport._files = Mock(return_value=files)
    with pytest.raises(UnknownMutation) as outcome:
        transport.write_bytes("/home/root/.local/lib/paperdesk/stage/file",b"synthetic")
    assert outcome.value.no_retry
    files.rename.assert_not_called()


def test_interrupted_sftp_download_removes_only_new_partial_copy(tmp_path):
    transport = SSHTransport(DeviceConfig(state_dir=tmp_path))
    files = Mock(); files.open.side_effect = paramiko.SSHException("lost")
    transport._files = Mock(return_value=files)
    target = tmp_path / "new-copy.pdf"
    with pytest.raises(DeviceError,match="interrupted"):
        transport.download("/home/root/synthetic.pdf",target)
    assert not target.exists()


def stalled_channel():
    channel = Mock()
    channel.recv_ready.return_value = False
    channel.recv_stderr_ready.return_value = False
    channel.exit_status_ready.return_value = False
    return channel


def test_typed_install_timeout_is_unknown_and_read_timeout_is_normal(tmp_path,monkeypatch):
    transport = SSHTransport(DeviceConfig(state_dir=tmp_path))
    channel = stalled_channel()
    session = Mock(); session.open_session.return_value = channel
    transport._session = Mock(return_value=session)
    monkeypatch.setattr("paperdesk.transport.time.sleep",lambda _:None)
    with pytest.raises(UnknownMutation) as failed:
        transport.run('# paperdesk-operation: {"action":"activate"}\nchange',timeout=0)
    assert failed.value.no_retry and failed.value.outcome_unknown
    with pytest.raises(DeviceError,match="read-only") as read:
        transport.run('# paperdesk-operation: {"action":"probe"}\nread',timeout=0)
    assert not isinstance(read.value,UnknownMutation)


def test_web_tunnel_reuses_authenticated_transport_and_tablet_usb_listener(tmp_path):
    transport = SSHTransport(DeviceConfig(host="test-wireless.invalid",state_dir=tmp_path))
    session = Mock(); transport._session = Mock(return_value=session)
    with transport.web_connection() as web:
        assert web.sock is session.open_channel.return_value
        session.open_channel.assert_called_once_with("direct-tcpip",("10.11.99.1",80),("127.0.0.1",0),timeout=10)
    session.open_channel.return_value.close.assert_called_once()


def test_pen_stream_returns_available_bytes_without_buffer_fill():
    channel = Mock()
    channel.closed = False
    channel.recv.return_value = b"event"
    stream = RemoteStream(channel)
    assert stream.read(65536) == b"event"
    channel.recv.assert_called_once_with(65536)
    assert not stream.stdout.read.called
    assert stream.write(b"payload") == 7
    channel.sendall.assert_called_once_with(b"payload")
    stream.close(); stream.close()
    channel.close.assert_called_once()
