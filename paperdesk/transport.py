"""Authenticated SSH transport with explicit host trust and bounded transfers.

There is no auto-accept policy, external SSH process, or background tunnel. The
tablet's USB web interface is reached by a direct-tcpip channel through the
already authenticated connection, including when the SSH host is wireless.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import json
import os
import re
import socket
import stat
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


class UnknownMutation(DeviceError):
    """A remote write may have happened; do not replay it automatically."""
    no_retry = True
    outcome_unknown = True


def state_directory(config: DeviceConfig) -> Path:
    return Path(config.state_dir) if config.state_dir else user_state_path("PaperDesk", "PaperDesk")


def validate_config(config: DeviceConfig) -> None:
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
        connection = socket.create_connection((config.host, config.port), timeout=timeout)
        session = paramiko.Transport(connection)
        session.banner_timeout = timeout
        session.start_client(timeout=timeout)
        key = session.get_remote_server_key()
        return {"host": config.host, "port": config.port, "fingerprint": fingerprint(key), "key_type": key.get_name()}
    except (OSError, paramiko.SSHException) as exc:
        raise DeviceError("Cannot observe the tablet SSH key. Check its address, developer mode, and connection.") from exc
    finally:
        if session is not None:
            session.close()
        elif connection is not None:
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
        except (OSError, EOFError):
            pass
        self.stdin.close()
        self.stdout.close()
        self.channel.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class SSHTransport:
    """Device transport. Construction never contacts a device."""

    MAX_READ = 64 * 1024 * 1024
    WRITE_ROOTS = ("/home/root/.local/lib/paperdesk", "/run/systemd/system",
                   "/home/root/.cache/remarkable-direct-paste", "/tmp/paperdesk-viewport")

    def __init__(self, config: DeviceConfig):
        validate_config(config)
        self.config = config
        self.client = None
        self._sftp = None
        self._streams: list[RemoteStream] = []

    @property
    def connected(self) -> bool:
        if self.client is None:
            return False
        session = self.client.get_transport()
        return bool(session and session.is_active() and session.is_authenticated())

    def connect(self) -> None:
        expected = _load_trust(self.config).get(_host_id(self.config))
        if not expected:
            raise HostTrustRequired("This tablet is not paired. Observe its SSH fingerprint and confirm trust before connecting.")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_FingerprintPolicy(expected))
        try:
            client.connect(hostname=self.config.host, port=self.config.port,
                           username=self.config.username,
                           password=self.config.password or None,
                           key_filename=self.config.key_path or None,
                           timeout=10, banner_timeout=10, auth_timeout=15,
                           look_for_keys=not bool(self.config.key_path or self.config.password),
                           allow_agent=not bool(self.config.key_path or self.config.password))
        except HostTrustRequired:
            client.close()
            raise
        except paramiko.AuthenticationException as exc:
            client.close()
            raise DeviceError("SSH authentication failed. Use the developer-mode password or the correct private key.") from exc
        except (paramiko.SSHException, OSError, ValueError) as exc:
            client.close()
            raise DeviceError("Cannot connect over SSH. Check developer mode, the tablet address, and the cable or network.") from exc
        self.client = client

    def close(self) -> None:
        for stream in self._streams:
            stream.close()
        self._streams.clear()
        if self._sftp is not None:
            self._sftp.close()
            self._sftp = None
        if self.client is not None:
            self.client.close()
            self.client = None

    disconnect = close

    def _session(self):
        if not self.connected:
            raise DeviceError("Connect to a paired tablet first.")
        return self.client.get_transport()

    def _files(self):
        self._session()
        if self._sftp is None:
            self._sftp = self.client.open_sftp()
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
        session = self._session()
        channel = session.open_session(timeout=min(timeout, 15))
        channel.settimeout(timeout)
        out, err = bytearray(), bytearray()
        try:
            channel.exec_command(script)
            deadline = time.monotonic() + timeout
            # Drain both streams to avoid deadlock on a verbose command.
            while True:
                if channel.recv_ready():
                    out.extend(channel.recv(65536))
                if channel.recv_stderr_ready():
                    err.extend(channel.recv_stderr(65536))
                if len(out) + len(err) > self.MAX_READ:
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
            raise DeviceError("The SSH connection was interrupted. Inspect any in-progress action before retrying.") from exc
        finally:
            channel.close()

    def stream(self, script: str, timeout: float = 30) -> RemoteStream:
        if not isinstance(script, str) or not script or "\x00" in script:
            raise DeviceError("Invalid device command.")
        channel = self._session().open_session(timeout=10)
        channel.settimeout(timeout)
        try:
            channel.exec_command(script)
            stream = RemoteStream(channel)
            self._streams = [item for item in self._streams if not item.closed]
            self._streams.append(stream)
            return stream
        except (OSError, EOFError, paramiko.SSHException) as exc:
            channel.close()
            raise DeviceError("Cannot start the tablet input stream.") from exc

    start = stream

    def list_dir(self, path: str) -> list[str]:
        try:
            return self._files().listdir(validate_remote_path(path))
        except OSError as exc:
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

    def read_bytes(self, path: str) -> bytes:
        path = validate_remote_path(path)
        try:
            with self._files().open(path, "rb") as source:
                data = source.read(self.MAX_READ + 1)
            if len(data) > self.MAX_READ:
                raise DeviceError("Remote file is too large for a metadata read; use streamed download.")
            return data
        except OSError as exc:
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
            except (AttributeError, OSError):
                # SFTP v3 rename cannot replace. Never delete the target to
                # fake replacement: losing the link at that point is unsafe.
                if self.exists(path):
                    raise DeviceError("Tablet lacks atomic replacement support; existing file preserved.")
                files.rename(temporary, path)
        except OSError as exc:
            raise DeviceError("Tablet file transfer failed; inspect the installation before retrying.") from exc
        finally:
            try:
                files.remove(temporary)
            except OSError:
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
        except OSError as exc:
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
            channel.settimeout(timeout)
            connection = http.client.HTTPConnection("10.11.99.1", 80, timeout=timeout)
            connection.sock = channel
            yield connection
        except (OSError, EOFError, paramiko.SSHException, http.client.HTTPException) as exc:
            raise DeviceError("Tablet web interface did not answer. Enable Settings → Storage → USB web interface.") from exc
        finally:
            if connection is not None:
                connection.close()
            elif channel is not None:
                channel.close()
