# SPDX-License-Identifier: AGPL-3.0-or-later
"""Art application discovery, safe launching, plugin installation and art sessions."""
from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .paths import cache_root, resource_root, state_root
from .windows_ink import windows_ink_available

APPLICATIONS = {"krita": ("Krita", "org.kde.krita"),
                "inkscape": ("Inkscape", "org.inkscape.Inkscape"),
                "blender": ("Blender", "org.blender.Blender")}
SUPPORTED_FIRMWARE = "3.28.0.172"
XOCHITL_SHA256 = "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021"
HELPER_PATH = "/home/root/.local/lib/paperdesk/current/bin/pen-bridge"
DISPLAY_PATH = "/home/root/.local/lib/paperdesk/current/lib/krita_viewport.so"


class ArtError(RuntimeError):
    """An actionable art setup or launch failure."""


def _application(application: str) -> str:
    if application not in APPLICATIONS:
        raise ArtError("Choose Krita, Inkscape or Blender.")
    return application


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_program(application: str, *, platform: str | None = None, home: Path | None = None, environ: dict | None = None) -> list[str]:
    """Find installed programs by files/PATH only; never invokes an installer or shell."""
    _application(application)
    platform, home = platform or sys.platform, home or Path.home()
    environ = os.environ if environ is None else environ
    name = application + (".exe" if platform == "win32" else "")
    executable = shutil.which(name, path=environ.get("PATH", ""))
    if executable:
        return [executable]
    candidates = []
    if platform == "darwin":
        title = APPLICATIONS[application][0]
        for root in (Path("/Applications"), home / "Applications"):
            candidates += [root / f"{title}.app" / "Contents" / "MacOS" / application]
    elif platform == "win32":
        roots = [Path(environ[key]) for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA") if environ.get(key)]
        for root in roots:
            if application == "krita":
                candidates += [root / "Krita (x64)" / "bin" / "krita.exe", root / "Krita" / "bin" / "krita.exe"]
            elif application == "inkscape":
                candidates += [root / "Inkscape" / "bin" / "inkscape.exe", root / "Inkscape" / "inkscape.exe"]
            else:
                base = root / "Blender Foundation"
                if base.is_dir():
                    candidates += sorted(base.glob("Blender*/blender.exe"), reverse=True)
                candidates += [root / "Blender" / "blender.exe"]
    else:
        candidates += [Path("/snap/bin") / application]
    for candidate in candidates:
        if candidate.is_file() and (platform == "win32" or os.access(candidate, os.X_OK)):
            return [str(candidate)]
    if platform.startswith("linux"):
        executable = shutil.which("flatpak", path=environ.get("PATH", ""))
        app_id = APPLICATIONS[application][1]
        roots = (Path(environ.get("XDG_DATA_HOME", str(home / ".local" / "share"))) / "flatpak" / "app", Path("/var/lib/flatpak/app"))
        if executable and any((root / app_id).is_dir() for root in roots):
            return [executable, "run", app_id]
    return []


def validate_options(options: dict | None, platform: str) -> dict:
    """Validate every value before a device or native input stream is opened."""
    options = dict(options or {})
    defaults = {"mode": "auto", "rotation": "auto", "aspect": "auto", "pressure": 0.5, "curve": 0.8,
                "tilt": 0.0, "touch": platform.startswith("linux"), "display": False,
                "echo": False, "refresh": 30.0, "jpeg": False, "new_canvas": False,
                "eraser_device": False, "palm_size": None, "eraser": None, "screen_rect": None, "open_path": ""}
    aliases = {"rotate": "rotation", "new": "new_canvas", "input_mode": "mode"}
    for old, new in aliases.items():
        if old in options:
            if new in options:
                raise ArtError(f"Specify {new} once.")
            options[new] = options.pop(old)
    extra = set(options) - set(defaults)
    if extra:
        raise ArtError("Unknown art option: " + ", ".join(sorted(extra)))
    defaults.update(options)
    if defaults["mode"] not in ("auto", "pen", "mouse"):
        raise ArtError("Art mode must be auto, pen or mouse.")
    if platform.startswith("linux") and defaults["mode"] == "mouse":
        raise ArtError("Linux art mode uses a native pen tablet. Choose auto or pen.")
    if defaults["rotation"] not in ("auto", "left", "right", "portrait", "inverted"):
        raise ArtError("Rotation must be auto, left, right, portrait or inverted.")
    for key in ("touch", "display", "echo", "jpeg", "new_canvas", "eraser_device"):
        if not isinstance(defaults[key], bool):
            raise ArtError(f"{key} must be on or off.")
    ranges = {"pressure": (0.05, 1), "curve": (0.2, 3), "tilt": (0, 3), "refresh": (0, 3600)}
    for key, (low, high) in ranges.items():
        value = defaults[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ArtError(f"{key} must be between {low} and {high}.")
    for key, low, high in (("palm_size", 1, 100000), ("eraser", 4, 200)):
        value = defaults[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high):
            raise ArtError(f"{key} must be between {low} and {high}.")
    if defaults["open_path"]:
        try:
            path = Path(defaults["open_path"]).expanduser().resolve(strict=True)
        except (TypeError, OSError):
            raise ArtError("Choose an existing local artwork file.") from None
        if not path.is_file(): raise ArtError("Choose an artwork file, not a folder.")
        defaults["open_path"] = str(path)
    elif not isinstance(defaults["open_path"], str):
        raise ArtError("Artwork path must be text.")
    aspect = defaults["aspect"]
    if aspect in ("auto", "full", None):
        # No Qt objects in worker threads. A GUI may pass its selected monitor's ratio.
        defaults["aspect"] = 16 / 10 if aspect == "auto" else None
    elif isinstance(aspect, str):
        try:
            w, h = (float(n) for n in aspect.split(":"))
            if not w > 0 or not h > 0: raise ValueError('Nonpositive aspect')
            aspect = w / h
        except (ValueError, ZeroDivisionError):
            raise ArtError("Aspect must be auto, full, a ratio such as 16:10, or a positive number.") from None
        defaults["aspect"] = aspect
    if defaults["aspect"] is not None:
        value = defaults["aspect"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.1 <= value <= 10:
            raise ArtError("Aspect ratio must be between 0.1 and 10.")
    screen = defaults["screen_rect"]
    if screen is not None:
        if not isinstance(screen, (list, tuple)) or len(screen) != 4 or not all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in screen) or screen[2] <= 0 or screen[3] <= 0:
            raise ArtError("Screen rectangle must contain x, y, positive width and positive height.")
        defaults["screen_rect"] = list(screen)
    if not platform.startswith("linux") and defaults["display"]:
        raise ArtError("Tablet canvas display currently requires Linux. Windows Ink pen/touch input is separate from the canvas display.")
    if platform == "darwin" and defaults["touch"]:
        raise ArtError("macOS supports native pointer mode without touch or pressure.")
    if platform == "win32" and defaults["touch"] and defaults["mode"] == "mouse":
        raise ArtError("Touch requires Windows Ink pen mode; turn it off for mouse mode.")
    return defaults


class ArtManager:
    def __init__(self, config, transport=None, simulated=False):
        self.config, self.transport, self.simulated = config, transport, bool(simulated)
        self.platform = sys.platform
        self.home = Path.home()
        self.environ = dict(os.environ)
        self.state_dir = Path(config.state_dir) if getattr(config, "state_dir", None) else state_root()
        self.program_paths = {}
        self._session = None
        self._sim_state = {"active": False, "application": "", "message": "Art mode is stopped."}
        self._probe = {}
        self._launched = []
        self._lock = threading.RLock()
        self._message = "Art mode is stopped."

    @property
    def assets(self):
        return resource_root() / "vendor" / "art"

    def _program(self, application):
        _application(application)
        if application in self.program_paths:
            path = Path(self.program_paths[application]).expanduser().resolve()
            if not path.is_file() or (self.platform != "win32" and not os.access(path, os.X_OK)):
                raise ArtError(f"The selected {APPLICATIONS[application][0]} executable is unavailable.")
            return [str(path)]
        return find_program(application, platform=self.platform, home=self.home, environ=self.environ)

    def set_program_path(self, application, path):
        """Explicit local setting for AppImages, portable apps and nonstandard installations."""
        _application(application)
        candidate = Path(path).expanduser().resolve(strict=True)
        if not candidate.is_file() or (self.platform != "win32" and not os.access(candidate, os.X_OK)):
            raise ArtError("Select an installed application executable.")
        self.program_paths[application] = str(candidate)
        return {"ok": True, "application": application, "path": str(candidate), "message": "Application path selected."}

    def capabilities(self):
        linux = self.platform.startswith("linux")
        evdev = linux and importlib.util.find_spec("evdev") is not None
        uinput = linux and Path("/dev/uinput").exists() and os.access("/dev/uinput", os.W_OK)
        native_ready = evdev and uinput
        ink = self.platform == "win32" and windows_ink_available()
        backend = getattr(getattr(self._session, "output", None), "backend", "")
        if backend == "native-mouse": ink = False
        pen = native_ready or ink
        return {"launch": True, "file_open": True, "pointer": self.platform in ("darwin", "win32") or native_ready,
                "pressure": pen, "tilt": pen, "multitouch": pen,
                "art_display": native_ready, "windows_ink_available": ink, "backend": backend,
                "experimental_pressure": self.platform == "darwin", "pressure_validation": "protocol-tested; physical target application validation pending",
                "experimental_backend_active": backend == "quartz-pen-experimental",
                "linux_driver_installed": evdev, "linux_input_permission": uinput,
                "tablet_support": "Paper Pro 3.28.0.172 with the pinned xochitl build",
                "pointer_only": self.platform == "darwin" or (self.platform == "win32" and not ink), "simulated": self.simulated,
                "provisioned": bool(self._probe.get("helper_verified")),
                "limits": "Windows Ink supports pressure/tilt/eraser and touch on Windows 10 1809+, with an explicit mouse fallback. Krita must use Windows 8+ Pointer Input. macOS defaults to native mouse input. Explicit pen mode uses experimental Quartz tablet packets with pressure/tilt; target app receipt is not yet verified. Native macOS multi-touch is unavailable. Tablet canvas display requires Linux. End-to-end physical tablet validation is pending for this release; older tablet art sensor layouts are not validated."}

    def applications(self):
        capabilities = self.capabilities()
        result = []
        for application, (title, _) in APPLICATIONS.items():
            argv = self._program(application)
            result.append({"id": application, "name": title, "installed": bool(argv), "simulated": self.simulated,
                           "executable": argv[0] if argv else "", "argv": argv,
                           "capabilities": {**capabilities, "art_display": capabilities["art_display"] and application == "krita"},
                           "message": "Ready to launch" if argv else f"Install {title}, or select its executable in the art settings."})
        return result

    def _socket(self):
        argv = self._program("krita")
        if self.platform.startswith("linux") and "run" in argv and APPLICATIONS["krita"][1] in argv:
            return self.home / ".var" / "app" / "org.kde.krita" / "cache" / "paperdesk-viewport.sock"
        return cache_root() / "paperdesk-viewport.sock"

    def open(self, application, path=""):
        _application(application)
        target = ""
        if path:
            try:
                source = Path(path).expanduser().resolve(strict=True)
            except OSError as error:
                raise ArtError("Choose an existing local artwork file.") from error
            if not source.is_file():
                raise ArtError("Choose an artwork file, not a folder.")
            target = str(source)  # An absolute filename cannot become an application option.
        if self.simulated:
            return {"ok": True, "simulated": True, "application": application, "path": target,
                    "message": f"Simulation: would open {APPLICATIONS[application][0]}" + (f" with {Path(target).name}." if target else ".")}
        argv = self._program(application)
        if not argv:
            raise ArtError(f"{APPLICATIONS[application][0]} was not found. Install it or select its executable.")
        env = dict(self.environ)
        if application == "krita" and "run" not in argv:
            # Native Krita reads this optional socket override; flatpak uses its private cache.
            socket_path = self._socket()
            socket_path.parent.mkdir(parents=True, exist_ok=True)
            env["RM_VIEWPORT_SOCKET"] = str(socket_path)
        if target:
            argv = [*argv, target]
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "env": env, "shell": False}
        if self.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(argv, **kwargs)
        except (OSError, subprocess.SubprocessError) as error:
            raise ArtError(f"Could not open {APPLICATIONS[application][0]}: {type(error).__name__}. Check the selected executable.") from error
        self._launched = [p for p in self._launched if p.poll() is None]
        self._launched.append(process)
        return {"ok": True, "application": application, "path": target, "pid": process.pid,
                "message": f"Opened {APPLICATIONS[application][0]}."}

    def _device_probe(self, display=False):
        if self.transport is None or not hasattr(self.transport, "stream"):
            raise ArtError("Pair and connect to the tablet before starting art mode.")
        # All device commands here are fixed read-only scripts, never interpolated config.
        script = """printf 'firmware='; sed -n 's/^IMG_VERSION=\"\\(.*\\)\"/\\1/p' /etc/os-release
printf 'arch='; uname -m
printf 'xochitl='; sha256sum /usr/bin/xochitl 2>/dev/null | cut -d' ' -f1
printf 'helper='; sha256sum /home/root/.local/lib/paperdesk/current/bin/pen-bridge 2>/dev/null | cut -d' ' -f1
printf 'display='; sha256sum /home/root/.local/lib/paperdesk/current/lib/krita_viewport.so 2>/dev/null | cut -d' ' -f1
printf 'display_loaded='; p=$(pidof xochitl); case "$p" in ''|*' '*) echo no;; *) grep -Fq "$(readlink -f /home/root/.local/lib/paperdesk/current/lib/krita_viewport.so)" /proc/$p/maps && echo yes || echo no;; esac
printf 'pen_node='; test -r /dev/input/event2 && echo yes || echo no
printf 'touch_node='; test -r /dev/input/event3 && echo yes || echo no
"""
        try:
            text = self.transport.run(script, timeout=15)
        except Exception as error:
            raise ArtError("Could not verify art support on the tablet. Reconnect and run setup diagnostics.") from error
        observed = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        if observed.get("firmware") != SUPPORTED_FIRMWARE or observed.get("arch") != "aarch64" or observed.get("xochitl") != XOCHITL_SHA256:
            raise ArtError("Art input is validated only for Paper Pro firmware 3.28.0.172 with the pinned xochitl build. This device is unsupported; no input stream was opened.")
        if observed.get("helper") != _digest(self.assets / "pen-bridge") or observed.get("pen_node") != "yes":
            raise ArtError("Install the art-display feature in the setup wizard before starting art input. The verified PaperDesk pen helper is required.")
        observed["helper_verified"] = True
        if display and (observed.get("display") != _digest(self.assets / "viewport" / "prebuilt" / "krita_viewport.so") or observed.get("display_loaded") != "yes"):
            raise ArtError("The verified art display is not loaded. Run the setup wizard for art-display after a tablet reboot; art mode never installs or restarts the tablet automatically.")
        self._probe = observed
        return observed

    def start(self, application="krita", options=None):
        _application(application)
        options = validate_options(options, self.platform)
        if options["display"] and application != "krita":
            raise ArtError("Canvas display is available with the Krita viewport plugin. Inkscape and Blender support drawing tablet input only.")
        with self._lock:
            if self.status()["active"]:
                raise ArtError("Art mode is already running. Stop it before starting a new session.")
            if self.simulated:
                self._sim_state = {"active": True, "application": application, "options": options,
                                   "message": "Simulation: art session active. No application, native input device or network stream was started."}
                return {"ok": True, "simulated": True, **self._sim_state, "capabilities": self.capabilities()}
            observed = self._device_probe(options["display"])
            if options["touch"] and observed.get("touch_node") != "yes":
                raise ArtError("The validated touch sensor is unavailable. Start with touch disabled, or check diagnostics.")
            if self.platform.startswith("linux"):
                caps = self.capabilities()
                if not caps["linux_driver_installed"]:
                    raise ArtError("Install PaperDesk's optional art dependency: pip install 'paperdesk[art]'. The Linux pen driver uses evdev; no driver is installed automatically.")
                if not caps["linux_input_permission"]:
                    raise ArtError("Linux art input needs writable /dev/uinput. Load the uinput kernel module and grant your user access using your distribution's udev rules. PaperDesk never runs as root or changes permissions automatically.")
            elif self.platform not in ("darwin", "win32"):
                raise ArtError("Native art input is supported on Linux, macOS and Windows.")
            if options["display"] and not self._socket().exists():
                raise ArtError("Enable the reMarkable Viewport plugin in Krita's Python Plugin Manager and restart Krita yourself before starting canvas display.")
            from .art_runtime import ArtSession
            self._session = ArtSession(self.transport, self.platform, options, self._socket(), self._notify)
            try:
                self._session.start()
            except Exception as error:
                self._session.stop()
                if isinstance(error, (ArtError, RuntimeError)):
                    raise ArtError(str(error)) from error
                raise ArtError("Could not start native art input. Check tablet connection and operating system input permissions.") from error
            self._application = application
            self._message = "Art input started. Open your art application to draw." if not options["display"] else "Krita canvas display started."
            state = self._session.status()
            if not state["active"]:
                raise ArtError(state.get("error") or "The input stream ended while art mode was starting.")
            return {"ok": True, "application": application, "capabilities": self.capabilities(), "options": options,
                    "message": self._message, **state}

    def _notify(self, message):
        self._message = message

    def stop(self):
        with self._lock:
            if self._session:
                self._session.stop()
                self._session = None
            self._sim_state["active"] = False
            self._sim_state["message"] = "Simulation: art session stopped." if self.simulated else "Art mode is stopped."
            self._message = "Art mode stopped. Pen and touch released; artwork applications remain open."
            self._launched = [p for p in self._launched if p.poll() is None]
            return {"ok": True, "active": False, "simulated": self.simulated, "message": self._sim_state["message"] if self.simulated else self._message}

    def status(self):
        state = dict(self._sim_state) if self.simulated else (self._session.status() if self._session else {"active": False})
        return {"active": False, "application": getattr(self, "_application", ""), "simulated": self.simulated,
                "message": self._message, "capabilities": self.capabilities(), **state}

    def plugin_install_path(self):
        if self.platform == "win32":
            return Path(self.environ.get("APPDATA", str(self.home / "AppData" / "Roaming"))) / "krita" / "pykrita"
        if self.platform == "darwin":
            return self.home / "Library" / "Application Support" / "krita" / "pykrita"
        argv = self._program("krita")
        if "run" in argv and APPLICATIONS["krita"][1] in argv:
            return self.home / ".var" / "app" / "org.kde.krita" / "data" / "krita" / "pykrita"
        return Path(self.environ.get("XDG_DATA_HOME", str(self.home / ".local" / "share"))) / "krita" / "pykrita"

    def plugin_install_plan(self, destination=""):
        target = Path(destination).expanduser().absolute() if destination else self.plugin_install_path()
        return {"ok": True, "destination": str(target), "files": ["rm_viewport.desktop", "rm_viewport/"],
                "requires_restart": True, "message": "Copy the Krita viewport plugin. Existing plugin files are preserved. After installation, enable reMarkable Viewport in Krita's Python Plugin Manager and restart Krita yourself."}

    def install_plugin(self, destination="", replace=False):
        """Explicit reversible copy; never enables a plugin, edits kritarc or restarts Krita."""
        plan = self.plugin_install_plan(destination)
        target = Path(plan["destination"])
        if self.simulated:
            return {**plan, "simulated": True, "message": "Simulation: would copy the Krita viewport plugin. No user application files were changed."}
        # Refuse symlinked ancestry so a selected path cannot overwrite an unrelated resource.
        if any(path.is_symlink() for path in (target, *target.parents)):
            raise ArtError("Choose a Krita plugin folder without symbolic links.")
        names = ("rm_viewport", "rm_viewport.desktop")
        existing = [target / name for name in names if (target / name).exists() or (target / name).is_symlink()]
        if existing and not replace:
            raise ArtError("A reMarkable Viewport plugin already exists. Keep it, or explicitly choose replacement with backup.")
        if any(path.is_symlink() for path in existing):
            raise ArtError("An existing plugin is a symbolic link. Remove that link yourself before installing a separate PaperDesk copy.")
        source = self.assets / "krita"
        backup = None
        target.mkdir(parents=True, exist_ok=True)
        # Stage on the destination volume so final renames are atomic.
        with tempfile.TemporaryDirectory(prefix=".paperdesk-plugin-", dir=target) as temporary:
            staging = Path(temporary)
            shutil.copytree(source / names[0], staging / names[0], ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy2(source / names[1], staging / names[1])
            if existing:
                backup = self.state_dir / "art" / "plugin-backups" / f"{time.time_ns()}"
                backup.mkdir(parents=True, exist_ok=False)
                for path in existing:
                    if path.is_dir():
                        shutil.copytree(path, backup / path.name, symlinks=True)
                    else:
                        shutil.copy2(path, backup / path.name)
            installed = []
            try:
                for path in existing:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                for name in names:
                    (staging / name).rename(target / name)
                    installed.append(target / name)
            except BaseException:
                for path in installed:
                    if path.is_dir():
                        shutil.rmtree(path)
                    elif path.exists():
                        path.unlink()
                if backup:
                    for original in existing:
                        saved = backup / original.name
                        if saved.is_dir():
                            shutil.copytree(saved, original, dirs_exist_ok=True, symlinks=True)
                        else:
                            shutil.copy2(saved, original)
                raise
        return {**plan, "backup": str(backup) if backup else "", "message": "Krita viewport plugin copied. Enable reMarkable Viewport in Krita's Python Plugin Manager and restart Krita when your work is saved."}
