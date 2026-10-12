"""Isolated behavioral tablet simulator. No firmware emulation or networking.

The installer sends the *same* shell scripts to this transport and real SSH.
Only their typed ``paperdesk-operation`` descriptions are interpreted here;
shell text is never executed on the host. All tablet paths stay inside root.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import time
import uuid
import zlib
import zipfile
from pathlib import Path, PurePosixPath

PINNED_FIRMWARE = "3.28.0.172"
PINNED_SHA256 = "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021"
APP_ROOT = "/home/root/.local/lib/paperdesk"
LOADER_CONF = "/run/systemd/system/xochitl.service.d/99-paperdesk.conf"
RECOVERY_UNIT = "/run/systemd/system/paperdesk-recover.service"


class SimulationError(RuntimeError):
    pass


class UnknownMutationError(SimulationError):
    """Mutation might have completed. It must not be repeated automatically."""
    outcome_unknown = True
    no_retry = True


def _pdf(text: str) -> bytes:
    """Small valid fixture PDF, generated exclusively from synthetic text."""
    safe = text.encode("ascii", "replace").decode().replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    lines = safe.splitlines()[:45] or [""]
    content = "BT /F1 12 Tf 40 800 Td " + " ".join(f"({line[:90]}) Tj 0 -16 Td" for line in lines) + " ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               f"<< /Length {len(content.encode())} >>\nstream\n{content}\nendstream".encode()]
    result = bytearray(b"%PDF-1.4\n% synthetic PaperDesk fixture\n"); offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result)); result.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(result); result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]: result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(result)


class TabletSimulator:
    simulated = True

    def __init__(self, root: Path | None = None, model: str = "paper-pro", firmware: str = PINNED_FIRMWARE):
        aliases = {"paper-pro": "paper-pro", "rm2": "rm2", "remarkable-2": "rm2", "rm1": "rm1", "remarkable-1": "rm1"}
        if model not in aliases: raise ValueError("Simulation model must be paper-pro, rm2, or rm1.")
        self.root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="paperdesk-tablet-"))
        if self.root.is_symlink(): raise SimulationError("Simulation root cannot be a symlink.")
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        marker = self.root / ".paperdesk-simulator"
        if any(self.root.iterdir()) and (marker.is_symlink() or not marker.is_file()):
            raise SimulationError("Choose an empty folder or an existing PaperDesk simulation; existing files are protected.")
        if not marker.exists(): marker.write_text("PaperDesk behavioral simulator v1\n", encoding="utf-8")
        self.faults: set[str] = set()
        self.operations: list[dict] = []
        self._write_count = 0
        self.connected = True
        state = self._path("/.simulator/state.json")
        if state.exists(): self._state = json.loads(state.read_text(encoding="utf-8"))
        else:
            self._state = {"model": aliases[model], "firmware": firmware, "architecture": "aarch64" if aliases[model] == "paper-pro" else "armv7l",
                           "xochitl_sha256": PINNED_SHA256 if firmware == PINNED_FIRMWARE else "0"*64,
                           "documents": [], "active_id": "", "page": 1, "clipboard": "", "boot_count": 0, "pid": 1001}
            for remote in ("/run", "/dev", "/home/root/.local", "/etc", "/.simulator/documents"):
                self._path(remote).mkdir(parents=True, exist_ok=True)
            self._path("/dev/uinput").touch()
            self._path("/etc/os-release").write_text(f'IMG_VERSION="{firmware}"\n', encoding="utf-8")
            for name, text, folder in (("Welcome notebook", "Simulation: your real tablet is untouched.", "Inbox"), ("Sample reading", "PaperDesk synthetic reading fixture.", "Archive")):
                ident = str(uuid.uuid4())
                kind = "notebook" if "notebook" in name else "pdf"
                self._state["documents"].append({"id": ident, "name": name, "type": kind, "pages": 1, "modified": time.time(), "folder": folder, "text": text, "images": [], "templates": ["blank"]})
                self._path(f"/.simulator/documents/{ident}.pdf").write_bytes(_pdf(text))
            self._state["active_id"] = self._state["documents"][0]["id"]
            self._save()

    def _path(self, remote: str) -> Path:
        if not isinstance(remote, str) or "\x00" in remote: raise SimulationError("Invalid simulated tablet path.")
        path = PurePosixPath(remote)
        if not path.is_absolute() or ".." in path.parts: raise SimulationError("Tablet path must be absolute and cannot contain traversal.")
        target = self.root.joinpath(*path.parts[1:])
        cursor = self.root
        for part in path.parts[1:]:
            cursor /= part
            if cursor.is_symlink(): raise SimulationError("Symlink paths are refused inside the simulation.")
        try: target.resolve(strict=False).relative_to(self.root)
        except ValueError as exc: raise SimulationError("Tablet path escapes simulation root.") from exc
        return target

    def _online(self):
        if not self.connected or "offline" in self.faults: raise SimulationError("Simulated tablet is offline. Reconnect the simulation.")

    def _save(self):
        path = self._path("/.simulator/state.json"); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    def _record(self, action, **details):
        event = {"action": action, "time": time.time(), **details}; self.operations.append(event)
        path = self._path("/.simulator/operations.jsonl")
        with path.open("a", encoding="utf-8") as stream: stream.write(json.dumps(event) + "\n")

    def set_fault(self, name: str, enabled: bool = True):
        allowed = {"offline", "upload-unknown", "wrong-firmware", "wrong-hash", "duplicate-loader", "write-failure", "activation-failure", "uninstall-failure", "manifest-corrupt", "restore-failure", "reboot-during-install"}
        if name not in allowed: raise ValueError(f"Unknown simulator fault: {name}")
        if enabled: self.faults.add(name)
        else: self.faults.discard(name)

    def exists(self, path: str) -> bool:
        self._online(); return self._path(path).exists()

    def read_bytes(self, path: str) -> bytes:
        self._online(); return self._path(path).read_bytes()

    def write_bytes(self, path: str, data: bytes, mode: int = 0o600):
        self._online(); self._write_count += 1
        if "write-failure" in self.faults and self._write_count % 3 == 0: raise SimulationError("Injected staging write failure.")
        target = self._path(path); target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data); target.chmod(mode)
        self._record("write", path=path, bytes=len(data))

    def run(self, script: str, timeout: float = 30) -> str:
        self._online()
        match = re.search(r"^# paperdesk-operation: (\{.*\})$", script, re.MULTILINE)
        if not match: raise SimulationError("The simulator refuses arbitrary shell commands.")
        op = json.loads(match.group(1)); action = op.get("action")
        self._record("script", operation=action)
        base = APP_ROOT
        if action == "probe":
            model = {"paper-pro": "reMarkable Paper Pro", "rm2": "reMarkable 2.0", "rm1": "reMarkable 1.0"}[self._state["model"]]
            foreign = "duplicate-loader" in self.faults
            for remote in ("/run/systemd/system/xochitl.service.d", "/etc/systemd/system/xochitl.service.d", "/usr/lib/systemd/system/xochitl.service.d", "/lib/systemd/system/xochitl.service.d"):
                run_dir = self._path(remote)
                if not run_dir.exists():continue
                for path in run_dir.iterdir():
                    # Do not follow symlinks while inspecting foreign files.
                    if path.is_symlink(): foreign = True; continue
                    if (remote!="/run/systemd/system/xochitl.service.d" or path.name!="99-paperdesk.conf") and path.is_file() and any(word in path.read_text(errors="replace") for word in ("LD_PRELOAD", "XOVI_ROOT")): foreign = True
            return "\n".join(f"{key}={value}" for key, value in {
                "model": model, "architecture": self._state["architecture"], "firmware": "9.99.0" if "wrong-firmware" in self.faults else self._state["firmware"],
                "xochitl_sha256": "0"*64 if "wrong-hash" in self.faults else self._state["xochitl_sha256"], "uinput": int(self._path("/dev/uinput").exists()),
                "foreign_loader": int(foreign), "owned_loader": int(self._path(LOADER_CONF).exists()), "installed": int(self._path(base+"/current/manifest.json").exists()),
                "unsafe_paths": 0}.items()) + "\n"
        if action == "guard":
            for path in op["paths"]: self._path(path)
            if op.get("mkdir"):self._path(op["mkdir"]).mkdir(parents=True,mode=0o700,exist_ok=True)
            return "guarded\n"
        if action == "prepare":
            lock=self._path(base+"/install.lock")
            try:lock.mkdir(mode=0o700,parents=True)
            except FileExistsError as exc:raise SimulationError("Another setup transaction is active or interrupted; inspect install.lock before retrying.") from exc
            self._path(base+"/install.lock/owner").write_text(op["token"])
            self._path(op["stage"]).mkdir(parents=True, exist_ok=False)
            return "staged\n"
        if action == "release":
            lock=self._path(base+"/install.lock");owner=self._path(base+"/install.lock/owner")
            if owner.exists() and owner.read_text()==op["token"]:owner.unlink();lock.rmdir()
            return "released\n"
        if action == "verify":
            for name, expected in op["files"].items():
                path = self._path(op["stage"] + "/" + name)
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if "manifest-corrupt" in self.faults or actual != expected: raise SimulationError("Tablet staged file checksum verification failed.")
            return "verified\n"
        if action == "activate":
            current = self._path(base+"/current"); backup = self._path(op["backup"]); stage = self._path(op["stage"])
            if current.exists(): current.rename(backup)
            stage.rename(current)
            if "reboot-during-install" in self.faults: self.reboot(); raise UnknownMutationError("Simulated reboot during activation; inspect the installation before retrying.")
            for path, source in op["runtime"].items():
                target = self._path(path); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(self._path(base+"/current/"+source).read_bytes())
            if "activation-failure" in self.faults: raise SimulationError("Injected xochitl activation failure.")
            self._state["pid"] += 1; self._save()
            return "activated\n"
        if action == "rollback":
            owner=self._path(base+"/install.lock/owner")
            if not owner.exists() or owner.read_text()!=op["token"]:return "lock belongs to another transaction; unchanged\n"
            if "restore-failure" in self.faults: raise SimulationError("Injected rollback failure.")
            current = self._path(base+"/current"); backup = self._path(op["backup"]); stage = self._path(op["stage"])
            for runtime in (LOADER_CONF, RECOVERY_UNIT):
                path = self._path(runtime)
                if path.exists(): path.unlink()
            if backup.exists():
                if current.exists(): shutil.rmtree(current)
                backup.rename(current)
            elif op.get("activated") and not op.get("previous_current") and current.exists(): shutil.rmtree(current)
            if stage.exists(): shutil.rmtree(stage)
            for path, encoded in op.get("previous_runtime", {}).items():
                import base64
                target = self._path(path); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(base64.b64decode(encoded))
            return "rolled back\n"
        if action == "heartbeat":
            if not self._path(LOADER_CONF).exists(): raise SimulationError("Extension loader is absent after reboot.")
            return json.dumps({"protocol": 2, "firmware": self._state["firmware"], "xochitl_sha256": self._state["xochitl_sha256"], "pid": self._state["pid"], "instance_id": str(uuid.uuid4()), "heartbeat_ms": int(time.time()*1000), "firmware_verified": True, "binary_verified": True})
        if action == "art-loaded":
            if not self._path(LOADER_CONF).exists() or not self._path(base+"/current/lib/krita_viewport.so").exists(): raise SimulationError("Simulated art viewport did not load.")
            return "viewport-loaded\n"
        if action == "uninstall":
            if self._path(base+"/install.lock").exists():raise SimulationError("An installation transaction is active or interrupted; inspect install.lock before uninstalling.")
            current = self._path(base+"/current")
            if current.exists(): current.rename(self._path(op["archive"]))
            for runtime in (LOADER_CONF, RECOVERY_UNIT):
                path = self._path(runtime)
                if path.exists(): path.unlink()
            if "uninstall-failure" in self.faults:raise SimulationError("Injected failure after uninstall rename.")
            return "uninstalled\n"
        if action == "restore-uninstall":
            if "restore-failure" in self.faults:raise SimulationError("Injected uninstall restoration failure.")
            archive=self._path(op["archive"]);current=self._path(base+"/current")
            if not archive.exists():return "unchanged\n"
            if current.exists():raise SimulationError("Current app changed during uninstall; manual restoration required.")
            archive.rename(current)
            for path,encoded in op["previous_runtime"].items():
                import base64
                target=self._path(path);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(base64.b64decode(encoded))
            return "uninstall restored\n"
        raise SimulationError(f"Unknown simulator script operation: {action}")

    def _installed(self):
        try: return json.loads(self._path(APP_ROOT+"/current/manifest.json").read_text())
        except FileNotFoundError: return {"features": []}

    def status(self) -> dict:
        self._online(); features = self._installed().get("features", [])
        advanced = self._path(LOADER_CONF).exists() and self._state["firmware"] == PINNED_FIRMWARE and "wrong-hash" not in self.faults and "wrong-firmware" not in self.faults
        caps = {"files": True, "upload": True, "download": True, "export": True, "sync": True,
                "keyboard": "keyboard" in features, "touch": "touch" in features,
                "direct-paste": advanced and "direct-paste" in features, "paste_image": advanced and "direct-paste" in features,
                "screenshot": advanced and "direct-paste" in features, "pages": advanced and "direct-paste" in features,
                "refresh": self._state["model"] == "paper-pro", "art-display": advanced and "art-display" in features, "art_pressure": "art-display" in features}
        doc = self._active(optional=True)
        active = {key: doc[key] for key in ("id", "name", "pages")} | {"page": self._state["page"]} if doc else {}
        return {"connected": True, "simulated": True, "model": self._state["model"], "firmware": self._state["firmware"], "host": "simulation", "transport": "isolated files", "capabilities": caps, "active_document": active, "message": "Simulation — no physical tablet is connected."}

    def documents(self):
        self._online(); return [{key: doc[key] for key in ("id", "name", "type", "pages", "modified", "folder")} for doc in self._state["documents"]]

    def _active(self, optional=False):
        doc = next((item for item in self._state["documents"] if item["id"] == self._state["active_id"]), None)
        if doc is None and not optional: raise SimulationError("Open a document on the simulated tablet first.")
        return doc

    def open_document(self, ident):
        self._online()
        if not any(doc["id"] == ident for doc in self._state["documents"]): raise SimulationError("Document is absent from the simulation.")
        self._state["active_id"] = ident; self._state["page"] = 1; self._save()

    def upload(self, path, folder=""):
        self._online(); source = Path(path)
        if source.suffix.lower() not in {".pdf", ".epub", ".rmdoc"}: raise SimulationError("Native imports accept PDF, EPUB and RMDOC files.")
        if not source.is_file() or source.is_symlink(): raise SimulationError("Select an existing regular document file.")
        data = source.read_bytes()
        if source.suffix.lower() == ".pdf" and not data.startswith(b"%PDF-"): raise SimulationError("The selected PDF has no valid PDF signature.")
        if source.suffix.lower() == ".epub" and not data.startswith(b"PK"): raise SimulationError("The selected EPUB has no ZIP signature.")
        ident = str(uuid.uuid4()); kind = source.suffix.lower()[1:]
        title = source.stem
        image_count = 0
        if kind == "rmdoc":
            try:
                with zipfile.ZipFile(source) as archive:
                    names = archive.namelist()
                    if (len(names) > 1000 or any(PurePosixPath(n).is_absolute() or ".." in PurePosixPath(n).parts for n in names)
                            or sum(i.file_size for i in archive.infolist()) > 64 * 1024 * 1024):
                        raise ValueError("Invalid or excessive archive")
                    metadata = [n for n in names if n.endswith(".metadata")]
                    if len(metadata) != 1 or not any(n.endswith(".content") for n in names):
                        raise ValueError("Not a single document archive")
                    title = json.loads(archive.read(metadata[0])).get("visibleName", title)
                    image_count = sum(n.endswith((".png", ".jpg")) for n in names)
            except (ValueError, KeyError, zipfile.BadZipFile) as exc:
                raise SimulationError("The selected RMDOC is not a supported native document archive.") from exc
        self._path(f"/.simulator/documents/{ident}.{kind}").write_bytes(data)
        self._state["documents"].append({"id": ident, "name": str(title), "type": "notebook" if kind == "rmdoc" else kind, "pages": 1, "modified": time.time(), "folder": folder or "Inbox", "text": "Imported simulated document", "images": [{"simulated": True} for _ in range(image_count)], "templates": ["blank"]})
        self._save(); self._record("native-import", id=ident, bytes=len(data))
        if "upload-unknown" in self.faults: raise UnknownMutationError("Import may have completed, but its receipt was lost. Inspect Documents; do not retry the upload automatically.")
        return {"ok": True, "message": "Imported into the simulation.", "id": ident, "state": "verified"}

    def _export(self, doc, destination):
        target = Path(destination)
        if target.exists() and target.is_dir(): target = target/(doc["name"]+".pdf")
        target.parent.mkdir(parents=True, exist_ok=True)
        source = self._path(f"/.simulator/documents/{doc['id']}.pdf")
        data = source.read_bytes() if doc["type"] == "pdf" and not doc.get("images") and source.exists() else _pdf(doc["text"] + f"\nSimulation pages: {doc['pages']}\nNative image objects: {len(doc.get('images', []))}")
        try:
            with target.open("xb") as stream: stream.write(data)
        except FileExistsError as exc: raise SimulationError("Export destination already exists. Choose a new name to preserve the original.") from exc
        self._record("export", id=doc["id"])
        return {"ok": True, "message": "Exported a simulated PDF copy.", "path": str(target), "id": doc["id"]}

    def download(self, ident, destination):
        self._online()
        if not re.fullmatch(r"[0-9a-f-]{36}", ident): raise SimulationError("Invalid document ID.")
        doc = next((item for item in self._state["documents"] if item["id"] == ident), None)
        if doc is None: raise SimulationError("Document no longer exists.")
        return self._export(doc, destination)

    def export_current(self, destination): self._online(); return self._export(self._active(), destination)

    def _require(self, feature):
        self._online()
        if not self.status()["capabilities"].get(feature): raise SimulationError(f"Install or restore the compatible {feature} feature first.")

    def screenshot(self, destination):
        self._require("screenshot")
        width, height = (162,216) if self._state["model"] == "paper-pro" else (140,187)
        raw = bytearray()
        for y in range(height):
            raw.append(0)
            for x in range(width): raw.extend((35,35,35) if x in (5,width-6) or y in (5,height-6) else (247,244,235))
        def chunk(kind, data): return struct.pack(">I",len(data))+kind+data+struct.pack(">I",zlib.crc32(kind+data)&0xffffffff)
        data = b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",width,height,8,2,0,0,0))+chunk(b"tEXt",b"Description\x00PaperDesk synthetic screenshot")+chunk(b"IDAT",zlib.compress(raw))+chunk(b"IEND",b"")
        target=Path(destination); target.parent.mkdir(parents=True,exist_ok=True)
        with target.open("xb") as stream: stream.write(data)
        self._record("screenshot"); return {"ok": True,"message":"Saved a synthetic simulated screenshot.","path":str(target)}

    def type_text(self,text):
        self._require("keyboard"); doc=self._active(); doc["text"]+=text; doc["modified"]=time.time(); self._save(); self._record("type",characters=len(text))
        return {"ok":True,"message":f"Typed {len(text)} characters in the simulation."}

    def send_key(self,key):
        self._require("keyboard"); allowed={"backspace","erase-five","enter","tab","escape","copy","paste","close","left","right","up","down"}
        if key not in allowed: raise SimulationError("Unknown key command.")
        doc=self._active()
        if key in {"backspace","erase-five"}: doc["text"]=doc["text"][:-1 if key=="backspace" else -5]
        elif key in {"enter","tab"}: doc["text"]+="\n" if key=="enter" else "\t"
        elif key=="copy": self._state["clipboard"]=doc["text"]
        elif key=="paste": doc["text"]+=self._state["clipboard"]
        elif key=="close": self._state["active_id"]=""
        elif key=="right": self._state["page"]=min(doc["pages"],self._state["page"]+1)
        elif key=="left": self._state["page"]=max(1,self._state["page"]-1)
        self._save();self._record("key",key=key); return {"ok":True,"message":f"Sent {key} in the simulation."}

    def touch(self,phase,x,y):
        self._require("touch")
        if phase not in {"down","move","up"} or not 0<=x<=1 or not 0<=y<=1: raise SimulationError("Touch requires down/move/up and coordinates between 0 and 1.")
        self._record("touch",phase=phase,x=x,y=y); return {"ok":True,"message":"Simulated touch recorded."}

    def paste_image(self,path):
        self._require("direct-paste"); doc=self._active()
        if doc["type"] not in {"notebook","pdf"}: raise SimulationError("Native image insertion supports portrait notebook or PDF pages.")
        from PIL import Image
        source=Path(path)
        if source.stat().st_size>40*1024*1024: raise SimulationError("Image exceeds the 40 MB limit.")
        with Image.open(source) as image:
            if image.width*image.height>30_000_000 or min(image.size)<8: raise SimulationError("Image dimensions are unsupported.")
            size=image.size
        doc["images"].append({"sha256":hashlib.sha256(source.read_bytes()).hexdigest(),"width":size[0],"height":size[1],"page":self._state["page"]})
        doc["modified"]=time.time();self._save();self._record("native-image",id=doc["id"])
        return {"ok":True,"message":"Native image object inserted into the simulation.","state":"persisted"}

    def add_page(self,template):
        self._require("pages")
        if template not in {"cornell","grid","lined","blank"}: raise SimulationError("Unknown page template.")
        doc=self._active()
        if doc["type"]!="notebook": raise SimulationError("Page creation requires an open notebook.")
        doc["pages"]+=1;doc["templates"].append(template);self._state["page"]=doc["pages"];self._save();self._record("native-page",template=template)
        return {"ok":True,"message":f"Added simulated {template} page.","pages":doc["pages"]}

    def refresh(self):
        self._require("refresh");self._record("refresh",nap_verified=True);return {"ok":True,"message":"Simulated suspend/wake refresh verified."}

    def sync(self,folder):
        self._online(); target=Path(folder);target.mkdir(parents=True,exist_ok=True); exported=[]
        for doc in self._state["documents"]:
            if not doc.get("images") and doc["type"]!="notebook":continue
            path=target/f"{doc['name']}_{doc['id'][:8]}_markup.pdf"
            if path.exists():continue
            exported.append(self._export(doc,path)["path"])
        return {"ok":True,"message":f"Exported {len(exported)} simulated annotation copies.","paths":exported,"simulated":True}

    def diagnostics(self):
        return {"simulated":True,"root":str(self.root),"faults":sorted(self.faults),"boot_count":self._state["boot_count"],"installed":self._installed(),"operation_count":len(self.operations),"limits":"Behavioral contract model only; no firmware, USB, e-ink, kernel, SSH, or XOVI execution."}

    def reboot(self):
        self._online();run=self._path("/run")
        # Validate every descendant before deleting; reject symlink tricks.
        for path in run.rglob("*"): self._path("/"+str(path.relative_to(self.root)).replace(os.sep,"/"))
        shutil.rmtree(run);run.mkdir();self._state["boot_count"]+=1;self._state["pid"]+=1;self._save();self._record("reboot")
