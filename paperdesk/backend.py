"""Synchronous, worker-friendly device API used by the Qt suite.

Startup stays disconnected. Physical mutations use native import APIs or the
guarded tablet extension; none write to xochitl's live document store. Upload
and advanced requests are durable and never replayed after uncertain results.
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import math
import os
import re
import shlex
import stat
import struct
import tempfile
import time
import uuid
from pathlib import Path

from .models import DeviceConfig
from .transport import (DeviceError, HostTrustRequired, UnknownMutation, SSHTransport,
                        PortableLock, atomic_json, state_directory,
                        probe_host_key, trust_host_key)
from .vendor.direct_paste.host_core import (prepare, PROTOCOL, FIRMWARE,
                                           XOCHITL_SHA256, KNOWN_PAPERS)

STORE = "/home/root/.local/share/remarkable/xochitl"
CACHE = "/home/root/.cache/remarkable-direct-paste"
KEYBOARD = "/home/root/.local/lib/paperdesk/current/bin/injector"
TOUCH = "/home/root/.local/lib/paperdesk/current/bin/touch-injector"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_LETTERS = dict(zip("abcdefghijklmnopqrstuvwxyz", (30,48,46,32,18,33,34,35,23,36,37,38,50,49,24,25,16,19,31,20,22,47,17,45,21,44)))
KEYMAP = {char: (code, False) for char, code in _LETTERS.items()}
KEYMAP.update({char.upper(): (code, True) for char, code in _LETTERS.items()})
KEYMAP.update({char: (code, False) for char, code in zip("1234567890", range(2,12))})
KEYMAP.update({char: (code, False) for char, code in zip(" -=[];'`\\,./", (57,12,13,26,27,39,40,41,43,51,52,53))})
KEYMAP.update({char: (code, True) for char, code in zip('!@#$%^&*()_+{}:"~|<>?', (2,3,4,5,6,7,8,9,10,11,12,13,26,27,39,40,41,43,51,52,53))})
PUNCTUATION = str.maketrans({"‘":"'", "’":"'", "“":'"', "”":'"', "–":"-", "—":"-", "−":"-", "…":"...", "\u00a0":" "})
KEYS = {"backspace":14, "enter":28, "tab":15, "escape":1, "left":105, "right":106, "up":103, "down":108}


def _uuid(value: object) -> str:
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise DeviceError("A valid document or request identifier is required.")
    return value


def _json_bytes(data: bytes, label: str) -> dict:
    try:
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError) as exc:
        raise DeviceError(f"The tablet returned invalid {label}; inspect diagnostics.") from exc


def _source_snapshot(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise DeviceError("Choose a regular local file, rather than a symbolic link.")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise DeviceError("The upload source must be a regular file.")
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DeviceError("Cannot read the selected source file.") from exc
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if identity(before) != identity(after) or identity(before) != identity(current):
        raise DeviceError("The source changed while reading it. Save it, then choose the stable file.")
    return {"path": str(path.resolve()), "sha256": digest.hexdigest(), "size": before.st_size,
            "mtime_ns": before.st_mtime_ns, "device": before.st_dev, "inode": before.st_ino}


def _safe_name(name: str) -> str:
    # Portable Windows/macOS/Linux filename, never a path derived from metadata.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:160] or "document"
    if name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1,10)), *(f"LPT{i}" for i in range(1,10))}:
        name = "_" + name
    return name


def _destination(destination: str, name: str, suffix: str) -> Path:
    if not isinstance(destination, str) or not destination or "\x00" in destination:
        raise DeviceError("Choose a destination filename or folder.")
    path = Path(destination).expanduser()
    if path.is_dir():
        path = path / (_safe_name(name) + suffix)
    if path.exists() or path.is_symlink():
        raise DeviceError("That destination already exists. Choose a new filename to preserve the original.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class SuiteService:
    def __init__(self, config: DeviceConfig):
        self.config = config
        self.state_dir = state_directory(config)
        self.transport = SSHTransport(config)
        self.simulator = None
        self.simulated = False
        self._connected = False
        self._installer = None
        self._art = None
        self._keyboard = None
        self._touch = None
        self._input_uncertain = False
        self._touch_down = False
        self._touch_position = (0.5, 0.5)
        self._last_refresh = 0.0
        self._ink_probed = False

    @classmethod
    def demo(cls, root: Path | None = None, model: str = "paper-pro", firmware: str = FIRMWARE) -> "SuiteService":
        from .simulator import TabletSimulator
        simulator = TabletSimulator(root, model=model, firmware=firmware)
        service = cls(DeviceConfig(host="simulation", state_dir=simulator.root / ".paperdesk-host-state"))
        service.simulator = simulator
        service.transport = simulator
        service.simulated = True
        service._connected = True
        return service

    @staticmethod
    def probe_host_key(config: DeviceConfig) -> dict:
        return probe_host_key(config)

    @staticmethod
    def trust_host_key(config: DeviceConfig, fingerprint: str) -> dict:
        return trust_host_key(config, fingerprint)

    def _require_connected(self) -> None:
        if not self._connected:
            raise DeviceError("Connect to a paired tablet or start Simulation first.")
        if not self.simulated and not self.transport.connected:
            self._connected = False
            raise DeviceError("The tablet connection was lost. Reconnect before continuing.")

    @property
    def installer(self):
        self._require_connected()
        if self._installer is None:
            from .installer import DeviceInstaller
            self._installer = DeviceInstaller(self.transport, self.state_dir)
        return self._installer

    @property
    def art(self):
        if self._art is None:
            from .art import ArtManager
            self._art = ArtManager(self.config, transport=self.transport, simulated=self.simulated)
        return self._art

    def connect(self) -> dict:
        if self.simulated:
            self.simulator.connected = True
        elif not self.transport.connected:
            self.transport.connect()
        self._connected = True
        self._input_uncertain = False
        try:
            return self.status()
        except Exception:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        if self._art is not None:
            self._art.stop()
        for stream in (self._keyboard, self._touch):
            if stream is not None:
                stream.close()
        self._keyboard = self._touch = None
        self._touch_down = False
        if self.simulated:
            self.simulator.connected = False
        else:
            self.transport.close()
        self._connected = False
        self._ink_probed = False

    def status(self) -> dict:
        if not self._connected:
            return {"connected": False, "simulated": self.simulated, "model": "", "firmware": "", "host": self.config.host,
                    "transport": "Simulation" if self.simulated else "SSH", "capabilities": {}, "active_document": {}, "message": "Disconnected"}
        self._require_connected()
        if self.simulated:
            result = dict(self.simulator.status())
            result["connected"] = True
            result["simulated"] = True
            result["host"] = "simulation"
            result["transport"] = "Simulation"
            result["capabilities"]["experimental-ink"] = result.get("model") == "paper-pro" and result.get("firmware") == FIRMWARE and not {"wrong-hash", "wrong-firmware"} & self.simulator.faults
            result["art"] = self.art.status()
            return result
        probe = self.installer.probe()
        model = probe.get("model", "unknown")
        capabilities = {"upload": True, "download": True, "export": True,
                        "keyboard": self.transport.exists(KEYBOARD),
                        "touch": model in ("paper-pro", "Paper Pro", "reMarkable Paper Pro") and self.transport.exists(TOUCH),
                        "direct-paste": False, "screenshot": False, "add-page": False,
                        "refresh": model in ("paper-pro", "Paper Pro", "reMarkable Paper Pro"),
                        "art-display": False, "experimental-ink": False,
                        "sync": True, "files": True, "pages": False}
        active = {}
        try:
            extension = self._extension_status(require_ready=False)
            capabilities.update({"direct-paste": extension.get("ready") is True,
                                 "screenshot": isinstance(extension.get("capture_slots"), int) and extension["capture_slots"] > 0,
                                 "add-page": extension.get("native_pages_ready") is True})
            capabilities["paste_image"] = capabilities["direct-paste"]
            capabilities["pages"] = capabilities["add-page"]
            ident = extension.get("document_uuid")
            if isinstance(ident, str) and UUID_RE.fullmatch(ident):
                metadata = _json_bytes(self.transport.read_bytes(f"{STORE}/{ident}.metadata"), "document metadata")
                active = {"id": ident, "name": str(metadata.get("visibleName", "Document")),
                          "page": extension.get("page_index", 0) + 1 if isinstance(extension.get("page_index"), int) else 1,
                          "pages": extension.get("page_count", 1)}
        except DeviceError:
            pass
        capabilities["experimental-ink"] = (model in ("paper-pro", "Paper Pro", "reMarkable Paper Pro")
                                            and probe.get("firmware") == FIRMWARE and probe.get("xochitl_sha256") == XOCHITL_SHA256)
        try:
            manifest = _json_bytes(self.transport.read_bytes("/home/root/.local/lib/paperdesk/current/manifest.json"), "installation manifest")
            capabilities["art-display"] = "art-display" in manifest.get("features", []) and probe.get("owned_loader") is True and capabilities["experimental-ink"]
            if capabilities["art-display"]:
                mapped = self.transport.run("pid=$(pidof xochitl); case \"$pid\" in ''|*[!0-9]*) printf absent;; *) library=$(readlink -f /home/root/.local/lib/paperdesk/current/lib/krita_viewport.so); if [ -n \"$library\" ] && grep -Fq \"$library\" /proc/$pid/maps; then printf loaded; else printf absent; fi;; esac")
                capabilities["art-display"] = mapped.strip() == "loaded"
        except DeviceError:
            pass
        art = self.art.status()
        capabilities.update({"art-" + key.replace("_", "-"): value for key, value in art.get("capabilities", {}).items() if isinstance(value, bool) and key != "art_display"})
        return {"connected": True, "simulated": False, "model": model, "firmware": probe.get("firmware", "unknown"),
                "host": self.config.host, "transport": "SSH over USB" if self.config.host == "10.11.99.1" else "SSH over network",
                "capabilities": capabilities, "active_document": active, "art": art,
                "message": "Connected. Advanced features require an explicitly installed compatible extension."}

    def _inventory_once(self) -> dict[str, dict]:
        result = {}
        for name in sorted(self.transport.list_dir(STORE)):
            if not name.endswith(".metadata") or not UUID_RE.fullmatch(name[:-9]):
                continue
            ident = name[:-9]
            raw = self.transport.read_bytes(f"{STORE}/{name}")
            metadata = _json_bytes(raw, "document metadata")
            if metadata.get("deleted") or metadata.get("parent") == "trash":
                continue
            kind = "folder" if metadata.get("type") == "CollectionType" else "notebook"
            content_raw = b""
            pages = 0
            if kind != "folder":
                content_raw = self.transport.read_bytes(f"{STORE}/{ident}.content")
                content = _json_bytes(content_raw, "document content")
                kind = content.get("fileType", "notebook")
                if kind not in ("pdf", "epub", "notebook"):
                    raise DeviceError("Tablet inventory contains an unsupported document type.")
                page_items = content.get("cPages", {}).get("pages", [])
                legacy_pages = content.get("pages", [])
                pages = len(page_items) if page_items else len(legacy_pages) if isinstance(legacy_pages, list) else 0
                if not pages:
                    count = content.get("pageCount", 1)
                    pages = count if isinstance(count, int) and count >= 0 else 1
            parent = metadata.get("parent", "")
            if parent not in ("", None) and not UUID_RE.fullmatch(str(parent)):
                raise DeviceError("Tablet inventory contains an invalid folder identifier.")
            modified = metadata.get("lastModified", "0")
            try:
                modified = int(modified) / 1000
            except (ValueError, TypeError):
                modified = 0
            try:
                last_opened = int(metadata.get("lastOpened", "0"))
            except (ValueError, TypeError):
                last_opened = 0
            result[ident] = {"id": ident, "name": str(metadata.get("visibleName", "Document")), "type": kind,
                             "pages": pages, "modified": modified, "parent": parent or "",
                             "last_opened": last_opened,
                             "_fingerprint": hashlib.sha256(raw + b"\x00" + content_raw).hexdigest()}
        return result

    def _inventory(self) -> dict[str, dict]:
        first = self._inventory_once()
        second = self._inventory_once()
        if first != second:
            raise DeviceError("The tablet library changed during inventory. Wait for saving or syncing to finish.")
        return first

    def documents(self) -> list[dict]:
        self._require_connected()
        if self.simulated:
            return self.simulator.documents()
        inventory = self._inventory()
        return [{key: item[key] for key in ("id", "name", "type", "pages", "modified")}
                | {"folder": inventory.get(item["parent"], {}).get("name", "")}
                for item in sorted(inventory.values(), key=lambda item: item["name"].casefold())]

    def _folder(self, folder: str, inventory: dict) -> str:
        if not folder:
            return ""
        if not isinstance(folder, str) or "\x00" in folder:
            raise DeviceError("Choose a valid tablet folder.")
        candidates = [item["id"] for item in inventory.values() if item["type"] == "folder" and (item["name"] == folder or item["id"] == folder)]
        if len(candidates) != 1:
            raise DeviceError("The destination folder is missing or ambiguous. Choose a unique existing tablet folder.")
        return candidates[0]

    def _load_uploads(self) -> dict:
        path = self.state_dir / "uploads.json"
        if not path.exists():
            return {"version": 1, "operations": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("operations"), list):
                raise ValueError()
            for record in data["operations"]:
                _uuid(record.get("id"))
                if record.get("state") not in ("pending", "unknown", "success", "failed") or not isinstance(record.get("source"), dict) or not isinstance(record.get("before_ids"), list):
                    raise ValueError()
            return data
        except (ValueError, OSError, AttributeError, DeviceError) as exc:
            raise DeviceError("The upload journal is invalid. Inspect uploads.json before making another import.") from exc

    def _save_uploads(self, data: dict) -> None:
        atomic_json(self.state_dir / "uploads.json", data)

    def _reconcile_upload(self, operation: dict, inventory: dict, journal: dict) -> dict:
        if operation["state"] == "success":
            current = inventory.get(operation.get("document_id"), {})
            if current.get("parent") != operation["folder_id"]:
                raise UnknownMutation("The earlier upload no longer resolves to its original folder. No new upload was sent.")
            return {"ok": True, "message": "This file was already imported; the existing upload was verified.", "id": operation["document_id"], "state": "success", "reused": True}
        expected = set(operation["expected_names"])
        candidates = [ident for ident, item in inventory.items() if ident not in operation["before_ids"] and item["parent"] == operation["folder_id"] and item["type"] != "folder" and item["name"] in expected]
        if len(candidates) != 1:
            operation.update(state="unknown", candidates=candidates)
            self._save_uploads(journal)
            raise UnknownMutation("The prior upload has an unknown outcome and cannot be matched to one document. No upload was retried; inspect the tablet and upload journal.")
        operation.update(state="success", document_id=candidates[0], confirmed_at=time.time())
        self._save_uploads(journal)
        return {"ok": True, "message": "Native import verified in the tablet library.", "id": candidates[0], "state": "success"}

    def _web_select(self, folder_id: str) -> None:
        with self.transport.web_connection(timeout=30) as connection:
            connection.request("GET", "/documents/" + folder_id)
            response = connection.getresponse()
            response.read(1024 * 1024)
            if not 200 <= response.status < 300:
                raise DeviceError("Enable Settings → Storage → USB web interface on the tablet before uploading.")

    def _web_post_file(self, path: Path, source: dict) -> None:
        boundary = "PaperDesk" + uuid.uuid4().hex
        filename = _safe_name(path.name).replace('"', "_")
        prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        timeout = max(300, source["size"] / (1024 * 1024) * 8)
        with self.transport.web_connection(timeout=timeout) as connection:
            connection.putrequest("POST", "/upload")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(len(prefix) + source["size"] + len(suffix)))
            connection.endheaders()
            connection.send(prefix)
            with path.open("rb") as handle:
                sent = 0
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                if sent != source["size"]:
                    raise UnknownMutation("The source changed during upload. The import outcome is unknown; do not retry.")
            connection.send(suffix)
            response = connection.getresponse()
            response.read(1024 * 1024)
            if not 200 <= response.status < 300:
                raise UnknownMutation("The native importer did not confirm success. Inspect the tablet before making any additional upload.")

    def upload(self, path: str, folder: str = "") -> dict:
        self._require_connected()
        if self.simulated:
            return self.simulator.upload(path, folder)
        source_path = Path(path).expanduser()
        if source_path.suffix.lower() not in (".pdf", ".epub", ".rmdoc"):
            raise DeviceError("The native tablet importer accepts PDF, EPUB, and RMDOC files.")
        with PortableLock(self.state_dir / "writer.lock"):
            source = _source_snapshot(source_path)
            before = self._inventory()
            folder_id = self._folder(folder, before)
            journal = self._load_uploads()
            same = [operation for operation in journal["operations"] if operation["source"].get("sha256") == source["sha256"] and operation.get("folder_id") == folder_id and operation["state"] in ("pending", "unknown", "success")]
            if len(same) > 1:
                raise DeviceError("The upload journal contains multiple active records for this file; inspect it before proceeding.")
            if same:
                return self._reconcile_upload(same[0], before, journal)
            # Selecting the native folder is not a document mutation. A failed
            # selection records no pending import and can be safely retried.
            self._web_select(folder_id)
            if _source_snapshot(source_path) != source:
                raise DeviceError("The source changed before upload. No import was sent.")
            operation = {"id": str(uuid.uuid4()), "state": "pending", "source": source,
                         "folder_id": folder_id, "before_ids": sorted(before), "started_at": time.time(),
                         "expected_names": list(dict.fromkeys((source_path.name, source_path.stem, _safe_name(source_path.name), Path(_safe_name(source_path.name)).stem)))}
            journal["operations"].append(operation)
            self._save_uploads(journal)  # durable before the first POST byte
            try:
                self._web_post_file(source_path, source)
                if _source_snapshot(source_path) != source:
                    raise UnknownMutation("The source changed during upload; inspect the tablet before making another import.")
                # Only read inventories are retried while native indexing settles.
                deadline = time.monotonic() + 10
                after = self._inventory()
                while set(after) <= set(before) and time.monotonic() < deadline:
                    time.sleep(0.25)
                    after = self._inventory()
                return self._reconcile_upload(operation, after, journal)
            except Exception as exc:
                operation.update(state="unknown", failed_at=time.time())
                self._save_uploads(journal)
                if isinstance(exc, UnknownMutation):
                    raise
                raise UnknownMutation("The upload outcome is unknown. No automatic retry will occur; inspect the tablet and uploads.json.") from exc

    def create_sticker(self, path: str, folder: str = "") -> dict:
        self._require_connected()
        from .figure_tools import create_sticker
        source = _source_snapshot(Path(path).expanduser())
        # Native notebooks carry generated UUIDs. Rebuilding one after a lost
        # import receipt would change its hash and bypass the upload journal.
        # Keep one app-owned staging asset per stable image so an uncertain
        # sticker retry reconciles the existing operation without another POST.
        directory = self.state_dir / "stickers" / source["sha256"]
        with PortableLock(self.state_dir / "stickers.lock"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            record = directory / "source.json"
            if record.exists():
                try:
                    saved = json.loads(record.read_text(encoding="utf-8"))
                    destination = directory / saved["filename"]
                    if destination.parent != directory or destination.suffix != ".rmdoc" or destination.is_symlink():
                        raise ValueError()
                    if _source_snapshot(destination)["sha256"] != saved["rmdoc_sha256"]:
                        raise ValueError()
                except (ValueError, OSError, KeyError, DeviceError) as exc:
                    raise DeviceError("The staged sticker journal is invalid. Inspect it before importing another copy.") from exc
            else:
                destination = directory / (_safe_name(Path(path).stem) + ".rmdoc")
                create_sticker(path, str(destination))
                if _source_snapshot(Path(path).expanduser()) != source:
                    raise DeviceError("The source image changed while making the sticker. No import was sent.")
                atomic_json(record, {"filename": destination.name, "source_sha256": source["sha256"],
                                     "rmdoc_sha256": _source_snapshot(destination)["sha256"]})
        return self.upload(str(destination), folder)

    def download(self, document_id: str, destination: str) -> dict:
        self._require_connected()
        _uuid(document_id)
        if self.simulated:
            return self.simulator.download(document_id, destination)
        inventory = self._inventory()
        document = inventory.get(document_id)
        if not document or document["type"] == "folder":
            raise DeviceError("Choose an existing tablet document.")
        kind = document["type"]
        if kind in ("pdf", "epub"):
            output = _destination(destination, document["name"], "." + kind)
            count = self.transport.download(f"{STORE}/{document_id}.{kind}", output)
            return {"ok": True, "message": "Original file downloaded as a new copy.", "path": str(output), "id": document_id, "bytes": count, "includes_annotations": False}
        return self._export(document_id, destination, document["name"])

    def _export(self, document_id: str, destination: str, name: str) -> dict:
        document_id = _uuid(document_id)
        output = _destination(destination, name, ".pdf")
        created = False
        total = 0
        try:
            with self.transport.web_connection(timeout=300) as connection:
                connection.request("GET", f"/download/{document_id}/pdf")
                response = connection.getresponse()
                if response.status != 200:
                    raise DeviceError("Native PDF export failed. Enable the tablet USB web interface and check the selected document.")
                first = response.read(8)
                if not first.startswith(b"%PDF-"):
                    raise DeviceError("The tablet returned an invalid PDF export.")
                with output.open("xb") as handle:
                    created = True
                    handle.write(first)
                    total = len(first)
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        total += len(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            return {"ok": True, "message": "Native PDF export saved as a new copy, including tablet ink and images.", "path": str(output), "id": document_id, "bytes": total, "includes_annotations": True}
        except Exception as exc:
            if created:
                output.unlink(missing_ok=True)
            if isinstance(exc, DeviceError):
                raise
            raise DeviceError("The export was interrupted; no complete copy was saved.") from exc

    def export_current(self, destination: str) -> dict:
        self._require_connected()
        if self.simulated:
            return self.simulator.export_current(destination)
        inventory = self._inventory()
        source = "open-document"
        try:
            extension = self._extension_status(require_ready=False)
            ident = _uuid(extension.get("document_uuid"))
        except DeviceError:
            # Match the TUI fallback on a fresh tablet or after reboot. This
            # reads metadata only and clearly labels the most-recent target.
            source = "most-recently-opened"
            candidates = [item for item in inventory.values() if item["type"] != "folder" and item.get("last_opened", 0) > 0]
            if not candidates:
                raise DeviceError("Open a document on the tablet, then export it. No active-document extension or recent-open metadata is available.")
            latest = max(item["last_opened"] for item in candidates)
            newest = [item for item in candidates if item["last_opened"] == latest]
            if len(newest) != 1:
                raise DeviceError("The most recently opened document is ambiguous. Select an explicit document to export.")
            ident = newest[0]["id"]
        document = inventory.get(ident)
        if not document:
            raise DeviceError("Open a tablet document before exporting the current page's document.")
        result = self._export(ident, destination, document["name"])
        result["target_source"] = source
        if source == "most-recently-opened":
            result["message"] = "Most recently opened tablet document exported as a new PDF copy; the active-page extension was unavailable."
        return result

    def _extension_status(self, require_ready: bool = True) -> dict:
        status = _json_bytes(self.transport.read_bytes(f"{CACHE}/status.json"), "extension status")
        if status.get("protocol") != PROTOCOL or status.get("firmware") != FIRMWARE or status.get("xochitl_sha256") != XOCHITL_SHA256:
            raise DeviceError("This extension does not match its exact supported firmware and xochitl build. Use the install wizard for compatibility details.")
        _uuid(status.get("instance_id"))
        pid = status.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
            raise DeviceError("The extension process identity is unavailable.")
        try:
            age = abs(int(time.time() * 1000) - int(status.get("heartbeat_ms", 0)))
        except (ValueError, TypeError) as exc:
            raise DeviceError("The extension heartbeat is invalid.") from exc
        if age > 5000:
            raise DeviceError("The extension heartbeat is stale. Open the install wizard to restore current-boot support explicitly.")
        if self.transport.run("pidof xochitl").strip() != str(pid):
            raise DeviceError("The extension belongs to a different tablet app process.")
        if not require_ready:
            return status
        if status.get("error"):
            raise DeviceError("The extension reported an active-page error. Close tablet dialogs and inspect diagnostics.")
        methods = status.get("native_methods", [])
        has_method = isinstance(methods, list) and any(isinstance(item, dict) and item.get("signature") == "insertImageFileAsSceneItem(QUrl,QPointF)" for item in methods)
        if status.get("ready") is not True or status.get("insert_compiled") is not True or not has_method:
            raise DeviceError("Native image insertion is not ready. Open a supported portrait notebook or PDF page.")
        document = _uuid(status.get("document_uuid"))
        page = _uuid(status.get("page_uuid"))
        paper = next(((width, height) for width, height in KNOWN_PAPERS if status.get("paper_portrait_bounds") == {"x": -(width // 2), "y": 0, "width": width, "height": height}), None)
        if paper is None or (status.get("paper_width"), status.get("paper_height")) != paper:
            raise DeviceError("The active page does not have a supported portrait paper geometry.")
        path = f"{STORE}/{document}.content"
        first = self.transport.read_bytes(path)
        if first != self.transport.read_bytes(path):
            raise DeviceError("The notebook content changed during image preflight.")
        content = _json_bytes(first, "active document content")
        pages = content.get("cPages", {}).get("pages", [])
        if content.get("fileType") not in ("notebook", "pdf") or content.get("orientation") != "portrait" or not any(isinstance(item, dict) and item.get("id") == page for item in pages):
            raise DeviceError("The active page is not a verified portrait notebook or PDF page.")
        status["paper"] = paper
        return status

    def _block_prior_request(self, root: Path, status: dict) -> dict | None:
        if not root.exists():
            return
        for directory in root.iterdir():
            if not directory.is_dir() or not (directory / "send-attempted").exists():
                continue
            try:
                prior = json.loads((directory / "request.json").read_text(encoding="utf-8"))
                result = json.loads((directory / "result.json").read_text(encoding="utf-8")) if (directory / "result.json").exists() else {}
            except (OSError, ValueError) as exc:
                raise DeviceError("An earlier tablet request journal is unreadable. Inspect it before sending another request.") from exc
            if prior.get("document_uuid") == status["document_uuid"] and prior.get("page_uuid") == status["page_uuid"] and result.get("state") not in ("persisted", "rejected"):
                # A late receipt can resolve an earlier unknown result using
                # reads alone. Return the confirmed previous action, without
                # silently sending the user's same action a second time.
                remote_kind = "incoming" if root.name == "images" else "pages"
                try:
                    receipt = _json_bytes(self.transport.read_bytes(f"{CACHE}/{remote_kind}/{_uuid(directory.name)}/receipt.json"), "earlier request receipt")
                except DeviceError:
                    receipt = {}
                if receipt.get("request_id") == directory.name and receipt.get("state") in ("persisted", "rejected"):
                    atomic_json(directory / "result.json", receipt)
                    if receipt["state"] == "persisted":
                        return {"ok": True, **receipt, "reused": True,
                                "message": "The earlier native action is now confirmed. No additional image or page request was sent."}
                    continue
                raise UnknownMutation("An earlier operation on this page is unconfirmed. No new request was sent; inspect its receipt and the tablet before continuing.")

    def _request_receipt(self, directory: Path, remote: str, timeout: float) -> dict:
        request_id = directory.name
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                receipt = _json_bytes(self.transport.read_bytes(f"{remote}/receipt.json"), "request receipt")
                if receipt.get("request_id") == request_id:
                    atomic_json(directory / "result.json", receipt)
                    if receipt.get("state") == "persisted":
                        return {"ok": True, "message": "The tablet confirmed native persistence.", **receipt}
                    if receipt.get("state") == "rejected":
                        raise DeviceError("The tablet rejected the native request before insertion. Reopen the supported page and inspect diagnostics.")
                    if receipt.get("state") == "failed-unconfirmed":
                        raise UnknownMutation("The native action failed without a persistence receipt. Inspect the tablet; it will not be replayed.")
            except UnknownMutation:
                raise
            except DeviceError:
                if (directory / "result.json").exists():
                    recorded = json.loads((directory / "result.json").read_text(encoding="utf-8"))
                    if recorded.get("state") == "rejected":
                        raise
            time.sleep(0.5)
        atomic_json(directory / "result.json", {"request_id": request_id, "state": "unknown"})
        raise UnknownMutation("No native persistence receipt arrived. The action may have happened; no automatic retry will occur.")

    def paste_image(self, path: str) -> dict:
        self._require_connected()
        if self.simulated:
            return self.simulator.paste_image(path)
        with PortableLock(self.state_dir / "writer.lock"):
            before = self._extension_status()
            root = self.state_dir / "requests" / "images"
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            resolved = self._block_prior_request(root, before)
            if resolved is not None:
                return resolved
            directory = root / str(uuid.uuid4())
            manifest = prepare(Path(path).expanduser(), directory, before["paper"])
            fresh = self._extension_status()
            if any(before[key] != fresh[key] for key in ("instance_id", "document_uuid", "page_uuid", "pid", "paper")):
                raise DeviceError("The active page changed during preparation. No request was sent.")
            created = int(time.time() * 1000)
            manifest.update(document_uuid=before["document_uuid"], page_uuid=before["page_uuid"], instance_id=before["instance_id"],
                            xochitl_sha256=XOCHITL_SHA256, created_ms=created, expires_ms=created + 600000)
            atomic_json(directory / "request.json", manifest)
            (directory / "send-attempted").touch(exist_ok=False)
            remote = f"{CACHE}/incoming/{directory.name}"
            try:
                self.transport.run(f"mkdir -p -m 700 {remote}")
                self.transport.write_bytes(f"{remote}/image.png", (directory / "image.png").read_bytes())
                # Atomic write publishes the request only after the image.
                self.transport.write_bytes(f"{remote}/request.json", (directory / "request.json").read_bytes())
                return self._request_receipt(directory, remote, 15)
            except UnknownMutation:
                raise
            except DeviceError as exc:
                if (directory / "result.json").exists() and json.loads((directory / "result.json").read_text()).get("state") == "rejected":
                    raise
                atomic_json(directory / "result.json", {"state": "unknown", "request_id": directory.name})
                raise UnknownMutation("Image insertion has an unknown outcome. Inspect the tablet and request receipt; it will not be replayed.") from exc

    def add_page(self, template: str) -> dict:
        self._require_connected()
        templates = {"cornell": "P Cornell", "grid": "P Grid medium", "lined": "P Lines medium", "blank": "Blank"}
        if template not in templates:
            raise DeviceError("Choose Cornell, grid, lined, or blank for the new page.")
        if self.simulated:
            return self.simulator.add_page(template)
        with PortableLock(self.state_dir / "writer.lock"):
            status = self._extension_status(require_ready=False)
            _uuid(status.get("document_uuid")); _uuid(status.get("page_uuid"))
            if status.get("native_pages_ready") is not True:
                raise DeviceError("Native page controls are not ready. Close tablet dialogs and open a document.")
            root = self.state_dir / "requests" / "pages"
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            resolved = self._block_prior_request(root, status)
            if resolved is not None:
                return resolved
            directory = root / str(uuid.uuid4())
            directory.mkdir(mode=0o700)
            created = int(time.time() * 1000)
            manifest = {"protocol": PROTOCOL, "request_id": directory.name, "template": templates[template],
                        "instance_id": status["instance_id"], "document_uuid": status["document_uuid"], "page_uuid": status["page_uuid"],
                        "xochitl_sha256": XOCHITL_SHA256, "created_ms": created, "expires_ms": created + 30000}
            atomic_json(directory / "request.json", manifest)
            fresh = self._extension_status(require_ready=False)
            if any(fresh[key] != status[key] for key in ("instance_id", "document_uuid", "page_uuid", "pid")):
                raise DeviceError("The active page changed before the request. No page addition was sent.")
            (directory / "send-attempted").touch(exist_ok=False)
            remote = f"{CACHE}/pages/{directory.name}"
            try:
                self.transport.run(f"mkdir -p -m 700 {remote}")
                self.transport.write_bytes(f"{remote}/request.json", (directory / "request.json").read_bytes())
                return self._request_receipt(directory, remote, 20)
            except UnknownMutation:
                raise
            except DeviceError as exc:
                if (directory / "result.json").exists() and json.loads((directory / "result.json").read_text()).get("state") == "rejected":
                    raise
                atomic_json(directory / "result.json", {"state": "unknown", "request_id": directory.name})
                raise UnknownMutation("Page addition has an unknown outcome. Inspect the tablet and request receipt; it will not be replayed.") from exc

    def screenshot(self, destination: str) -> dict:
        self._require_connected()
        if self.simulated:
            return self.simulator.screenshot(destination)
        output = _destination(destination, "tablet-screen", ".png")
        with PortableLock(self.state_dir / "writer.lock"):
            before = self._extension_status(require_ready=False)
            slots = before.get("capture_slots")
            if not isinstance(slots, int) or isinstance(slots, bool) or not 1 <= slots <= 100:
                raise DeviceError("The installed extension does not support screenshot capture. Use the install wizard.")
            count = int(before.get("capture_count", 0))
            self.transport.run(f"touch {CACHE}/capture.request")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                time.sleep(0.5)
                current = self._extension_status(require_ready=False)
                if current["instance_id"] != before["instance_id"]:
                    raise DeviceError("The tablet app restarted during capture; no screenshot was saved.")
                if int(current.get("capture_count", 0)) > count:
                    break
            else:
                raise DeviceError("The tablet did not answer the screenshot request.")
            if current.get("capture_state") != "saved":
                raise DeviceError("The tablet reported a screen-capture failure.")
            slot = (int(current["capture_count"]) - 1) % slots + 1
            data = self.transport.read_bytes(f"{CACHE}/capture-{slot}.png")
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise DeviceError("The tablet returned an invalid screenshot.")
            with output.open("xb") as handle:
                handle.write(data)
            return {"ok": True, "message": "Tablet screenshot saved as a new copy.", "path": str(output), "width": current.get("capture_width"), "height": current.get("capture_height"), "bytes": len(data)}

    def _input_stream(self, kind: str):
        if self._input_uncertain:
            raise UnknownMutation("The previous input was interrupted and may have partly arrived. Inspect the tablet and reconnect; input will not be replayed.")
        path = KEYBOARD if kind == "keyboard" else TOUCH
        attribute = "_keyboard" if kind == "keyboard" else "_touch"
        stream = getattr(self, attribute)
        if stream is None or stream.closed:
            if not self.transport.exists(path):
                raise DeviceError(f"Install the {kind} helper explicitly through the install wizard first.")
            stream = self.transport.stream("exec " + shlex.quote(path), timeout=30)
            setattr(self, attribute, stream)
            time.sleep(1)  # uinput must register before the first event
            if stream.channel.exit_status_ready():
                stream.close()
                raise DeviceError(f"The tablet {kind} helper could not start. Check its installation and /dev/uinput.")
        return stream

    def _send_input(self, data: bytes, kind: str = "keyboard") -> None:
        # Helper availability/readiness checks happen before any input byte.
        # Their errors are ordinary preflight failures, not unknown mutations.
        stream = self._input_stream(kind)
        try:
            stream.write(data)
        except UnknownMutation:
            raise
        except (OSError, EOFError, DeviceError) as exc:
            self._input_uncertain = True
            raise UnknownMutation("Tablet input was interrupted and may be partial. Inspect the tablet; no input will be retried automatically.") from exc

    @staticmethod
    def _tap(code: int, modifier: int | None = None) -> bytes:
        return ((bytes((0, modifier)) if modifier is not None else b"") + bytes((0, code, 1, code))
                + (bytes((1, modifier)) if modifier is not None else b""))

    def type_text(self, text: str) -> dict:
        self._require_connected()
        if not isinstance(text, str) or len(text) > 100000:
            raise DeviceError("Enter no more than 100,000 characters.")
        if self.simulated:
            return self.simulator.type_text(text)
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").translate(PUNCTUATION)
        unsupported = sorted({char for char in normalized if char not in KEYMAP and char not in "\n\t"})
        if unsupported:
            raise DeviceError("The tablet keyboard helper supports the US ASCII layout. Replace unsupported characters before sending; no input was sent.")
        with PortableLock(self.state_dir / "writer.lock"):
            for char in normalized:
                if char in "\n\t":
                    payload = self._tap(28 if char == "\n" else 15)
                else:
                    code, shifted = KEYMAP[char]
                    payload = self._tap(code, 42 if shifted else None)
                self._send_input(payload)
                time.sleep(0.2)
        return {"ok": True, "message": "Text sent through the tablet keyboard. Check the focused text field on the tablet.", "characters": len(normalized), "normalized_punctuation": normalized != text}

    def send_key(self, key: str) -> dict:
        self._require_connected()
        if key not in (*KEYS, "erase-five", "copy", "paste", "close"):
            raise DeviceError("Choose a supported keyboard action.")
        if self.simulated:
            return self.simulator.send_key(key)
        with PortableLock(self.state_dir / "writer.lock"):
            if key == "erase-five":
                for _ in range(5):
                    self._send_input(self._tap(14)); time.sleep(0.2)
            elif key in ("copy", "paste", "close"):
                self._send_input(self._tap({"copy":46, "paste":25, "close":17}[key], 29))
            else:
                self._send_input(self._tap(KEYS[key]))
        return {"ok": True, "message": "Keyboard action sent to the tablet's focused control.", "key": key}

    def touch(self, phase: str, x: float, y: float) -> dict:
        self._require_connected()
        if phase not in ("down", "move", "up") or any(not isinstance(value, (float, int)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1 for value in (x,y)):
            raise DeviceError("Touch uses down/move/up with normalized x and y between 0 and 1.")
        if self.simulated:
            return self.simulator.touch(phase, x, y)
        model = self.installer.probe().get("model")
        if model not in ("paper-pro", "Paper Pro", "reMarkable Paper Pro"):
            raise DeviceError("Remote touch is currently supported only for Paper Pro's measured panel geometry.")
        if phase != "down" and not self._touch_down:
            raise DeviceError("Start a touch contact before moving or releasing it.")
        self._send_input(struct.pack("<BHH", {"down":0,"move":1,"up":2}[phase], round(x * 2064), round(y * 2832)), "touch")
        self._touch_down = phase != "up"
        self._touch_position = (x,y)
        return {"ok": True, "message": "Remote touch event sent.", "phase": phase, "x": x, "y": y}

    def refresh(self) -> dict:
        self._require_connected()
        if self.simulated:
            return self.simulator.refresh()
        probe = self.installer.probe()
        if probe.get("model") not in ("paper-pro", "Paper Pro", "reMarkable Paper Pro"):
            raise DeviceError("The verified full-screen refresh method currently supports Paper Pro only.")
        if time.monotonic() - self._last_refresh < 8:
            raise DeviceError("Wait a few seconds for the previous refresh to finish.")
        with PortableLock(self.state_dir / "writer.lock"):
            identifier = uuid.uuid4().hex
            marker = f"/run/paperdesk-refresh-{identifier}.ok"
            command = ("set -e; orig=$(cat /sys/power/autosleep); "
                       "restore() { printf '%s\\n' \"$orig\" > /sys/power/autosleep || true; }; trap restore EXIT; "
                       "echo off > /sys/power/autosleep; echo 1 > /sys/bus/i2c/devices/1-0048/enable_nowait; "
                       f"syspowernap 2; printf ok > {marker}")
            self._last_refresh = time.monotonic()
            try:
                self.transport.run(f"systemd-run --unit=paperdesk-refresh-{identifier} --no-block sh -c {shlex.quote(command)}")
                time.sleep(6)
                if self.transport.run(f"test -f {marker} && cat {marker}").strip() != "ok":
                    raise DeviceError("The tablet did not confirm suspend and wake.")
                self.transport.run(f"rm -f {marker}")
            except DeviceError as exc:
                raise UnknownMutation("Refresh could not be confirmed after the tablet nap. Wait, inspect the screen, and reconnect if needed; no automatic retry occurs.") from exc
            return {"ok": True, "message": "Paper Pro suspend and wake confirmed; the screen refreshed."}

    def sync(self, folder: str) -> dict:
        """One explicit bidirectional copy pass, with no deletion or watcher."""
        self._require_connected()
        if self.simulated:
            return self.simulator.sync(folder)
        local = Path(folder).expanduser()
        if local.is_symlink() or not local.is_dir():
            raise DeviceError("Choose an existing local folder for the explicit sync pass.")
        imported = []
        for path in sorted(local.iterdir()):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in (".pdf", ".epub", ".rmdoc"):
                imported.append(self.upload(str(path)))
        exports = local / "device-copies" / time.strftime("%Y%m%d-%H%M%S")
        exports.mkdir(parents=True, exist_ok=False)
        copied = []
        for document in self.documents():
            if document["type"] == "folder":
                continue
            output = exports / f"{_safe_name(document['name'])}_{document['id'][:8]}.pdf"
            copied.append(self._export(document["id"], str(output), document["name"]))
        return {"ok": True, "message": "Explicit sync copied local imports and native tablet PDF exports. Originals were preserved.", "uploaded": len(imported), "downloaded": len(copied), "path": str(exports), "mode": "copy-only"}

    def installer_plan(self, features: list[str]) -> dict:
        return self.installer.plan(features)

    def install(self, features: list[str]) -> dict:
        with PortableLock(self.state_dir / "writer.lock"):
            return self.installer.install(features)

    def uninstall(self) -> dict:
        for stream in (self._keyboard, self._touch):
            if stream is not None:
                stream.close()
        self._keyboard = self._touch = None
        self._touch_down = False
        if self._art is not None:
            self._art.stop()
        with PortableLock(self.state_dir / "writer.lock"):
            return self.installer.uninstall()

    def diagnostics(self) -> dict:
        self._require_connected()
        if self.simulated:
            return self.simulator.diagnostics()
        uploads = self._load_uploads()
        return {"ok": True, "simulated": False, "host": self.config.host, "port": self.config.port,
                "device": self.installer.probe(), "input_uncertain": self._input_uncertain,
                "pending_uploads": [{"id": item["id"], "state": item["state"]} for item in uploads["operations"] if item["state"] in ("pending", "unknown")],
                "state_directory": str(self.state_dir), "message": "Diagnostics contain no password, key material, or document contents."}

    def start_art(self, application: str = "krita", options: dict | None = None) -> dict:
        self._require_connected()
        return self.art.start(application, options)

    def stop_art(self) -> dict:
        if self._art is None:
            return {"ok": True, "message": "Art bridge is already stopped."}
        return self._art.stop()

    def open_art(self, application: str, path: str = "") -> dict:
        return self.art.open(application, path)

    def art_applications(self) -> list[dict]:
        return self.art.applications()

    applications = art_applications

    def art_status(self) -> dict:
        return self.art.status()

    def set_art_program(self, application: str, path: str) -> dict:
        return self.art.set_program_path(application, path)

    set_program_path = set_art_program

    def art_plugin_plan(self, destination: str = "") -> dict:
        return self.art.plugin_install_plan(destination)

    plugin_install_plan = art_plugin_plan

    def install_art_plugin(self, destination: str = "", replace: bool = False) -> dict:
        return self.art.install_plugin(destination, replace)

    install_plugin = install_art_plugin

    def simulate_reboot(self) -> dict:
        if not self.simulated:
            raise DeviceError("A sandbox reboot is available only in Simulation; no physical device was contacted.")
        self._require_connected()
        if self._art is not None:
            self._art.stop()
        self._ink_probed = False
        self.simulator.reboot()
        return {"ok": True, "message": "The simulated tablet rebooted. Restore advanced features explicitly in Setup.", "status": self.status()}

    def simulation_fault(self, name: str, enabled: bool = True) -> dict:
        if not self.simulated:
            raise DeviceError("Sandbox faults are available only in Simulation; no physical device was contacted.")
        self.simulator.set_fault(name, enabled)
        return {"ok": True, "message": f"Simulation fault {name} {'enabled' if enabled else 'cleared'}.", "faults": sorted(self.simulator.faults)}

    @staticmethod
    def prepare_ink(source: str, preview: str, placement=(1000,1000,9000,9000), threshold: int = 160,
                    resolution: int = 160, mode: str = "diagram", axis_ratio: float = 11180 / 15340) -> dict:
        from .figure_tools import prepare_ink
        return prepare_ink(source, preview, placement, threshold, resolution, mode, axis_ratio)

    def probe_ink(self) -> dict:
        self._require_connected()
        info = self.installer.probe()
        if info.get("model") != "paper-pro" or info.get("firmware") != FIRMWARE or info.get("xochitl_sha256") != XOCHITL_SHA256:
            raise DeviceError("Experimental figure ink requires the exactly supported Paper Pro firmware and xochitl build.")
        if self.simulated:
            self.simulator._record("probe-ink")
            result = {"ok": True, "simulated": True, "axis_ratio": 11180 / 15340,
                      "message": "Synthetic pen range selected for Simulation. Physical positioning and ink acceptance are not tested."}
        else:
            from .figure_tools import probe_ink
            with PortableLock(self.state_dir / "writer.lock"):
                result = probe_ink(self.transport, info)
        self._ink_probed = True
        return result

    def stream_ink(self, strokes, cancelled=lambda: False, progress=lambda n,total: None) -> dict:
        self._require_connected()
        if not self._ink_probed:
            raise DeviceError("Check the pen and inspect a calibration mark before drawing experimental ink.")
        # The GUI owns separate preview/calibration confirmations. The service
        # retains exact-device gating on every stream and never replays records.
        info = self.installer.probe()
        if info.get("firmware") != FIRMWARE or info.get("xochitl_sha256") != XOCHITL_SHA256 or info.get("model") != "paper-pro":
            self._ink_probed = False
            raise DeviceError("The device identity changed after the pen check; drawing was refused.")
        if self.simulated:
            from .vendor.ink.geometry import protocol
            records = protocol(strokes)
            for index, _record in enumerate(records):
                if cancelled():
                    raise UnknownMutation("Simulated ink was cancelled after partial records; no automatic replay occurs.")
                progress(index + 1, len(records))
            self.simulator._record("figure-ink", records=len(records))
            return {"ok": True, "simulated": True, "records": len(records), "message": "Simulation recorded bounded pen records; no physical ink was drawn."}
        from .figure_tools import stream_ink
        with PortableLock(self.state_dir / "writer.lock"):
            try:
                return stream_ink(self.transport, strokes, cancelled, progress)
            except Exception as exc:
                self._ink_probed = False
                raise UnknownMutation("Ink streaming stopped and partial marks may remain. Inspect the tablet; no strokes will be replayed.") from exc
