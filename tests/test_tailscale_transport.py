"""Real isolated SSH-none handshakes through an argv-only stdio proxy.

No Tailscale daemon, tailnet, physical tablet, browser or VPN is accessed. The
proxy fixture speaks the same byte-pipe interface as tailscale nc, and the
server provides real Paramiko SFTP plus a synthetic forwarded HTTP endpoint.
"""
import io
import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import paramiko
import pytest

from paperdesk.models import DeviceConfig
from paperdesk.transport import (SSHTransport, HostTrustRequired, DeviceError,
                                 TailscaleAuthorizationRequired, probe_host_key, trust_host_key, fingerprint, _ProcessSocket)


class Files(paramiko.SFTPServerInterface):
    def __init__(self,*args,root,**kwargs):
        super().__init__(*args,**kwargs); self.root = root
    def path(self,name):
        candidate = (self.root / name.lstrip("/")).resolve()
        candidate.relative_to(self.root)
        return candidate
    def stat(self,path):
        try: return paramiko.SFTPAttributes.from_stat(self.path(path).stat())
        except OSError as error: return paramiko.SFTPServer.convert_errno(error.errno)
    lstat = stat
    def list_folder(self,path):
        try:
            result = []
            for name in self.path(path).iterdir():
                item = paramiko.SFTPAttributes.from_stat(name.stat()); item.filename = name.name; result.append(item)
            return result
        except OSError as error: return paramiko.SFTPServer.convert_errno(error.errno)
    def open(self,path,flags,attr):
        try:
            descriptor = os.open(self.path(path),flags,0o600)
            mode = "r+b" if flags & os.O_RDWR else "wb" if flags & os.O_WRONLY else "rb"
            handle = paramiko.SFTPHandle(flags)
            stream = os.fdopen(descriptor,mode)
            if flags & (os.O_WRONLY | os.O_RDWR): handle.writefile = stream
            if not flags & os.O_WRONLY: handle.readfile = stream
            return handle
        except OSError as error: return paramiko.SFTPServer.convert_errno(error.errno)
    def chattr(self,path,attributes):
        try: paramiko.SFTPServer.set_file_attr(str(self.path(path)),attributes); return paramiko.SFTP_OK
        except OSError as error: return paramiko.SFTPServer.convert_errno(error.errno)
    def posix_rename(self,source,target):
        try: os.replace(self.path(source),self.path(target)); return paramiko.SFTP_OK
        except OSError as error: return paramiko.SFTPServer.convert_errno(error.errno)
    def rename(self,source,target): return self.posix_rename(source,target)
    def remove(self,path):
        try: self.path(path).unlink(); return paramiko.SFTP_OK
        except OSError as error: return paramiko.SFTPServer.convert_errno(error.errno)


class Authentication(paramiko.ServerInterface):
    def __init__(self,owner): self.owner = owner
    def check_auth_none(self,username):
        self.owner.none_attempts.append(username); self.owner.auth_started.set()
        if self.owner.hold_auth: self.owner.auth_release.wait(10)
        return paramiko.AUTH_SUCCESSFUL if self.owner.allow_none else paramiko.AUTH_FAILED
    def check_auth_password(self,user,password):
        self.owner.password_attempts.append((user,password)); return paramiko.AUTH_FAILED
    def get_allowed_auths(self,username): return "none"
    def get_banner(self): return self.owner.banner,"en-US"
    def check_channel_request(self,kind,identifier):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
    def check_channel_exec_request(self,channel,command):
        self.owner.commands.append(command)
        def respond():
            channel.sendall(b"synthetic-ssh-none\n"); channel.send_exit_status(0); channel.shutdown_write()
        threading.Thread(target=respond,daemon=True).start()
        return True
    def check_channel_direct_tcpip_request(self,identifier,origin,destination):
        self.owner.forwards[identifier] = (origin,destination)
        return paramiko.OPEN_SUCCEEDED


class SSHNoneServer:
    def __init__(self,root):
        self.root = root.resolve(); self.key = paramiko.RSAKey.generate(2048)
        self.none_attempts = []; self.password_attempts = []; self.commands = []; self.forwards = {}
        self.allow_none = True; self.hold_auth = False
        self.banner = ""
        self.auth_started = threading.Event(); self.auth_release = threading.Event()
        self.stop = threading.Event(); self.transports = []
        self.socket = socket.socket(); self.socket.bind(("127.0.0.1",0)); self.socket.listen(4); self.socket.settimeout(.1)
        self.port = self.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.listen,daemon=True); self.thread.start()
    def listen(self):
        while not self.stop.is_set():
            try: connection,_address = self.socket.accept()
            except socket.timeout: continue
            except OSError: return
            threading.Thread(target=self.serve,args=(connection,),daemon=True).start()
    def serve(self,connection):
        transport = paramiko.Transport(connection); self.transports.append(transport)
        transport.add_server_key(self.key)
        transport.set_subsystem_handler("sftp",paramiko.SFTPServer,Files,root=self.root)
        try:
            transport.start_server(server=Authentication(self))
            while transport.is_active() and not self.stop.is_set():
                channel = transport.accept(.1)
                if channel is None: continue
                if channel.get_id() in self.forwards:
                    threading.Thread(target=self.http,args=(channel,),daemon=True).start()
        except (OSError,EOFError,paramiko.SSHException): pass
        finally: transport.close()
    def http(self,channel):
        try:
            request = bytearray()
            while b"\r\n\r\n" not in request:
                block = channel.recv(4096)
                if not block: return
                request.extend(block)
            channel.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            channel.shutdown_write()
        except (OSError,EOFError): pass
    def close(self):
        self.stop.set(); self.auth_release.set(); self.socket.close()
        for transport in self.transports: transport.close()
        self.thread.join(1)


@pytest.fixture
def tailnet_fixture(tmp_path,monkeypatch):
    tablet = tmp_path / "tablet"; tablet.mkdir()
    server = SSHNoneServer(tablet)
    # sys.executable runs a script named nc, allowing the exact argv shape on
    # Windows too without using cmd, shell=True or an executable script shim.
    (tmp_path / "nc").write_text('''import socket,sys,threading
connection=socket.create_connection(("127.0.0.1",int(sys.argv[2])),5)
def outgoing():
 try:
  while True:
   data=sys.stdin.buffer.read1(65536)
   if not data:
    connection.shutdown(socket.SHUT_WR); return
   connection.sendall(data)
 except OSError: pass
threading.Thread(target=outgoing,daemon=True).start()
try:
 while True:
  data=connection.recv(65536)
  if not data: break
  sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
finally: connection.close()
''')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("paperdesk.transport._tailscale_executable",lambda:sys.executable)
    original_proxy = _ProcessSocket
    server.proxies = []
    def record_proxy(*args,**kwargs):
        proxy = original_proxy(*args,**kwargs); server.proxies.append(proxy); return proxy
    monkeypatch.setattr("paperdesk.transport._ProcessSocket",record_proxy)
    config = DeviceConfig(host="synthetic-tailnet.invalid",port=server.port,transport_mode="tailscale",
                          state_dir=tmp_path / "host-state",password="must-never-be-sent",key_path="must-never-be-read")
    try: yield server,config
    finally: server.close()


def pair(config):
    observed = probe_host_key(config); trust_host_key(config,observed["fingerprint"])
    return observed


def test_real_none_auth_proxy_sftp_streamed_copy_and_web_forwarding(tailnet_fixture,tmp_path):
    server,config = tailnet_fixture
    observed = pair(config)
    assert observed["fingerprint"] == fingerprint(server.key) and observed["transport_mode"] == "tailscale"
    assert not server.none_attempts
    remote = server.root / "home/root/.local/lib/paperdesk/stage"; remote.mkdir(parents=True)
    transport = SSHTransport(config); transport.connect(); proxy = transport._proxy
    try:
        assert server.none_attempts == ["root"] and not server.password_attempts
        assert transport.run("synthetic read-only command") == "synthetic-ssh-none\n"
        data = b"Synthetic SFTP fixture\x00" * 10000
        transport.write_bytes("/home/root/.local/lib/paperdesk/stage/test.bin",data)
        assert transport.read_bytes("/home/root/.local/lib/paperdesk/stage/test.bin") == data
        output = tmp_path / "download-copy.bin"
        assert transport.download("/home/root/.local/lib/paperdesk/stage/test.bin",output) == len(data)
        assert output.read_bytes() == data
        with transport.web_connection() as connection:
            connection.request("GET","/documents/")
            assert connection.getresponse().read() == b"ok"
        assert any(destination == ("10.11.99.1",80) for _origin,destination in server.forwards.values())
    finally: transport.close()
    assert not transport.connected and proxy.process.poll() is not None


def test_changed_key_refused_before_auth_and_nc_proxy_is_reaped(tailnet_fixture):
    server,config = tailnet_fixture; pair(config)
    server.key = paramiko.RSAKey.generate(2048)
    transport = SSHTransport(config)
    with pytest.raises(HostTrustRequired,match="changed"): transport.connect()
    assert not server.none_attempts and not server.password_attempts
    assert transport._proxy is None and not transport.connected
    assert all(proxy.process.poll() is not None for proxy in server.proxies)


def test_tailnet_policy_refusal_never_falls_back_to_password(tailnet_fixture):
    server,config = tailnet_fixture; pair(config); server.allow_none = False
    transport = SSHTransport(config)
    with pytest.raises(DeviceError,match="authorization|check-mode"): transport.connect()
    assert server.none_attempts == ["root"] and not server.password_attempts
    assert transport._proxy is None
    assert all(proxy.process.poll() is not None for proxy in server.proxies)


def test_pending_signin_link_is_returned_without_opening_browser(tailnet_fixture,monkeypatch):
    server,config = tailnet_fixture; pair(config); server.allow_none = False
    server.banner = "To authorize: https://login.tailscale.com/a/synthetic-fixture"
    browser = Mock(side_effect=AssertionError("Browser opened without explicit user action"))
    monkeypatch.setattr("webbrowser.open",browser)
    with pytest.raises(TailscaleAuthorizationRequired) as pending: SSHTransport(config).connect()
    assert pending.value.auth_url == "https://login.tailscale.com/a/synthetic-fixture"
    assert not browser.called and not server.password_attempts
    assert all(proxy.process.poll() is not None for proxy in server.proxies)


def test_cancellation_during_pending_auth_kills_only_owned_proxy(tailnet_fixture):
    server,config = tailnet_fixture; pair(config); server.hold_auth = True
    transport = SSHTransport(config); failures = []
    def connect():
        try: transport.connect()
        except DeviceError as error: failures.append(error)
    worker = threading.Thread(target=connect); worker.start()
    assert server.auth_started.wait(2)
    proxy = transport._proxy; before = time.monotonic()
    transport.cancel_streams(); worker.join(2)
    assert not worker.is_alive() and time.monotonic()-before < 2
    assert failures and proxy.process.poll() is not None and not transport.connected


def test_unpaired_tailnet_connect_spawns_no_process(tmp_path,monkeypatch):
    spawn = Mock(side_effect=AssertionError("Unexpected process"))
    monkeypatch.setattr("paperdesk.transport.subprocess.Popen",spawn)
    config = DeviceConfig(host="fixture.invalid",transport_mode="tailscale",state_dir=tmp_path)
    with pytest.raises(HostTrustRequired): SSHTransport(config).connect()
    assert not spawn.called


def test_invalid_mode_or_host_cannot_become_proxy_arguments(tmp_path,monkeypatch):
    spawn = Mock(side_effect=AssertionError("Unexpected process"))
    monkeypatch.setattr("paperdesk.transport.subprocess.Popen",spawn)
    for mode,host in (("auto","fixture.invalid"),("tailscale","--malicious"),("tailscale","fixture; command")):
        with pytest.raises(DeviceError): probe_host_key(DeviceConfig(host=host,transport_mode=mode,state_dir=tmp_path))
    assert not spawn.called


def test_missing_cli_is_actionable_and_never_auto_installs(tmp_path,monkeypatch):
    monkeypatch.setattr("paperdesk.transport.shutil.which",lambda _:None)
    monkeypatch.setattr("paperdesk.transport.sys.platform","linux")
    spawn = Mock(side_effect=AssertionError("Unexpected process")); monkeypatch.setattr("paperdesk.transport.subprocess.Popen",spawn)
    with pytest.raises(DeviceError,match="command-line client is unavailable"):
        probe_host_key(DeviceConfig(host="fixture.invalid",transport_mode="tailscale",state_dir=tmp_path))
    assert not spawn.called
