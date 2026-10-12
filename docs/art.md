# Art suite

PaperDesk opens Krita, Inkscape and Blender and can open a selected local artwork in those programs. It discovers native installations, Linux Flatpak/Snap installations and application executables on PATH. Portable executables and AppImages can be selected explicitly. Programs are launched with argument arrays; artwork filenames are absolute paths, never shell commands.

Art mode is a separate, explicit action. It opens sensor streams only after the connected tablet's firmware, CPU architecture, xochitl digest and installed pen-helper digest have been verified. Starting art mode does not install software, change host permissions, kill another tablet helper, restart xochitl, repair font caches or launch an art program. If a stream ends, the session releases input and stops. A fresh start is required; a disconnected session does not replay strokes or reconnect automatically.

## Support and validation

| Feature | Linux | Windows | macOS |
| --- | --- | --- | --- |
| Open Krita, Inkscape, Blender; open a local file | Implemented | Implemented | Implemented |
| Native pointer mode | Linux pen device | SendInput mouse mode | Quartz mouse mode, default |
| Native pen pressure / tilt / eraser | evdev/uinput | Windows Ink on Windows 10 1809+ | Experimental, explicit Quartz pen mode |
| Native two-finger touch | evdev/uinput, palm filtered | Windows Ink synthetic touch, palm filtered | Native driver unavailable; Krita display gestures use IPC |
| Krita canvas on the tablet | Implemented with explicitly provisioned viewport extension | Implemented through Qt named-pipe IPC | Implemented through Qt local-socket IPC |
| On-tablet palette, eraser sizes, layer opacity, reference images, undo, pinch/pan/rotate | Original Krita plugin and display bridge retained | Same plugin and display protocol | Same plugin and display protocol; gestures use IPC, without a native touch driver |
| End-to-end validation with this release and a physical tablet | Pending | Pending | Pending |

This release's art sensor profile is restricted to Paper Pro, ARM64, firmware **3.28.0.172**, with xochitl SHA256 `b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021`. Older reMarkable models, Paper Pro Move and other firmware builds are refused for sensor streaming until their input layouts and executable compatibility have been validated. General document/keyboard support has a separate support matrix.

The shared art bridge retains the original pressure curve, rotated tilt, Marker Plus eraser, hover, portrait/landscape rotation, two-finger palm rejection, palette and reference tools. Canvas previews use one authenticated SSH stream and a one-second heartbeat. The viewport uses app-owned `/tmp/paperdesk-viewport` files; after a stopped or lost session, its overlay expires in about five seconds. The tablet helper's input grab is tied to its process and is released when the stream exits. PaperDesk never changes the document store for art mode.

Tests generate input records and fake native API receipts. They verify transforms, native structure layout, pressure and tilt fields, mouse/pen release, touch release, device destruction, explicit installation, preserved plugin originals and command argument handling. Passing those tests proves the protocol and cleanup logic; it does not prove physical stylus latency, panel behavior or pressure receipt in each art application. See the release's platform proof report for any separate native operating-system receiver test.

## Linux input access

Source installs need the optional art dependency: `pip install 'paperdesk[art]'`. The desktop user also needs writable `/dev/uinput`. Your distribution may require loading the `uinput` kernel module and adding a persistent udev rule that grants the intended user access. PaperDesk reports a missing driver or permission and performs no privileged setup automatically. Run the desktop app as your normal user.

Application input support also matters: Inkscape and Blender expose tablet settings separately, and Wayland/compositor support can vary. The capability panel distinguishes a functioning local driver from a provisioned tablet and an active stream.

## Windows Ink

Windows Ink is the default pen backend when the native APIs are present. It injects real Windows pen records with pressure normalized to 0–1024, tilt, hover and eraser flags, plus optional native touch frames. Windows 10 version 1809 or newer is required by Microsoft's [synthetic pointer API](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-injectsyntheticpointerinput). The packet fields and pressure range follow [POINTER_PEN_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-pointer_pen_info).

In Krita, select **Settings → Configure Krita → Tablet settings → Windows 8+ Pointer Input** when using Windows Ink. Other programs need their own Windows Ink support. The input session and target art app must run at the same privilege level. PaperDesk does not elevate itself. If native device creation fails, it reports the failure; it does not silently downgrade pressure to mouse events. The explicit **mouse** mode uses SendInput and has no pressure, tilt or eraser tool identity.

## macOS

The default Quartz mouse backend needs **System Settings → Privacy & Security → Accessibility** permission for PaperDesk. Permission lets the app move and click the pointer during an explicit art session. PaperDesk checks permission without opening a system prompt or changing the setting.

Explicit **pen** mode is experimental. It emits native tablet-proximity events and matching mouse tablet-point subtypes with pressure, tilt and pen/eraser identities. Apple's [tablet event guide](https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/EventOverview/HandlingTabletEvents/HandlingTabletEvents.html) describes that packaging. The implementation follows the normalized tilt values consumed by [Qt's Cocoa tablet path](https://code.qt.io/cgit/qt/qtbase.git/tree/src/plugins/platforms/cocoa/qnsview_tablet.mm?h=6.8). Pressure/tilt fields and proximity teardown are protocol-tested, but genuine pressure receipt in the target macOS application remains unverified. The capability panel keeps verified pressure support disabled for macOS until that evidence exists. No native multi-touch device is implemented. With Krita canvas display enabled, the tablet's two-finger pinch/pan/rotate and palette taps can instead control the plugin directly through IPC. Native pointer/experimental pen input and displayed canvas use the same mapped rectangle.

## Krita viewport plugin

Plugin installation is an explicit local file copy. Its suggested destination follows Krita's native resource directory on Linux, macOS and Windows, or Flatpak's private data directory. A custom resource directory can be selected. Existing plugin files are kept unless replacement with backup is explicitly requested. Replacement stores the complete old plugin in PaperDesk's `art/plugin-backups` state directory. Unrelated plugins are preserved. Symlinked destinations and existing plugin symlinks are refused.

After copying, enable **reMarkable Viewport** in Krita's **Python Plugin Manager** and restart Krita yourself when your artwork is saved. PaperDesk neither enables the plugin nor edits Krita's configuration nor closes the program. Krita documents the [plugin resource folder and enable/restart steps](https://docs.krita.org/en/user_manual/python_scripting/install_custom_python_plugin.html). Some Krita builds do not include Python plugin support.

Canvas mirroring is decoupled from the Linux evdev backend. The local client uses [Qt QLocalSocket](https://doc.qt.io/qt-6/qlocalsocket.html), which selects Unix local sockets on Linux/macOS and named pipes on Windows. It uses bounded synchronous calls inside the owning mirror worker, with cancellation checked at most every 100 ms. Qt networking adds no browser runtime. No local connection or mirror thread runs while art mode is stopped.

The plugin exposes a read-only protocol ping. Starting canvas display requires a compatible reply before creating native input or sensor streams. Windows uses a short, stable per-user pipe identifier and Qt's user-access ACL; Unix uses a user-owned private cache directory. Canvas replies include virtual desktop origin and Windows native window/DPI metadata. The Windows mapper uses the actual client-area origin rather than assuming a monitor starts at zero; pressure and tilt are forwarded independently of that coordinate mapping. Mixed display/DPI configurations still need physical application validation.

The local transport is exercised with a real isolated Qt server in `tests/test_art_ipc.py`: native connection, fragmented binary PNG, multiple requests, bounded frames, mutation timeout without replay, cancellation, complete mirror relay and shutdown. Those tests run on the native operating system in the release matrix and require neither Krita nor a tablet. They prove the local IPC and shared relay; they do not prove Krita's rendering on every platform or the physical panel.

The plugin preserves the original palette, pressure-to-opacity mode, eraser presets, layer opacity controls, reference image docker and reference rotation handles. It communicates through a local user-only socket. The PaperDesk adaptation adds portable socket paths and bounded request/frame sizes and removes the development `exec` request. Plugin installation alone does not imply readiness: native input permission, a compatible local plugin reply and the verified tablet extension are required.

## Simulation and lifecycle

Simulation starts and stops a behavioral art session without opening a program, creating a native input device, installing a plugin, sending SSH commands or changing user application files. The demo's capability information remains explicit about host support.

Stopping a live art session closes its authenticated remote streams, sends mouse-up or pen-up, cancels touch contacts and destroys native input devices. The artwork program remains open so that unsaved work is preserved. Closing/disconnecting the suite must call `stop_art()` before dropping the device transport.

## Developer API

`ArtManager(config, transport=None, simulated=False)` implements `applications()`, `open(application, path='')`, `start(application='krita', options=None)`, `stop()` and `status()`. All results are JSON serializable. `start()` starts sensor input; `open()` launches the selected art program independently.

Extra explicit methods: `set_program_path(application, path)`, `plugin_install_plan(destination='')`, `install_plugin(destination='', replace=False)` and the explicit read-only `probe_plugin()`.

Options include `input_mode` (`auto`, `pen`, `mouse`; alias `mode`), `rotation` (`auto`, `left`, `right`, `portrait`, `inverted`), `aspect` (`auto`, `full`, ratio or number), `pressure`, `curve`, `tilt`, `touch`, `display`, `echo`, `refresh`, `jpeg`, `new_canvas`, `open_path`, `eraser_device`, `palm_size`, `eraser` and `screen_rect` (`x,y,width,height`). `auto` rotation follows the accelerometer while Krita canvas mirroring is active; a pen-only session starts with the top edge pointing left, or uses the selected fixed rotation. `auto` aspect uses a documented 16:10 fallback without touching GUI objects from a worker thread; the UI can supply the intended monitor's ratio. `screen_rect` selects the intended mouse/Windows Ink screen area. Options are validated before any stream or input device is opened.

The transport must already be authenticated and expose `run(script, timeout)` and `stream(script)`. A stream supports `read`, `readline`, `write`, `flush`, `close` and optionally a Paramiko `channel`. `read(size)` returns immediately available bytes rather than buffering to fill the requested size. Credentials are never passed to a local SSH process or command line.

## Open-source artifacts

The Linux transforms, canvas bridge, Krita Python plugin, pen helper and tablet viewport derive from the owner's AGPL-3.0-or-later `remarkable-linux-desktop` source. See `paperdesk/vendor/art/PROVENANCE.json` and the included license. Desktop native adapters are original PaperDesk code written from the operating-system API definitions.

`paperdesk/vendor/art/build-tablet.sh` rebuilds the ARM64 static pen helper and stripped viewport library using an externally installed reMarkable SDK selected with `RM_SDK`. Source and build scripts are included. The package contains no proprietary firmware, xochitl binary, device documents or SDK/system libraries. Compiler prefix maps remove personal build paths from the binaries. The install wizard owns persistent deployment and compatibility checks; no legacy auto-install script is shipped or invoked by art mode.
