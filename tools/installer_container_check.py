"""Execute the real generic and advanced installer scripts in isolated Docker.

All device observations, systemctl, heartbeat and /proc mapping observations are
explicit fixtures. No firmware, ARM helper, Qt extension, USB, cloud, SSH or
physical device executes. Shell file operations, integrity checks, permissions,
ownership and rollback are real within the disposable container. No host mounts
or networking are requested. This complements the contract simulator; it is not
hardware or systemd validation.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import tempfile
import uuid

from paperdesk.installer import DeviceInstaller, InstallerError, UnknownInstallationOutcome
from paperdesk.simulator import APP_ROOT, LOADER_CONF, RECOVERY_UNIT, PINNED_FIRMWARE, PINNED_SHA256


class ContainerTransport:
    simulated = True

    def __init__(self, image="python:3.11-slim"):
        self.name = "paperdesk-check-" + uuid.uuid4().hex[:10]
        self.write_count = 0
        self.fail_write = None
        self.script_actions = []
        subprocess.run(["docker", "run", "-d", "--rm", "--pull", "never", "--network", "none", "--cpus", "1",
                        "--memory", "256m", "--pids-limit", "64", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges:true", "--name", self.name, image, "sleep", "900"],
                       check=True, capture_output=True)
        self.run("mkdir -p /mock/bin /home/root/.local/share/remarkable/xochitl /run/systemd/system/xochitl.service.d")
        self.fixture_write("/etc/os-release", b'IMG_VERSION="3.28.0.172"\n')
        self.fixture_write("/usr/bin/xochitl", b"Synthetic non-executable fixture; no proprietary firmware.\n")
        self.fixture_write("/dev/uinput", b"")
        self.fixture_write("/home/root/.local/share/remarkable/xochitl/original", b"preserve document")
        self.fixture_write("/run/systemd/system/xochitl.service.d/20-unrelated.conf", b"# preserve other apps\n")
        self.fixture_write("/mock/model", b"reMarkable Paper Pro\n")
        self.fixture_write("/mock/architecture", b"aarch64\n")
        self.fixture_write("/mock/pid", b"4242\n")
        self.fixture_write("/mock/bin/cat", b'''#!/bin/sh
case "${1:-}" in
  /sys/devices/soc0/machine) exec /bin/cat /mock/model;;
  *) exec /bin/cat "$@";;
esac
''', 0o755)
        self.fixture_write("/mock/bin/uname", b"#!/bin/sh\nexec /bin/cat /mock/architecture\n", 0o755)
        self.fixture_write("/mock/bin/pidof", b"#!/bin/sh\nexec /bin/cat /mock/pid\n", 0o755)
        self.fixture_write("/mock/bin/sha256sum", b'''#!/bin/sh
# Only xochitl discovery is stubbed. Every transferred payload hash is real.
if [ "${1:-}" = /usr/bin/xochitl ] && [ -f /mock/pinned-hash ]; then
  printf '%s  /usr/bin/xochitl\\n' "$(/bin/cat /mock/pinned-hash)"
else
  exec /usr/bin/sha256sum "$@"
fi
''', 0o755)
        self.fixture_write("/mock/bin/grep", b'''#!/bin/sh
# /proc cannot contain a real ARM Qt process; substitute only its fixture maps.
if [ "$#" = 3 ] && [ "$3" = /proc/4242/maps ]; then
  exec /usr/bin/grep "$1" "$2" /mock/maps
fi
exec /usr/bin/grep "$@"
''', 0o755)
        self.fixture_write("/mock/bin/systemctl", b'''#!/bin/sh
# Stub only; never execute xochitl, libraries, ARM helpers or a systemd daemon.
printf '%s\\n' "$*" >> /mock/systemctl.log
case "${1:-}" in
  restart|start)
    if [ -f /mock/fail-restart-once ]; then rm /mock/fail-restart-once; exit 1; fi
    if [ -f /mock/fail-restart-always ]; then exit 1; fi
    exec python /mock/service.py;;
  is-active) [ ! -f /mock/fail-active ]; exit $?;;
  *) exit 0;;
esac
''', 0o755)
        self.fixture_write("/mock/service.py", SERVICE_FIXTURE.encode())
        self.write_count = 0

    def run(self, script, timeout=30):
        for line in script.splitlines():
            if line.startswith("# paperdesk-operation: "):
                self.script_actions.append(json.loads(line.split(": ",1)[1])["action"])
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
        # Same strict contract as SFTP: installer must provision parent directories.
        script = f"set -eu\ncat > {shlex.quote(path)}\nchmod {mode:o} {shlex.quote(path)}"
        result = subprocess.run(["docker", "exec", "-i", self.name, "/bin/sh", "-c", script],
                                input=data, capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace"))

    def fixture_write(self, path, data, mode=0o600):
        self.run(f"mkdir -p {shlex.quote(str(PurePosixPath(path).parent))}")
        self.write_bytes(path,data,mode)

    def fixture_remove(self,path):
        self.run(f"test ! -f {shlex.quote(path)} || rm {shlex.quote(path)}")

    def read_bytes(self, path):
        return subprocess.check_output(["docker", "exec", self.name, "cat", path], timeout=15)

    def exists(self, path):
        return subprocess.run(["docker", "exec", self.name, "test", "-e", path],
                              capture_output=True, timeout=10).returncode == 0

    def close(self):
        subprocess.run(["docker", "stop", "-t", "1", self.name], capture_output=True, timeout=15)


SERVICE_FIXTURE = r'''
"""Synthetic service state only. Never load or execute deployed artifacts."""
import json,time
from pathlib import Path
root=Path("/home/root/.local/lib/paperdesk/current")
manifest=root/"manifest.json"
features=json.loads(manifest.read_text())["features"] if manifest.exists() else []
runtime=Path("/run/systemd/system/xochitl.service.d/99-paperdesk.conf")
loaded=features if runtime.exists() else []
Path("/mock/maps").write_text("".join("fixture mapping "+str(root/path)+"\n" for feature,path in (("direct-paste","xovi.so"),("art-display","lib/krita_viewport.so")) if feature in loaded))
if "direct-paste" in loaded:
    status={"protocol":2,"firmware":"3.28.0.172","xochitl_sha256":"b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021","pid":4242,"instance_id":"00000000-0000-4000-8000-000000000001","heartbeat_ms":int(time.time()*1000),"firmware_verified":True,"binary_verified":True}
    fault=Path("/mock/bad-heartbeat-once")
    if fault.exists():status["firmware_verified"]=False;fault.unlink()
    directory=Path("/home/root/.cache/remarkable-direct-paste");directory.mkdir(parents=True,exist_ok=True)
    (directory/"status.json").write_text(json.dumps(status,separators=(",",":")))
omit=Path("/mock/omit-viewport-once")
if omit.exists():Path("/mock/maps").write_text("");omit.unlink()
'''


def assert_preserved(remote):
    assert remote.read_bytes("/home/root/.local/share/remarkable/xochitl/original") == b"preserve document"
    assert remote.read_bytes("/run/systemd/system/xochitl.service.d/20-unrelated.conf") == b"# preserve other apps\n"


def expect_refusal(call,text):
    try:call()
    except InstallerError as error:
        assert text in str(error),str(error)
        return error
    raise AssertionError("Expected installer refusal: "+text)


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
            assert_preserved(remote)
            results.append("documents and unrelated configuration preserved")
            # Advanced installer *shell scripts* are real; all device/runtime
            # observations remain labeled fixtures rather than firmware execution.
            remote.fixture_write("/mock/pinned-hash",PINNED_SHA256.encode())
            assert installer.plan(["keyboard","touch","direct-paste","art-display"])["compatible"]
            assert installer.install(["keyboard","touch","direct-paste","art-display"])["ok"]
            runtime=remote.read_bytes(LOADER_CONF);unit=remote.read_bytes(RECOVERY_UNIT)
            assert b"current/xovi.so:" in runtime and b"current/lib/krita_viewport.so" in runtime
            assert b"OnFailure=\nOnFailure=paperdesk-recover.service" in runtime
            assert b"current/bin/recover-stock" in unit
            manifest=json.loads(remote.read_bytes(APP_ROOT+"/current/manifest.json"))
            for name,digest in manifest["files"].items():assert hashlib.sha256(remote.read_bytes(APP_ROOT+"/current/"+name)).hexdigest()==digest
            assert remote.run("stat -c '%a' "+APP_ROOT+"/current/bin/injector").strip()=="700"
            assert remote.run("stat -c '%a' "+APP_ROOT+"/current/extensions.d/direct_paste.so").strip()=="600"
            assert remote.run("find "+APP_ROOT+"/current/extensions.d -type f | wc -l").strip()=="1"
            results.append("advanced scripts deploy checksummed payloads, guarded loader and recovery unit")
            prior=remote.read_bytes(APP_ROOT+"/current/manifest.json")
            remote.fixture_write("/mock/fail-restart-once",b"fault")
            expect_refusal(lambda:installer.install(["direct-paste"]),"previous app installation was restored")
            assert remote.read_bytes(APP_ROOT+"/current/manifest.json")==prior
            assert remote.read_bytes(LOADER_CONF)==runtime and remote.read_bytes(RECOVERY_UNIT)==unit
            assert not remote.exists(APP_ROOT+"/install.lock")
            results.append("advanced service activation failure restores prior files and runtime")
            remote.fixture_write("/mock/bad-heartbeat-once",b"fault")
            expect_refusal(lambda:installer.install(["direct-paste"]),"exact firmware/build identity")
            assert remote.read_bytes(APP_ROOT+"/current/manifest.json")==prior
            assert remote.read_bytes(LOADER_CONF)==runtime
            results.append("advanced heartbeat identity failure rolls back the real promoted files")
            remote.fixture_write("/mock/omit-viewport-once",b"fault")
            expect_refusal(lambda:installer.install(["art-display"]),"previous app installation was restored")
            assert remote.read_bytes(APP_ROOT+"/current/manifest.json")==prior
            assert remote.read_bytes(LOADER_CONF)==runtime
            results.append("missing fixture process viewport mapping refuses completion and rolls back")
            # Recovery helper is shell-only; this invokes no shipped ARM payload.
            remote.run(APP_ROOT+"/current/bin/recover-stock")
            assert not remote.exists(LOADER_CONF)
            assert remote.exists(APP_ROOT+"/current/manifest.json")
            assert installer.install(["direct-paste","art-display"])["ok"]
            results.append("app-owned recovery script clears its loader and explicit restore reinstalls it")
            remote.fixture_remove(LOADER_CONF);remote.fixture_remove(RECOVERY_UNIT)
            assert installer.install(["direct-paste","art-display"])["ok"]
            results.append("reboot-like volatile loader loss can be explicitly restored")
            prior=remote.read_bytes(APP_ROOT+"/current/manifest.json");runtime=remote.read_bytes(LOADER_CONF)
            remote.fixture_write("/mock/fail-restart-once",b"fault")
            expect_refusal(installer.uninstall,"previous app installation was restored")
            assert remote.read_bytes(APP_ROOT+"/current/manifest.json")==prior
            assert remote.read_bytes(LOADER_CONF)==runtime
            results.append("partial advanced uninstall restores archived files and prior runtime")
            remote.fixture_write("/run/systemd/system/xochitl.service.d/foreign.conf",b'[Service]\nEnvironment="LD_PRELOAD=/other/loader.so"\n')
            before=remote.write_count
            expect_refusal(lambda:installer.install(["direct-paste"]),"before changes")
            assert remote.write_count==before
            assert remote.read_bytes(LOADER_CONF)==runtime
            remote.fixture_remove("/run/systemd/system/xochitl.service.d/foreign.conf")
            results.append("foreign preload configuration refuses advanced setup before transfer")
            remote.fixture_write(LOADER_CONF,b"# externally modified app-named config\n")
            expect_refusal(lambda:installer.install(["direct-paste"]),"modified")
            expect_refusal(installer.uninstall,"modified")
            assert remote.read_bytes(LOADER_CONF)==b"# externally modified app-named config\n"
            remote.fixture_write(LOADER_CONF,runtime)
            results.append("modified app runtime remains preserved on setup and uninstall refusal")
            assert installer.uninstall()["ok"]
            assert not remote.exists(APP_ROOT+"/current") and not remote.exists(LOADER_CONF) and not remote.exists(RECOVERY_UNIT)
            remote.fixture_write(RECOVERY_UNIT,b"# unowned runtime file\n")
            expect_refusal(lambda:installer.install(["keyboard"]),"before changes")
            assert remote.read_bytes(RECOVERY_UNIT)==b"# unowned runtime file\n"
            remote.fixture_remove(RECOVERY_UNIT)
            assert_preserved(remote)
            results.append("advanced uninstall clears only owned loader files; unowned runtime is preserved")
            # A bounded failure verifies installer unknown-outcome flags without
            # any network disconnect or host resource manipulation.
            remote.fixture_write("/mock/fail-restart-always",b"fault")
            error=expect_refusal(lambda:installer.install(["direct-paste"]),"Rollback could not be verified")
            assert isinstance(error,UnknownInstallationOutcome) and error.no_retry and error.outcome_unknown
            remote.fixture_remove("/mock/fail-restart-always")
            assert_preserved(remote)
            results.append("unverified activation restoration preserves no-retry outcome flags")
        print(json.dumps({"ok": True, "checks": results, "check_count":len(results), "network":"none", "host_mounts":False,
                          "scripts_executed":sorted(set(remote.script_actions)), "device_observations":"synthetic fixtures",
                          "arm_payloads_executed":False, "qt_executed":False, "hardware_validated": False}, indent=2))
    finally:
        remote.close()


if __name__ == "__main__":
    main()
