"""Transactional, device-probed installation of exclusively app-owned files.

No document-store writes, firmware changes, SSH configuration, or persistent
vendor service edits. Advanced integration is exact-build gated and loaded
from /run only. A reboot leaves stock xochitl active until explicit restore.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
import time
import uuid
from pathlib import Path, PurePosixPath

from .simulator import APP_ROOT, LOADER_CONF, RECOVERY_UNIT, PINNED_FIRMWARE, PINNED_SHA256, UnknownMutationError


class InstallerError(RuntimeError):
    outcome_unknown = False
    no_retry = False


class UnknownInstallationOutcome(InstallerError):
    """A transaction or its restoration lacks a conclusive receipt."""
    outcome_unknown = True
    no_retry = True


def _script(action: str, details: dict, body: str) -> str:
    return "#!/bin/sh\n# paperdesk-operation: " + json.dumps({"action": action, **details}, separators=(",", ":")) + "\nset -eu\n" + body + "\n"


def _guard(paths: list[str]) -> str:
    parents: set[str] = set()
    for value in paths:
        path = PurePosixPath(value)
        if not any(value == root or value.startswith(root + "/") for root in (APP_ROOT, "/run/systemd/system")):
            raise InstallerError("Installer attempted a path outside its ownership boundary.")
        while str(path) != "/":
            parents.add(path.as_posix()); path = path.parent
    lines = []
    for path in sorted(parents, key=lambda item: (item.count("/"), item)):
        quoted = shlex.quote(path)
        lines.append(f"if [ -L {quoted} ]; then echo 'Refusing symlink in installation path' >&2; exit 1; fi")
    return "\n".join(lines)


PROBE_SCRIPT = _script("probe", {}, r'''
model=$(cat /sys/devices/soc0/machine 2>/dev/null || true)
if [ -z "$model" ]; then model=$(tr -d '\000' </sys/firmware/devicetree/base/model 2>/dev/null || true); fi
arch=$(uname -m)
firmware=$(sed -n 's/^IMG_VERSION="\{0,1\}\([^" ]*\)"\{0,1\}$/\1/p' /etc/os-release | head -n 1)
digest=$(sha256sum /usr/bin/xochitl 2>/dev/null | cut -d ' ' -f1)
uinput=0; [ ! -e /dev/uinput ] || uinput=1
foreign=0
for config in /etc/systemd/system/xochitl.service.d/*.conf /usr/lib/systemd/system/xochitl.service.d/*.conf /lib/systemd/system/xochitl.service.d/*.conf /run/systemd/system/xochitl.service.d/*.conf; do
  [ -f "$config" ] || continue
  [ "$config" != /run/systemd/system/xochitl.service.d/99-paperdesk.conf ] || continue
  if grep -Eq 'LD_PRELOAD|XOVI_ROOT' "$config"; then foreign=1; fi
done
unsafe=0
for path in /home /home/root /home/root/.local /home/root/.local/lib /home/root/.local/lib/paperdesk /home/root/.local/lib/paperdesk/current /run /run/systemd /run/systemd/system /run/systemd/system/xochitl.service.d; do
  [ ! -L "$path" ] || unsafe=1
done
installed=0; [ ! -f /home/root/.local/lib/paperdesk/current/manifest.json ] || installed=1
owned=0; [ ! -f /run/systemd/system/xochitl.service.d/99-paperdesk.conf ] || owned=1
printf 'model=%s\narchitecture=%s\nfirmware=%s\nxochitl_sha256=%s\nuinput=%s\nforeign_loader=%s\nowned_loader=%s\ninstalled=%s\nunsafe_paths=%s\n' "$model" "$arch" "$firmware" "$digest" "$uinput" "$foreign" "$owned" "$installed" "$unsafe"
''')


class DeviceInstaller:
    FEATURE_IDS = {"keyboard", "touch", "direct-paste", "art-display"}

    def __init__(self, transport, state_dir: Path):
        self.transport = transport
        self.state_dir = Path(state_dir)
        self.resources = Path(__file__).resolve().parent / "vendor"

    def probe(self) -> dict:
        raw = self.transport.run(PROBE_SCRIPT)
        values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        observed = values.get("model", "")
        normalized = observed.lower()
        if "paper pro" in normalized or "ferrari" in normalized: model = "paper-pro"
        elif "remarkable 2" in normalized or "rm2" == normalized: model = "rm2"
        elif "remarkable 1" in normalized or normalized == "remarkable": model = "rm1"
        else: model = "unsupported"
        arch = values.get("architecture", "")
        if model == "paper-pro" and arch != "aarch64": model = "unsupported"
        if model in {"rm1", "rm2"} and arch not in {"armv7l", "armv6l", "arm"}: model = "unsupported"
        return {"model": model, "observed_model": observed, "architecture": arch, "firmware": values.get("firmware", ""),
                "xochitl_sha256": values.get("xochitl_sha256", ""), "uinput": values.get("uinput") == "1",
                "foreign_loader": values.get("foreign_loader") == "1", "installed": values.get("installed") == "1",
                "owned_loader": values.get("owned_loader") == "1", "unsafe_paths": values.get("unsafe_paths") == "1",
                "simulated": bool(getattr(self.transport, "simulated", False))}

    def plan(self, features: list[str]) -> dict:
        requested = list(dict.fromkeys(features))
        unknown = set(requested) - self.FEATURE_IDS
        if unknown: raise InstallerError("Unknown installer feature: " + ", ".join(sorted(unknown)))
        observed = self.probe(); supported = {}; reasons = {}
        generic = observed["model"] in {"paper-pro", "rm2", "rm1"} and bool(re.match(r"^3\.\d+\.\d+", observed["firmware"])) and observed["uinput"]
        exact = observed["model"] == "paper-pro" and observed["firmware"] == PINNED_FIRMWARE and observed["xochitl_sha256"] == PINNED_SHA256
        supported["keyboard"] = generic
        supported["touch"] = generic and observed["model"] == "paper-pro"
        supported["direct-paste"] = supported["art-display"] = exact and not observed["foreign_loader"]
        for feature in self.FEATURE_IDS:
            if supported[feature]: reasons[feature] = "Available for the observed model and architecture."
            elif feature == "keyboard": reasons[feature] = "Requires a recognized reMarkable model, firmware 3.x, its matching ARM architecture, and /dev/uinput."
            elif feature == "touch": reasons[feature] = "Touch geometry is validated only for Paper Pro on firmware 3.x with /dev/uinput."
            elif observed["foreign_loader"]: reasons[feature] = "Another xochitl preload/extension configuration is present. PaperDesk preserves it and refuses a conflicting loader."
            else: reasons[feature] = f"Requires Paper Pro ARM64 firmware {PINNED_FIRMWARE} and xochitl SHA256 {PINNED_SHA256}."
        if observed["unsafe_paths"]:
            supported = {feature: False for feature in supported}; reasons = {feature: "An installation path is a symlink. Resolve it manually before setup." for feature in reasons}
        denied = [{"feature": feature, "reason": reasons[feature]} for feature in requested if not supported[feature]]
        if observed["owned_loader"] and not observed["installed"]:
            denied.append({"feature": "loader", "reason": "An existing PaperDesk-named loader has no ownership manifest; it will not be replaced."})
        return {"device": observed, "features": requested, "supported": supported, "compatibility": reasons, "compatible": not denied,
                "unsupported": denied, "changes": [f"Stage checksum-verified files under {APP_ROOT}.", "Retain the previous app installation as a rollback backup."] +
                (["Install app-owned current-boot loader and crash recovery in /run; restart xochitl once and verify a fresh heartbeat."] if any(feature in requested for feature in ("direct-paste", "art-display")) else []),
                "reboot": "Helpers persist; advanced loaders expire at reboot. Run setup explicitly to restore them.",
                "document_safety": "Setup never writes to the tablet document store, firmware, SSH configuration, or vendor service files."}

    def _payload(self, features, device):
        files: dict[str, tuple[bytes, int]] = {}
        try: inventory=json.loads((self.resources/"tablet/artifacts.json").read_text(encoding="utf-8"))["sha256"]
        except (OSError,ValueError,KeyError) as exc:raise InstallerError("The packaged tablet artifact inventory is missing or invalid. Reinstall the application bundle.") from exc
        def add(name: str, resource: Path, mode=0o600):
            if not resource.is_file(): raise InstallerError(f"Required packaged artifact is missing: {resource.name}. Rebuild from the included source before setup.")
            data = resource.read_bytes()
            key=resource.relative_to(self.resources).as_posix()
            if inventory.get(key)!=hashlib.sha256(data).hexdigest():raise InstallerError(f"Packaged tablet helper checksum failed: {name}. Inspect rebuild changes before updating the artifact inventory.")
            if name.endswith((".so", "injector", "pen-bridge")):
                expected = 183 if device["architecture"] == "aarch64" else 40
                if not data.startswith(b"\x7fELF") or int.from_bytes(data[18:20], "little") != expected:
                    raise InstallerError(f"Packaged helper has the wrong ELF architecture: {name}.")
            files[name] = (data, mode)
        arch = "aarch64" if device["architecture"] == "aarch64" else "armv7l"
        if "keyboard" in features: add("bin/injector", self.resources / "tablet/bin" / f"injector-{arch}", 0o700)
        if "touch" in features: add("bin/touch-injector", self.resources / "tablet/bin/touch-injector-aarch64", 0o700)
        advanced = "direct-paste" in features or "art-display" in features
        preloads = []
        if "direct-paste" in features:
            add("xovi.so", self.resources / "tablet/direct-paste/xovi.so")
            add("extensions.d/direct_paste.so", self.resources / "tablet/direct-paste/direct_paste.so")
            preloads.append(APP_ROOT + "/current/xovi.so")
        if "art-display" in features:
            add("bin/pen-bridge", self.resources / "art/pen-bridge", 0o700)
            add("lib/krita_viewport.so", self.resources / "art/viewport/prebuilt/krita_viewport.so")
            preloads.append(APP_ROOT + "/current/lib/krita_viewport.so")
        runtime = {}
        if advanced:
            config = "[Unit]\nOnFailure=\nOnFailure=paperdesk-recover.service\nJobTimeoutSec=60s\n[Service]\nRestart=no\nRestartMode=direct\nEnvironment=\"MALLOC_ARENA_MAX=8\"\nEnvironment=\"LD_PRELOAD=" + ":".join(preloads) + "\"\n"
            if "direct-paste" in features: config += f'Environment="XOVI_ROOT={APP_ROOT}/current"\n'
            recovery = f'''#!/bin/sh
set -eu
# Remove only PaperDesk's volatile overrides; original vendor configuration remains.
rm -f {LOADER_CONF}
systemctl daemon-reload
systemctl reset-failed xochitl.service || true
systemctl start xochitl.service
'''
            unit = f"[Unit]\nDescription=Recover stock xochitl after PaperDesk loader failure\n[Service]\nType=oneshot\nExecStart={APP_ROOT}/current/bin/recover-stock\n"
            files["runtime/99-paperdesk.conf"] = (config.encode(), 0o600)
            files["runtime/paperdesk-recover.service"] = (unit.encode(), 0o600)
            files["bin/recover-stock"] = (recovery.encode(), 0o700)
            runtime = {LOADER_CONF: "runtime/99-paperdesk.conf", RECOVERY_UNIT: "runtime/paperdesk-recover.service"}
        return files, runtime

    def _verify_manifest(self):
        manifest_path = APP_ROOT + "/current/manifest.json"
        if not self.transport.exists(manifest_path): return None
        try: manifest = json.loads(self.transport.read_bytes(manifest_path))
        except (ValueError, UnicodeError) as exc: raise InstallerError("Existing PaperDesk manifest is invalid. Restore it from a backup before modifying setup.") from exc
        if manifest.get("owner") != "paperdesk" or manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict):
            raise InstallerError("Existing app directory has no valid PaperDesk ownership manifest.")
        for name, digest in manifest["files"].items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.\-/]+",name) or name.startswith("/") or ".." in PurePosixPath(name).parts:
                raise InstallerError("Existing manifest contains an unsafe path.")
            if not re.fullmatch(r"[0-9a-f]{64}",str(digest)): raise InstallerError("Existing manifest has an invalid checksum.")
            if hashlib.sha256(self.transport.read_bytes(APP_ROOT+"/current/"+name)).hexdigest() != digest:
                raise InstallerError(f"Existing app file was modified: {name}. Preserve it manually before replacing setup.")
        for runtime, name in ((LOADER_CONF,"runtime/99-paperdesk.conf"),(RECOVERY_UNIT,"runtime/paperdesk-recover.service")):
            if self.transport.exists(runtime):
                expected=manifest["files"].get(name)
                if expected is None or hashlib.sha256(self.transport.read_bytes(runtime)).hexdigest()!=expected:
                    raise InstallerError("Existing app-named runtime configuration was modified; it is preserved and setup is refused.")
        return manifest

    def install(self, features: list[str]) -> dict:
        plan = self.plan(features)
        if not plan["compatible"]: raise InstallerError("Setup refused before changes: " + "; ".join(item["reason"] for item in plan["unsupported"]))
        features=plan["features"]
        if not features: raise InstallerError("Select at least one feature before setup.")
        previous=self._verify_manifest()
        if previous is None and self.transport.exists(APP_ROOT+"/current"):
            raise InstallerError("Existing app directory has no ownership manifest and is preserved.")
        files,runtime=self._payload(features,plan["device"])
        token=uuid.uuid4().hex;stage=APP_ROOT+"/stage-"+token;backup=APP_ROOT+"/backup-"+token
        previous_runtime={path:base64.b64encode(self.transport.read_bytes(path)).decode() for path in (LOADER_CONF,RECOVERY_UNIT) if self.transport.exists(path)}
        sums={name:hashlib.sha256(data).hexdigest() for name,(data,_) in files.items()}
        manifest={"schema":1,"owner":"paperdesk","features":features,"device":plan["device"],"files":sums,"transaction":token,"installed_at":int(time.time())}
        files["manifest.json"]=(json.dumps(manifest,indent=2).encode(),0o600)
        activated=False;prepared=False
        try:
            self.transport.run(_script("guard",{"paths":[stage,backup,LOADER_CONF,RECOVERY_UNIT]},_guard([stage,backup,LOADER_CONF,RECOVERY_UNIT])))
            prepared=True
            prepare=_guard([stage,APP_ROOT+"/install.lock"])+f"\nmkdir -p -m 700 {shlex.quote(APP_ROOT)}\nif ! mkdir -m 700 {APP_ROOT}/install.lock; then echo 'Another setup transaction is active or interrupted; inspect install.lock before retrying' >&2; exit 1; fi\nprintf '%s' {shlex.quote(token)} >{APP_ROOT}/install.lock/owner\nmkdir -m 700 {shlex.quote(stage)}"
            self.transport.run(_script("prepare",{"stage":stage,"token":token},prepare))
            for name,(data,mode) in files.items():
                target=stage+"/"+name
                parent=str(PurePosixPath(target).parent)
                self.transport.run(_script("guard",{"paths":[target],"mkdir":parent},_guard([target])+f"\nmkdir -p -m 700 {shlex.quote(parent)}"))
                self.transport.write_bytes(target,data,mode=mode)
            all_sums={name:hashlib.sha256(data).hexdigest() for name,(data,_) in files.items()}
            checks="\n".join(f"printf '%s  %s\\n' {shlex.quote(digest)} {shlex.quote(stage+'/'+name)} | sha256sum -c - >/dev/null" for name,digest in all_sums.items())
            self.transport.run(_script("verify",{"stage":stage,"files":all_sums},_guard([stage])+"\n"+checks))
            # Probe again immediately before activation: no advanced writes on drift.
            live=self.probe()
            for key in ("model","architecture","firmware","xochitl_sha256","foreign_loader","unsafe_paths"):
                if live[key]!=plan["device"][key]: raise InstallerError("Device compatibility changed during staging; activation was refused.")
            body=_guard([stage,backup,APP_ROOT+"/current",APP_ROOT+"/install.lock",LOADER_CONF,RECOVERY_UNIT])+f"\n[ \"$(cat {APP_ROOT}/install.lock/owner)\" = {shlex.quote(token)} ]\nif [ -d {APP_ROOT}/current ]; then mv {APP_ROOT}/current {shlex.quote(backup)}; fi\nmv {shlex.quote(stage)} {APP_ROOT}/current\n"
            for path,source in runtime.items():
                body+=f"mkdir -p -m 755 {shlex.quote(str(PurePosixPath(path).parent))}\ncp {shlex.quote(APP_ROOT+'/current/'+source)} {shlex.quote(path)}\n"
            if runtime: body+="systemctl daemon-reload\nsystemctl reset-failed xochitl.service || true\nsystemctl restart xochitl.service\nsystemctl is-active --quiet xochitl.service\n"
            elif previous_runtime: body+=f"rm -f {LOADER_CONF} {RECOVERY_UNIT}\nsystemctl daemon-reload\nsystemctl restart xochitl.service\n"
            activated=True
            self.transport.run(_script("activate",{"stage":stage,"backup":backup,"runtime":runtime},body),timeout=90)
            if "direct-paste" in features: self._heartbeat()
            if "art-display" in features: self._art_loaded()
            self.state_dir.mkdir(parents=True,exist_ok=True)
            (self.state_dir/"last-install.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
            self._release(token)
            return {"ok":True,"message":"Setup completed and verified."+(" Advanced features need explicit restore after reboot." if runtime else ""),"features":features,"manifest":APP_ROOT+"/current/manifest.json","backup":backup if previous else "","simulated":plan["device"]["simulated"]}
        except Exception as failure:
            if prepared:
                try: self._rollback(stage,backup,activated,previous is not None,previous_runtime,token)
                except Exception as rollback_error:
                    raise UnknownInstallationOutcome(f"Setup failed: {failure}. Rollback could not be verified: {rollback_error}. App backups remain at {backup}; do not rerun automatically.") from failure
            if isinstance(failure, UnknownMutationError) or getattr(failure,"outcome_unknown",False) or getattr(failure,"no_retry",False):
                raise UnknownInstallationOutcome(f"Setup was interrupted with an uncertain activation outcome: {failure}. Restoration was requested; inspect device diagnostics before any further setup.") from failure
            raise InstallerError(f"Setup failed and the previous app installation was restored: {failure}") from failure

    def _heartbeat(self):
        script=_script("heartbeat",{},r'''
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  path=/home/root/.cache/remarkable-direct-paste/status.json
  if [ -f "$path" ]; then
    pid=$(pidof xochitl || true)
    if [ -n "$pid" ] && grep -Eq '"pid"[[:space:]]*:[[:space:]]*'"$pid"'[,}]' "$path"; then cat "$path"; exit 0; fi
  fi
  sleep 1
done
echo 'No live PaperDesk extension heartbeat; restoring stock xochitl' >&2
exit 1
''')
        try: status=json.loads(self.transport.run(script,timeout=15))
        except ValueError as exc: raise InstallerError("Extension heartbeat is not valid JSON.") from exc
        if status.get("protocol")!=2 or status.get("firmware")!=PINNED_FIRMWARE or status.get("xochitl_sha256")!=PINNED_SHA256 or not status.get("firmware_verified") or not status.get("binary_verified"):
            raise InstallerError("Activated extension did not verify its exact firmware/build identity.")
        if abs(int(time.time()*1000)-int(status.get("heartbeat_ms",0)))>5000:
            raise InstallerError("Activated extension heartbeat is stale.")

    def _art_loaded(self):
        # Viewport publishes its detailed heartbeat when an art session starts.
        # During setup verify the exact installed library is mapped in xochitl.
        body = r'''
pid=$(pidof xochitl || true)
case "$pid" in ''|*[!0-9]*) echo 'No single live xochitl process' >&2; exit 1;; esac
grep -F '/home/root/.local/lib/paperdesk/current/lib/krita_viewport.so' "/proc/$pid/maps" >/dev/null
printf 'viewport-loaded\n'
'''
        self.transport.run(_script("art-loaded",{},body),timeout=15)

    def _release(self,token):
        body=_guard([APP_ROOT+"/install.lock"])+f"\nif [ -f {APP_ROOT}/install.lock/owner ] && [ \"$(cat {APP_ROOT}/install.lock/owner)\" = {shlex.quote(token)} ]; then rm -f {APP_ROOT}/install.lock/owner; rmdir {APP_ROOT}/install.lock; fi"
        self.transport.run(_script("release",{"token":token},body))

    def _rollback(self,stage,backup,activated,previous_current,previous_runtime,token):
        body=_guard([stage,backup,APP_ROOT+"/current",APP_ROOT+"/install.lock",LOADER_CONF,RECOVERY_UNIT])+f"\nif [ ! -f {APP_ROOT}/install.lock/owner ] || [ \"$(cat {APP_ROOT}/install.lock/owner)\" != {shlex.quote(token)} ]; then exit 0; fi\nrm -f {LOADER_CONF} {RECOVERY_UNIT}\n"
        body+=f"if [ -d {shlex.quote(backup)} ]; then rm -rf {APP_ROOT}/current; mv {shlex.quote(backup)} {APP_ROOT}/current; "
        if activated and not previous_current: body+=f"elif [ -d {APP_ROOT}/current ]; then rm -rf {APP_ROOT}/current; "
        body+=f"fi\nrm -rf {shlex.quote(stage)}\n"
        for path,encoded in previous_runtime.items():
            body+=f"mkdir -p {shlex.quote(str(PurePosixPath(path).parent))}\nprintf '%s' {shlex.quote(encoded)} | base64 -d >{shlex.quote(path)}\n"
        if activated: body+="systemctl daemon-reload\nsystemctl reset-failed xochitl.service || true\nsystemctl restart xochitl.service\nsystemctl is-active --quiet xochitl.service\n"
        self.transport.run(_script("rollback",{"stage":stage,"backup":backup,"activated":activated,"previous_current":previous_current,"previous_runtime":previous_runtime,"token":token},body),timeout=90)
        self._release(token)

    def uninstall(self):
        previous=self._verify_manifest()
        if previous is None:
            if self.transport.exists(LOADER_CONF) or self.transport.exists(RECOVERY_UNIT): raise InstallerError("Unowned app-named loader files are preserved. Inspect them manually.")
            return {"ok":True,"message":"PaperDesk helpers are already uninstalled."}
        archive=APP_ROOT+"/uninstalled-"+uuid.uuid4().hex
        previous_runtime={path:base64.b64encode(self.transport.read_bytes(path)).decode() for path in (LOADER_CONF,RECOVERY_UNIT) if self.transport.exists(path)}
        body=_guard([archive,APP_ROOT+"/current",LOADER_CONF,RECOVERY_UNIT])+f"\nif [ -e {APP_ROOT}/install.lock ]; then echo 'An installation transaction is active or interrupted; inspect install.lock before uninstalling' >&2; exit 1; fi\nmv {APP_ROOT}/current {shlex.quote(archive)}\nrm -f {LOADER_CONF} {RECOVERY_UNIT}\nsystemctl daemon-reload\nsystemctl reset-failed xochitl.service || true\nsystemctl restart xochitl.service\nsystemctl is-active --quiet xochitl.service\n"
        try:self.transport.run(_script("uninstall",{"archive":archive},body),timeout=90)
        except Exception as failure:
            restore=_guard([archive,APP_ROOT+"/current",LOADER_CONF,RECOVERY_UNIT])+f"\nif [ ! -d {shlex.quote(archive)} ]; then exit 0; fi\nif [ ! -e {APP_ROOT}/current ]; then mv {shlex.quote(archive)} {APP_ROOT}/current; else echo 'Current app changed during uninstall; manual restoration required' >&2; exit 1; fi\n"
            for path,encoded in previous_runtime.items():
                restore+=f"mkdir -p {shlex.quote(str(PurePosixPath(path).parent))}\nprintf '%s' {shlex.quote(encoded)} | base64 -d >{shlex.quote(path)}\n"
            restore+="systemctl daemon-reload\nsystemctl reset-failed xochitl.service || true\nsystemctl restart xochitl.service\n"
            try:self.transport.run(_script("restore-uninstall",{"archive":archive,"previous_runtime":previous_runtime},restore),timeout=90)
            except Exception as rollback_error:raise UnknownInstallationOutcome(f"Uninstall failed: {failure}. Restoration could not be verified: {rollback_error}. Do not rerun automatically.") from failure
            if getattr(failure,"outcome_unknown",False) or getattr(failure,"no_retry",False):raise UnknownInstallationOutcome(f"Uninstall outcome was uncertain: {failure}. Inspect device diagnostics before any further changes.") from failure
            raise InstallerError(f"Uninstall failed and the previous app installation was restored: {failure}") from failure
        return {"ok":True,"message":"PaperDesk helpers disabled; stock tablet service restored. Documents and other configuration are untouched.","archive":archive}
