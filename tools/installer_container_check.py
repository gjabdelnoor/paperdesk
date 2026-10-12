"""Execute real setup shell scripts inside a disposable, network-isolated container.

The container stubs model discovery and systemctl; no Qt extension or real firmware
executes. This checks shell syntax, checksums, ownership, rollback and uninstall,
complementing (not replacing) the behavioral simulator and hardware validation.
"""
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import uuid

from paperdesk.installer import DeviceInstaller, InstallerError


class ContainerTransport:
    simulated = True

    def __init__(self, image="python:3.11-slim"):
        self.name = "paperdesk-check-" + uuid.uuid4().hex[:10]
        self.write_count = 0
        self.fail_write = None
        subprocess.run(["docker", "run", "-d", "--rm", "--network", "none", "--cpus", "1",
                        "--memory", "256m", "--name", self.name, image, "sleep", "900"],
                       check=True, capture_output=True)
        self.run("mkdir -p /mock/bin /home/root/.local/share/remarkable/xochitl /run/systemd/system/xochitl.service.d")
        self.write_bytes("/etc/os-release", b'IMG_VERSION="3.28.0.172"\n')
        self.write_bytes("/usr/bin/xochitl", b"Disposable shell fixture: no proprietary firmware.\n")
        self.write_bytes("/dev/uinput", b"")
        self.write_bytes("/home/root/.local/share/remarkable/xochitl/original", b"preserve document")
        self.write_bytes("/run/systemd/system/xochitl.service.d/20-unrelated.conf", b"# preserve other apps\n")
        self.write_bytes("/mock/bin/cat", b'''#!/bin/sh
case "${1:-}" in
  /sys/devices/soc0/machine) printf 'reMarkable Paper Pro\\n';;
  *) exec /bin/cat "$@";;
esac
''', 0o755)
        self.write_bytes("/mock/bin/uname", b"#!/bin/sh\nprintf 'aarch64\\n'\n", 0o755)
        self.write_bytes("/mock/bin/systemctl", b"#!/bin/sh\n# Service stub: this container does not run xochitl.\nexit 0\n", 0o755)
        self.write_count = 0

    def run(self, script, timeout=30):
        result = subprocess.run(["docker", "exec", self.name, "/bin/sh", "-c",
                                 "export PATH=/mock/bin:$PATH\n" + script],
                                capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace") or f"Shell failed: {result.returncode}")
        return result.stdout.decode()

    def write_bytes(self, path, data, mode=0o600):
        self.write_count += 1
        if self.fail_write == self.write_count:
            raise OSError("Injected container staging failure")
        script = f"mkdir -p {shlex.quote(str(Path(path).parent))}; cat > {shlex.quote(path)}; chmod {mode:o} {shlex.quote(path)}"
        result = subprocess.run(["docker", "exec", "-i", self.name, "/bin/sh", "-c", script],
                                input=data, capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace"))

    def read_bytes(self, path):
        return subprocess.check_output(["docker", "exec", self.name, "cat", path], timeout=15)

    def exists(self, path):
        return subprocess.run(["docker", "exec", self.name, "test", "-e", path],
                              capture_output=True, timeout=10).returncode == 0

    def close(self):
        subprocess.run(["docker", "stop", "-t", "1", self.name], capture_output=True, timeout=15)


def main():
    remote = ContainerTransport()
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix="paperdesk-container-state-") as state:
            installer = DeviceInstaller(remote, Path(state))
            plan = installer.plan(["keyboard", "touch"])
            assert plan["compatible"], plan
            assert not installer.plan(["direct-paste"])["compatible"]
            results.append("unknown xochitl digest refuses advanced setup")
            assert installer.install(["keyboard", "touch"])["ok"]
            prior = remote.read_bytes("/home/root/.local/lib/paperdesk/current/manifest.json")
            remote.fail_write = remote.write_count + 2
            try:
                installer.install(["keyboard"])
                raise AssertionError("Injected staging failure did not stop installation")
            except InstallerError:
                pass
            assert remote.read_bytes("/home/root/.local/lib/paperdesk/current/manifest.json") == prior
            results.append("failed staging restores previous manifest")
            remote.fail_write = None
            assert installer.install(["keyboard", "touch"])["ok"]
            results.append("repeat setup retains valid installation")
            assert installer.uninstall()["ok"]
            assert not remote.exists("/home/root/.local/lib/paperdesk/current")
            results.append("uninstall archives app files")
            assert remote.read_bytes("/home/root/.local/share/remarkable/xochitl/original") == b"preserve document"
            assert remote.read_bytes("/run/systemd/system/xochitl.service.d/20-unrelated.conf") == b"# preserve other apps\n"
            results.append("documents and unrelated configuration preserved")
        print(json.dumps({"ok": True, "checks": results, "network": "none", "hardware_validated": False}, indent=2))
    finally:
        remote.close()


if __name__ == "__main__":
    main()
