"""Where the window gets its SuiteService from, and native dialogs it may open.

Both are small seams: tests replace them, the real application uses the
backend class and the operating system's own file dialogs.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from ..models import DeviceConfig

FEATURES = [
    ("keyboard", "Keyboard forwarding",
     "Type from this computer into the document open on the tablet."),
    ("touch", "Touch forwarding",
     "Send taps from the touch surface on the Type page."),
    ("direct-paste", "Direct image paste",
     "Place a clipboard or file image on the page that is open."),
    ("art-display", "Art tablet mode",
     "Use the tablet as a drawing surface for Krita, Inkscape or Blender."),
]
FEATURE_NAMES = {fid: name for fid, name, _ in FEATURES}


def backend_class():
    from ..backend import SuiteService
    return SuiteService


class ServiceProvider:
    """Creates services on request; nothing here connects to anything."""

    def __init__(self, live_service: Any = None, demo_service: Any = None):
        self._live, self._demo = live_service, demo_service
        self._fresh = None

    def live(self, config: DeviceConfig) -> Any:
        if self._live is not None:
            if hasattr(self._live, "config"):
                try:
                    self._live.config = config
                except Exception:
                    pass
            return self._live
        return backend_class()(config)

    def demo(self, model: str | None = None, firmware: str | None = None) -> Any:
        """A simulated tablet.  Naming a model always builds a fresh, separate one.

        Nothing here reads saved connection settings: the simulator takes no host.
        """
        if model is None and firmware is None:
            if self._demo is not None:
                return self._demo
            return backend_class().demo()
        if self._fresh is not None:
            return self._fresh(model, firmware)
        options = {key: value for key, value in (("model", model), ("firmware", firmware)) if value}
        return backend_class().demo(**options)

    def set_fresh_factory(self, factory: Any) -> None:
        """Tests supply how a fresh simulated tablet is built."""
        self._fresh = factory


def method(service: Any, *names: str) -> Any:
    """The first of these calls the service actually offers, or None."""
    for name in names:
        found = getattr(service, name, None)
        if callable(found):
            return found
    return None


def accepts(function: Any, name: str) -> bool:
    """Whether a call takes this keyword, checked before anything is sent."""
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(p.kind is p.VAR_KEYWORD for p in parameters.values())


# Calls beyond ARCHITECTURE.md that the refinement contract names or implies.
STICKER = ("create_sticker",)
INK_PREPARE = ("prepare_ink", "ink_prepare", "prepare_figure_ink")
INK_PROBE = ("probe_ink", "ink_probe", "calibrate_ink")
INK_STREAM = ("stream_ink", "ink_stream", "draw_ink")
ART_APPLICATIONS = ("art_applications", "applications")
ART_PROGRAM = ("set_art_program", "set_art_program_path", "set_program_path", "set_art_application")
ART_PLUGIN_PLAN = ("art_plugin_plan", "plugin_install_plan", "install_art_plugin_plan")
ART_PLUGIN = ("install_art_plugin", "install_plugin")
SIM_REBOOT = ("simulate_reboot",)
SIM_FAULT = ("simulation_fault",)

_SETUP_HINTS = (
    (("install or restore", "setup wizard", "run setup", "feature first", "is not loaded",
      "loader is absent", "pen helper"), "Open Setup › Features", "setup", "features"),
    (("not paired", "fingerprint", "host key", "pair and connect", "authentication", "password",
      "developer mode", "cannot observe"), "Open Setup › Connect", "setup", "connect"),
    (("diagnostics", "install.lock", "inspect the installation"), "Open Diagnostics", "setup", "diagnostics"),
    (("krita's python plugin manager", "viewport plugin", "executable", "was not found. install"),
     "Open Art", "art", None),
    (("offline", "stopped answering", "reconnect", "connect to a tablet first"),
     "Open Setup › Connect", "setup", "connect"),
)


def setup_hint(text: str) -> tuple[str, str, str | None] | None:
    """Where in the window a failure can be acted on: (link text, page, section)."""
    lowered = text.lower()
    for needles, link, page, section in _SETUP_HINTS:
        if any(needle in lowered for needle in needles):
            return link, page, section
    return None


def describe_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    if isinstance(exc, (AttributeError, NotImplementedError, TypeError, ImportError)):
        return f"This build's device backend does not provide this operation ({text})."
    return text


class Dialogs:
    """Native file dialogs and confirmations, starting in the user's own folders."""

    def __init__(self, parent: QWidget):
        self.parent = parent
        self._last: dict[str, str] = {}

    def _start(self, kind: str) -> str:
        if kind in self._last:
            return self._last[kind]
        if kind == "keys":
            return str(Path.home())
        location = (QStandardPaths.StandardLocation.PicturesLocation if kind == "pictures"
                    else QStandardPaths.StandardLocation.DocumentsLocation)
        return QStandardPaths.writableLocation(location) or str(Path.home())

    def _remember(self, kind: str, path: str) -> None:
        if path:
            self._last[kind] = str(Path(path).parent)

    def open_file(self, title: str, filters: str = "", kind: str = "documents") -> str:
        path, _ = QFileDialog.getOpenFileName(self.parent, title, self._start(kind), filters)
        self._remember(kind, path)
        return path

    def open_files(self, title: str, filters: str = "", kind: str = "documents") -> list[str]:
        paths, _ = QFileDialog.getOpenFileNames(self.parent, title, self._start(kind), filters)
        if paths:
            self._remember(kind, paths[0])
        return list(paths)

    def save_file(self, title: str, suggested: str, filters: str = "", kind: str = "documents") -> str:
        """Ask for a new file name.  Existing files are never offered for overwrite."""
        start = str(Path(self._start(kind)) / suggested)
        while True:
            path, _ = QFileDialog.getSaveFileName(
                self.parent, title, start, filters, "", QFileDialog.Option.DontConfirmOverwrite)
            if not path:
                return ""
            if not Path(path).exists():
                self._remember(kind, path)
                return path
            self.inform("That file already exists",
                        f"{Path(path).name} already exists. PaperDesk never overwrites a file; "
                        "choose a different name.")
            start = path

    def directory(self, title: str, kind: str = "documents") -> str:
        path = QFileDialog.getExistingDirectory(self.parent, title, self._start(kind))
        if path:
            self._last[kind] = path
        return path

    def confirm(self, title: str, text: str, action: str, danger: bool = False) -> bool:
        box = QMessageBox(self.parent)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
        box.setText(title)
        box.setInformativeText(text)
        go = box.addButton(action, QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is go

    def inform(self, title: str, text: str) -> None:
        QMessageBox.information(self.parent, title, text)
