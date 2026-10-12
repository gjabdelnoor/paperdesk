"""Authenticated SSH transport with explicit host trust and bounded transfers.

There is no auto-accept policy, external SSH process, or background tunnel. The
tablet's USB web interface is reached by a direct-tcpip channel through the
already authenticated connection, including when the SSH host is wireless.
Explicit Tailscale mode launches only its nc byte-stream proxy and authenticates
SSH with the Tailscale none method; it never installs or starts a VPN/browser.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import json
import os
import queue
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

import paramiko
import portalocker
from platformdirs import user_state_path

from .models import DeviceConfig


class DeviceError(RuntimeError):
    """An actionable failure without authentication data in its message."""


class HostTrustRequired(DeviceError):
    """The UI must display an observed fingerprint and request explicit trust."""


class TailscaleAuthorizationRequired(DeviceError):
    """Tailnet policy/check-mode authorization is pending or denied."""
    def __init__(self, message: str, auth_url: str = ""):
        self.auth_url = auth_url
        super().__init__(message + (f" Sign-in link from the verified server: {auth_url}" if auth_url else ""))


class UnknownMutation(DeviceError):
    """A remote write may have happened; do not replay it automatically."""
    no_retry = True
    outcome_unknown = True


def state_directory(config: DeviceConfig) -> Path:
    return Path(config.state_dir) if config.state_dir else user_state_path("PaperDesk", "PaperDesk")


def validate_config(config: DeviceConfig) -> None:
    if config.transport_mode not in ("ssh", "tailscale"):
        raise DeviceError("Choose SSH or the explicit Tailscale SSH transport mode.")
    host = config.host
    if not isinstance(host, str) or not host or len(host) > 253:
        raise DeviceError("Enter a tablet IP address or hostname.")
    # Reject URLs, user@host, flags, whitespace and shell syntax. IPv6 is valid
    # without brackets; DNS names may include the underscore used by mDNS.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:%-]*", host) or ".." in host:
        raise DeviceError("Host must be a plain IP address or hostname, without a URL or SSH options.")
    if not isinstance(config.port, int) or isinstance(config.port, bool) or not 1 <= config.port <= 65535:
        raise DeviceError("SSH port must be between 1 and 65535.")
    if not isinstance(config.username, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,63}", config.username):
        raise DeviceError("Enter a valid tablet SSH username.")


def validate_remote_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or "\\" in path:
        raise DeviceError("Remote path must be an absolute POSIX path.")
    if any(part in (".", "..") for part in path.split("/")) or "//" in path:
        raise DeviceError("Remote path traversal is refused.")
    if any(ord(char) < 32 for char in path):
        raise DeviceError("Control characters are not allowed in a remote path.")
    return str(PurePosixPath(path))


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _tailscale_executable() -> str:
    executable = shutil.which("tailscale")
    if executable:
        return executable
    candidates = []
    if sys.platform == "darwin":
        # Use CLI links from a standalone install, rather than invoking an
        # arbitrary App Store GUI executable as a byte-pipe helper.
        candidates.extend((Path("/usr/local/bin/tailscale"), Path("/opt/homebrew/bin/tailscale")))
    elif sys.platform == "win32":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(variable):
                candidates.append(Path(os.environ[variable]) / "Tailscale" / "tailscale.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise DeviceError("The Tailscale command-line client is unavailable. Install its standalone client, sign in to your tailnet, and select Tailscale mode explicitly.")


class _ProcessSocket:
    """Bounded, portable socket interface around an argv-only nc subprocess.

    Windows anonymous pipes cannot use select(). Blocking pipe reads/writes run
    in two owned threads with bounded queues; socket timeouts remain bounded,
    and close terminates the proxy so no pending bytes can be sent later.
    """
    def __init__(self, argv: list[str], timeout: float = 10):
        self._timeout = timeout
        self._closed = threading.Event()
        self._read_eof = threading.Event()
        self._close_lock = threading.Lock()
        self._incoming = queue.Queue(maxsize=16)
        self._outgoing = queue.Queue(maxsize=2)
        self._buffer = bytearray()
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, bufsize=0, shell=False,
                                            creationflags=flags)
        except OSError as exc:
            raise DeviceError("Cannot launch the Tailscale nc proxy. Check its standalone client installation.") from exc
        self._reader = threading.Thread(target=self._read_pipe, name="PaperDesk-tailnet-read", daemon=True)
        self._writer = threading.Thread(target=self._write_pipe, name="PaperDesk-tailnet-write", daemon=True)
        self._reader.start(); self._writer.start()

    def _enqueue(self, item):
        while not self._closed.is_set():
            try:
                self._incoming.put(item, timeout=0.1)
                return
            except queue.Full:
                pass

    def _read_pipe(self):
        try:
            while not self._closed.is_set():
                block = self.process.stdout.read(65536)
                if not block:
                    break
                self._enqueue(block)
        except (OSError, ValueError):
            pass
        finally:
            self._read_eof.set()
            self._enqueue(None)

    def _write_pipe(self):
        while not self._closed.is_set():
            try:
                data, result, done = self._outgoing.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                result["count"] = self.process.stdin.write(data)
            except (OSError, ValueError) as error:
                result["error"] = error
            finally:
                done.set()

    def settimeout(self, timeout):
        self._timeout = timeout

    def recv(self, amount: int) -> bytes:
        if amount <= 0:
            return b""
        if not self._buffer:
            if self._closed.is_set() or self._read_eof.is_set() and self._incoming.empty():
                return b""
            try:
                block = self._incoming.get(timeout=self._timeout)
            except queue.Empty as exc:
                raise socket.timeout("Tailscale proxy read timed out") from exc
            if block is None:
                return b""
            self._buffer.extend(block)
        result = bytes(self._buffer[:amount])
        del self._buffer[:amount]
        return result

    def send(self, data: bytes) -> int:
        if self._closed.is_set():
            raise OSError("Tailscale proxy is closed")
        result = {}
        done = threading.Event()
        # Paramiko uses a short polling read timeout. Pipe backpressure needs a
        # separate bounded write allowance; cancellation still closes the pipe.
        timeout = max(float(self._timeout or 10), 10)
        try:
            self._outgoing.put((bytes(data), result, done), timeout=timeout)
        except queue.Full as exc:
            self.close()
            raise socket.timeout("Tailscale proxy write queue timed out") from exc
        if not done.wait(timeout):
            self.close()
            raise socket.timeout("Tailscale proxy write timed out")
        if "error" in result or not result.get("count"):
            raise OSError("Tailscale proxy input pipe closed")
        return result["count"]

    def close(self):
        if not self._close_lock.acquire(blocking=False):
            return
        try:
            if self._closed.is_set():
                return
            self._closed.set()
            while True:
                try:
                    _data, result, done = self._outgoing.get_nowait()
                    result["error"] = OSError("Tailscale proxy was cancelled")
                    done.set()
                except queue.Empty:
                    break
            if self.process.poll() is None:
                try:
                    self.process.terminate()
                except OSError:
                    pass
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=1)
            for handle in (self.process.stdin, self.process.stdout):
                try:
                    handle.close()
                except (OSError, ValueError):
                    pass
            for worker in (self._reader, self._writer):
                if worker is not threading.current_thread():
                    worker.join(0.2)
        finally:
            self._close_lock.release()


def _connection_socket(config: DeviceConfig, timeout: float):
    if config.transport_mode == "tailscale":
        return _ProcessSocket([_tailscale_executable(), "nc", config.host, str(config.port)], timeout)
    return socket.create_connection((config.host, config.port), timeout=timeout)


class PortableLock:
    """Process-safe, nonblocking file lock on Linux, macOS and Windows."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = portalocker.Lock(str(self.path), mode="a+b", timeout=0,
                                      flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            self._lock.acquire()
        except portalocker.LockException as exc:
            raise DeviceError("Another PaperDesk operation is running; wait for it to finish.") from exc
        return self

    def __exit__(self, *_args):
        if self._lock is not None:
            self._lock.release()


def atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            json.dump(data, handle, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _trust_path(config: DeviceConfig) -> Path:
    return state_directory(config) / "trusted-hosts.json"


def _host_id(config: DeviceConfig) -> str:
    return f"{config.host.lower()}:{config.port}"


def _load_trust(config: DeviceConfig) -> dict:
    path = _trust_path(config)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in data.items()):
            raise ValueError()
        return data
    except (ValueError, OSError) as exc:
        raise DeviceError("Saved host trust is unreadable. Inspect trusted-hosts.json before pairing again.") from exc


def probe_host_key(config: DeviceConfig, timeout: float = 10) -> dict:
    """Observe a server key without authenticating or saving trust."""
    validate_config(config)
    connection = None
    session = None
    try:
        connection = _connection_socket(config, timeout)
        session = paramiko.Transport(connection)
        session.banner_timeout = timeout
        session.start_client(timeout=timeout)
        key = session.get_remote_server_key()
        return {"host": config.host, "port": config.port, "fingerprint": fingerprint(key), "key_type": key.get_name(), "transport_mode": config.transport_mode}
    except (OSError, paramiko.SSHException) as exc:
        raise DeviceError("Cannot observe the tablet SSH key. Check its address, developer mode, and connection.") from exc
    finally:
        if session is not None:
            session.close()
        if connection is not None:
            connection.close()


def trust_host_key(config: DeviceConfig, observed_fingerprint: str) -> dict:
    """Persist only the fingerprint the user explicitly confirmed in the UI.

    This does not probe a second key or authenticate. The next connect verifies
    that the server still presents this exact key before sending credentials.
    """
    validate_config(config)
    if not isinstance(observed_fingerprint, str) or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", observed_fingerprint):
        raise DeviceError("A valid observed SHA256 host fingerprint is required.")
    with PortableLock(state_directory(config) / "trust.lock"):
        data = _load_trust(config)
        data[_host_id(config)] = observed_fingerprint
        atomic_json(_trust_path(config), data)
    return {"ok": True, "message": "Host fingerprint saved. Connect to authenticate.", "fingerprint": observed_fingerprint}


class _FingerprintPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected: str):
        self.expected = expected

    def missing_host_key(self, client, hostname, key):
        actual = fingerprint(key)
        if actual != self.expected:
            raise HostTrustRequired(
                f"The tablet host key changed. Observed {actual}. Verify it on the tablet before pairing again.")
        # SSHClient checks policy before beginning authentication. We do not
        # add the key to global/user OpenSSH known_hosts or silently persist it.


class RemoteStream:
    """Duplex remote process stream, closed explicitly by its owning feature."""

    def __init__(self, channel: paramiko.Channel):
        self.channel = channel
        self.stdout = channel.makefile("rb", -1)
        self.stdin = channel.makefile_stdin("wb", 0)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size >= 0:
            # recv returns available bytes rather than waiting to fill a pen
            # buffer; ChannelFile.read(n) introduces unacceptable latency.
            return self.channel.recv(size)
        return self.stdout.read(size)

    def readline(self, size: int = -1) -> bytes:
        return self.stdout.readline(size)

    def write(self, data: bytes) -> int:
        if self.closed or self.channel.closed:
            raise DeviceError("The tablet input stream is disconnected. Reconnect before sending more input.")
        self.channel.sendall(data)
        return len(data)

    def flush(self) -> None:
        self.stdin.flush()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.channel.shutdown_write()
        except (OSError, EOFError, paramiko.SSHException):
            pass
        for item in (self.stdin, self.stdout, self.channel):
            try:
                item.close()
            except (OSError, EOFError, paramiko.SSHException):
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class SSHTransport:
    """Device transport. Construction never contacts a device."""

    MAX_READ = 64 * 1024 * 1024
    FILE_OPEN_TIMEOUT = 30.0
    WRITE_ROOTS = ("/home/root/.local/lib/paperdesk", "/run/systemd/system",
                   "/home/root/.cache/remarkable-direct-paste", "/tmp/paperdesk-viewport")

    def __init__(self, config: DeviceConfig):
        validate_config(config)
        self.config = config
        self.client = None
        self.host_fingerprint = ""
        self._tailscale_session = None
        self._proxy = None
        self._sftp = None
        self._streams: list[RemoteStream] = []
        self._streams_lock = threading.RLock()
        self._web_channels: list[paramiko.Channel] = []
        self._opening_requests: list[dict] = []
        self._sftp_channel = None
        self._cancel_generation = 0

    @property
    def connected(self) -> bool:
        if self._tailscale_session is not None:
            return self._tailscale_session.is_active() and self._tailscale_session.is_authenticated()
        if self.client is None:
            return False
        session = self.client.get_transport()
        return bool(session and session.is_active() and session.is_authenticated())

    def connect(self) -> None:
        validate_config(self.config)
        if self.connected:
            return
        if self.client is not None or self._tailscale_session is not None:
            self.close()
        expected = _load_trust(self.config).get(_host_id(self.config))
        if not expected:
            raise HostTrustRequired("This tablet is not paired. Observe its SSH fingerprint and confirm trust before connecting.")
        with self._streams_lock:
            generation = self._cancel_generation
        if self.config.transport_mode == "tailscale":
            self._connect_tailscale(expected,generation)
            return
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_FingerprintPolicy(expected))
        self.client = client  # owned while banner/authentication is pending too
        try:
            client.connect(hostname=self.config.host, port=self.config.port,
                           username=self.config.username,
                           password=self.config.password or None,
                           key_filename=self.config.key_path or None,
                           timeout=10, banner_timeout=10, auth_timeout=15,
                           look_for_keys=not bool(self.config.key_path or self.config.password),
                           allow_agent=not bool(self.config.key_path or self.config.password))
            if generation != self._cancel_generation:
                raise DeviceError("The SSH connection request was cancelled. Reconnect explicitly to continue.")
        except DeviceError:
            self.close()
            raise
        except paramiko.AuthenticationException as exc:
            self.close()
            raise DeviceError("SSH authentication failed. Use the developer-mode password or the correct private key.") from exc
        except (paramiko.SSHException, OSError, ValueError) as exc:
            self.close()
            raise DeviceError("Cannot connect over SSH. Check developer mode, the tablet address, and the cable or network.") from exc
        self.client = client
        self.host_fingerprint = fingerprint(client.get_transport().get_remote_server_key())

    def _connect_tailscale(self, expected: str, generation: int) -> None:
        proxy = _connection_socket(self.config, 10)
        session = paramiko.Transport(proxy)
        session.banner_timeout = session.handshake_timeout = 10
        session.auth_timeout = 15
        self._proxy, self._tailscale_session = proxy, session
        try:
            if generation != self._cancel_generation:
                raise DeviceError("The Tailscale connection request was cancelled before authentication.")
            session.start_client(timeout=10)
            observed = fingerprint(session.get_remote_server_key())
            if observed != expected:
                raise HostTrustRequired(f"The tablet host key changed. Observed {observed}. Verify it before pairing again.")
            # Passwords and private keys are deliberately unused in this
            # explicit mode; Tailscale policy authenticates the node identity.
            session.auth_none(self.config.username)
            if not session.is_authenticated():
                raise paramiko.AuthenticationException("Tailscale authorization not confirmed")
            if generation != self._cancel_generation:
                raise DeviceError("The Tailscale connection request was cancelled. Reconnect explicitly to continue.")
            self.host_fingerprint = observed
        except HostTrustRequired:
            self.close()
            raise
        except DeviceError:
            self.close()
            raise
        except paramiko.AuthenticationException as exc:
            banner = session.get_banner() or b""
            if isinstance(banner, bytes):
                banner = banner.decode("utf-8", "replace")
            link = re.search(r"https://(?:login|console)\.tailscale\.com/[^\s<>\"']+", str(banner)[:4096])
            self.close()
            raise TailscaleAuthorizationRequired("Tailscale SSH authorization is unavailable or needs check-mode sign-in. Review your tailnet SSH policy and authorize it with the Tailscale client, then reconnect explicitly.", link.group(0) if link else "") from exc
        except (OSError, EOFError, ValueError, paramiko.SSHException) as exc:
            self.close()
            raise DeviceError("Cannot connect through Tailscale nc. Check that its client is signed in, the tablet is online, and Tailscale SSH is enabled for your user.") from exc

    def close(self) -> None:
        self.cancel_streams()
        owned = (self._sftp, self.client, self._tailscale_session, self._proxy)
        self._sftp = self.client = self._tailscale_session = self._proxy = None
        for item in owned:
            if item is not None:
                try:
                    item.close()
                except (OSError, EOFError, paramiko.SSHException):
                    pass

    disconnect = close

    def cancel_streams(self) -> None:
        """Close owned live input/web channels without disconnecting SSH.

        Closing the channel unblocks bounded reads/writes in the worker. It
        never launches a remote command or retries an interrupted mutation.
        """
        with self._streams_lock:
            streams, web = list(self._streams), list(self._web_channels)
            opening = [request["channel"] or request["session"] for request in self._opening_requests]
            sftp = self._sftp_channel
            self._sftp = self._sftp_channel = None
            self._cancel_generation += 1
            self._streams.clear()
            self._web_channels.clear()
        for item in streams + web + opening + ([sftp] if sftp is not None else []):
            try:
                item.close()
            except (OSError, EOFError, paramiko.SSHException):
                pass
        if self.client is not None and not self.connected:
            self.client.close()
        if self._tailscale_session is not None and not self._tailscale_session.is_authenticated():
            # GUI cancellation during handshake/authentication must terminate
            # the proxy instead of waiting for a browser approval indefinitely.
            if self._proxy is not None:
                self._proxy.close()

    def _session(self):
        if not self.connected:
            raise DeviceError("Connect to a paired tablet first.")
        return self._tailscale_session if self._tailscale_session is not None else self.client.get_transport()

    def _new_channel(self, timeout: float):
        """Own channel open/ACK waits under one deadline and cancel watchdog.

        Paramiko exec_command/invoke_subsystem wait on an Event without using
        Channel.settimeout. Closing their owned channel unblocks that Event.
        During a pending channel-open ACK no Channel is returned yet, so the
        watchdog closes only this app's authenticated SSH session instead.
        """
        if not isinstance(timeout, (int,float)) or isinstance(timeout,bool) or timeout < 0:
            raise DeviceError("A nonnegative device-operation timeout is required.")
        session = self._session()
        deadline = time.monotonic() + timeout
        request = {"session":session,"channel":None,"expired":threading.Event()}
        with self._streams_lock:
            generation = self._cancel_generation
            self._opening_requests.append(request)
        def expire():
            request["expired"].set()
            with self._streams_lock:
                owned = request["channel"] or request["session"]
            try: owned.close()
            except (OSError,EOFError,paramiko.SSHException): pass
        timer = threading.Timer(max(0,deadline-time.monotonic()),expire)
        timer.daemon = True; request["timer"] = timer; timer.start()
        try:
            channel = session.open_session(timeout=max(.001,min(timeout,15)))
            with self._streams_lock:
                request["channel"] = channel
                cancelled = generation != self._cancel_generation
            if request["expired"].is_set() or time.monotonic() >= deadline:
                raise DeviceError("Timed out opening the SSH channel before any device command was sent.")
            if cancelled:
                raise DeviceError("The SSH channel request was cancelled before a device command was sent.")
            channel.settimeout(max(.001,deadline-time.monotonic()))
            return channel, deadline, request
        except (OSError,EOFError,paramiko.SSHException) as exc:
            self._finish_channel(request,close=True)
            raise DeviceError("Opening the SSH channel timed out or was interrupted before any device command was sent.") from exc
        except Exception:
            self._finish_channel(request,close=True)
            raise

    def _finish_channel(self, request: dict, *, close: bool) -> None:
        request["timer"].cancel()
        with self._streams_lock:
            if request in self._opening_requests:
                self._opening_requests.remove(request)
        if close and request["channel"] is not None:
            try: request["channel"].close()
            except (OSError,EOFError,paramiko.SSHException): pass

    def _files(self):
        self._session()
        if self._sftp is None:
            channel, _deadline, request = self._new_channel(self.FILE_OPEN_TIMEOUT)
            completed = False
            try:
                # Set the timeout before subsystem negotiation and version
                # exchange, rather than after from_transport has finished.
                channel.invoke_subsystem("sftp")
                files = paramiko.SFTPClient(channel)
                if request["expired"].is_set() or time.monotonic() >= _deadline:
                    raise DeviceError("Starting the tablet SFTP subsystem timed out.")
                with self._streams_lock:
                    if channel.closed:
                        raise DeviceError("Starting the tablet SFTP subsystem was cancelled.")
                    self._sftp, self._sftp_channel = files, channel
                    completed = True
            except (OSError, EOFError, paramiko.SSHException, paramiko.SFTPError) as exc:
                raise DeviceError("The tablet SFTP subsystem is unavailable. For Tailscale SSH, check that its server permits SFTP and can launch its embedded tailscaled server.") from exc
            finally:
                self._finish_channel(request,close=not completed)
            self._sftp.get_channel().settimeout(30)
        return self._sftp

    def run(self, script: str, timeout: float = 30, mutation: bool | None = None) -> str:
        if not isinstance(script, str) or not script or "\x00" in script:
            raise DeviceError("Invalid device command.")
        if mutation is None:
            marker = re.search(r"^# paperdesk-operation: (.+)$", script, re.MULTILINE)
            if marker:
                try:
                    operation = json.loads(marker.group(1))
                    mutation = operation.get("action") not in ("probe", "verify", "heartbeat", "art-loaded")
                    if operation.get("action") == "guard":
                        mutation = bool(operation.get("mkdir"))
                except (ValueError, AttributeError):
                    mutation = True  # malformed typed script fails conservatively
            else:
                mutation = False
        channel, deadline, request = self._new_channel(timeout)
        out, err = bytearray(), bytearray()
        try:
            channel.exec_command(script)
            # Drain both streams to avoid deadlock on a verbose command.
            while True:
                if request["expired"].is_set() or time.monotonic() >= deadline:
                    if mutation:
                        raise UnknownMutation("The device command timed out after a possible change. Inspect the tablet before any further setup; no automatic retry is allowed.")
                    raise DeviceError("The read-only device command timed out. Check the tablet connection.")
                if channel.recv_ready():
                    out.extend(channel.recv(65536))
                if channel.recv_stderr_ready():
                    err.extend(channel.recv_stderr(65536))
                if len(out) + len(err) > self.MAX_READ:
                    if mutation:
                        raise UnknownMutation("A device change exceeded its output limit without a final receipt. Inspect the tablet; the command will not be replayed.")
                    raise DeviceError("Device command produced too much output.")
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() >= deadline:
                    if mutation:
                        raise UnknownMutation("The device command timed out after a possible change. Inspect the tablet before any further setup; no automatic retry is allowed.")
                    raise DeviceError("The read-only device command timed out. Check the tablet connection.")
                time.sleep(0.01)
            code = channel.recv_exit_status()
            if code:
                # Remote stderr may contain secrets or document names. Keep
                # generic transport errors; callers can give operation context.
                raise DeviceError(f"Device command failed (exit {code}). Check tablet diagnostics.")
            return out.decode("utf-8", "replace")
        except (OSError, EOFError, paramiko.SSHException) as exc:
            if mutation:
                raise UnknownMutation("The SSH connection was lost during a possible change. Inspect the tablet before any further setup; the command will not be replayed.") from exc
            if request["expired"].is_set():
                raise DeviceError("The read-only device command timed out while waiting for acknowledgement.") from exc
            raise DeviceError("The SSH connection was interrupted. Inspect any in-progress action before retrying.") from exc
        finally:
            self._finish_channel(request,close=True)

    def stream(self, script: str, timeout: float = 30) -> RemoteStream:
        if not isinstance(script, str) or not script or "\x00" in script:
            raise DeviceError("Invalid device command.")
        channel, deadline, request = self._new_channel(timeout)
        completed = False
        try:
            channel.exec_command(script)
            if request["expired"].is_set() or time.monotonic() >= deadline:
                raise DeviceError("Starting the tablet input stream timed out before input was sent.")
            stream = RemoteStream(channel)
            with self._streams_lock:
                if channel.closed:
                    raise DeviceError("Starting the tablet input stream was cancelled before input was sent.")
                self._streams = [item for item in self._streams if not item.closed]
                self._streams.append(stream)
                completed = True
            channel.settimeout(timeout)
            return stream
        except (OSError, EOFError, paramiko.SSHException) as exc:
            raise DeviceError("Cannot start the tablet input stream.") from exc
        finally:
            self._finish_channel(request,close=not completed)

    start = stream

    def list_dir(self, path: str) -> list[str]:
        try:
            return self._files().listdir(validate_remote_path(path))
        except (OSError, EOFError, paramiko.SSHException, paramiko.SFTPError) as exc:
            raise DeviceError("Cannot read the tablet directory. Check its connection and permissions.") from exc

    def exists(self, path: str) -> bool:
        path = validate_remote_path(path)
        try:
            self._files().lstat(path)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            if exc.errno == 2:
                return False
            raise DeviceError("Cannot inspect the tablet path.") from exc
        except (EOFError, paramiko.SSHException, paramiko.SFTPError) as exc:
            raise DeviceError("Cannot inspect the tablet path because the SSH connection was interrupted.") from exc

    def read_bytes(self, path: str) -> bytes:
        path = validate_remote_path(path)
        try:
            with self._files().open(path, "rb") as source:
                data = source.read(self.MAX_READ + 1)
            if len(data) > self.MAX_READ:
                raise DeviceError("Remote file is too large for a metadata read; use streamed download.")
            return data
        except (OSError, EOFError, paramiko.SSHException, paramiko.SFTPError) as exc:
            raise DeviceError("Cannot read the tablet file. Check its connection and installation.") from exc

    def _check_write(self, path: str) -> str:
        path = validate_remote_path(path)
        if not any(path.startswith(root + "/") for root in self.WRITE_ROOTS):
            raise DeviceError("Writes are restricted to PaperDesk installation, volatile service, and guarded request paths.")
        # Reject existing symlink components; never follow a tablet symlink out
        # of the intended app-owned tree. Parent directories are made only by
        # explicit installer/request commands.
        files = self._files()
        parts = PurePosixPath(path).parts
        for index in range(2, len(parts) + 1):
            component = str(PurePosixPath(*parts[:index]))
            try:
                info = files.lstat(component)
            except OSError as exc:
                if exc.errno == 2:
                    continue
                raise DeviceError("Cannot verify the tablet write path.") from exc
            except (EOFError, paramiko.SSHException, paramiko.SFTPError) as exc:
                raise DeviceError("Cannot verify the tablet write path because SSH was interrupted.") from exc
            if stat.S_ISLNK(info.st_mode):
                raise DeviceError("Writing through a tablet symlink is refused.")
        return path

    def write_bytes(self, path: str, data: bytes, mode: int = 0o600) -> None:
        path = self._check_write(path)
        if not isinstance(data, bytes) or mode not in (0o600, 0o644, 0o700, 0o755):
            raise DeviceError("Invalid file data or permission mode.")
        temporary = path + "." + uuid.uuid4().hex + ".tmp"
        files = self._files()
        try:
            with files.open(temporary, "wx") as handle:
                handle.write(data)
                handle.flush()
            files.chmod(temporary, mode)
            try:
                files.posix_rename(temporary, path)
            except (AttributeError, OSError) as rename_error:
                if isinstance(rename_error, socket.timeout):
                    raise
                if isinstance(rename_error, OSError) and (
                        rename_error.errno not in (None, 38, 95) or
                        rename_error.errno is None and not any(word in str(rename_error).lower() for word in ("unsupported", "not supported"))):
                    raise
                # SFTP v3 rename cannot replace. Never delete the target to
                # fake replacement: losing the link at that point is unsafe.
                if self.exists(path):
                    raise DeviceError("Tablet lacks atomic replacement support; existing file preserved.")
                files.rename(temporary, path)
        except (EOFError, paramiko.SSHException, paramiko.SFTPError, socket.timeout) as exc:
            raise UnknownMutation("The tablet file transfer lost its receipt. A change may have happened; inspect the installation or native request before retrying.") from exc
        except OSError as exc:
            raise DeviceError("Tablet file transfer failed; inspect the installation before retrying.") from exc
        finally:
            try:
                files.remove(temporary)
            except (OSError, EOFError, paramiko.SSHException, paramiko.SFTPError):
                pass

    def download(self, remote: str, destination: Path) -> int:
        remote = validate_remote_path(remote)
        destination = Path(destination).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise DeviceError("A download cannot overwrite a symbolic link.")
        total = 0
        created = False
        try:
            # Exclusive creation also preserves originals on a name collision.
            with destination.open("xb") as target:
                created = True
                with self._files().open(remote, "rb") as source:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                        total += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            return total
        except FileExistsError as exc:
            raise DeviceError("That destination already exists. Choose a new filename to preserve the original.") from exc
        except (OSError, EOFError, paramiko.SSHException, paramiko.SFTPError) as exc:
            if created:
                destination.unlink(missing_ok=True)
            raise DeviceError("Download interrupted; no complete copy was saved.") from exc

    @contextlib.contextmanager
    def web_connection(self, timeout: float = 30) -> Iterator[http.client.HTTPConnection]:
        """Native USB web interface carried inside the trusted SSH session."""
        channel = None
        connection = None
        try:
            channel = self._session().open_channel("direct-tcpip", ("10.11.99.1", 80), ("127.0.0.1", 0), timeout=10)
            with self._streams_lock:
                self._web_channels.append(channel)
            channel.settimeout(timeout)
            connection = http.client.HTTPConnection("10.11.99.1", 80, timeout=timeout)
            connection.sock = channel
            yield connection
        except (OSError, EOFError, paramiko.SSHException, http.client.HTTPException) as exc:
            if self.config.transport_mode == "tailscale":
                raise DeviceError("Tablet web forwarding failed. Enable its native USB web interface and permit local port forwarding in the Tailscale SSH policy.") from exc
            raise DeviceError("Tablet web interface did not answer. Enable Settings → Storage → USB web interface.") from exc
        finally:
            if connection is not None:
                connection.close()
            elif channel is not None:
                channel.close()
            with self._streams_lock:
                if channel in self._web_channels:
                    self._web_channels.remove(channel)
