# Native platform verification

The simulator, mock native APIs, Qt GUI smoke, and operating-system input checks
answer different questions. A successful standalone build or an offscreen Qt
smoke does not establish Windows Ink pressure/tilt delivery. No check here
connects to a tablet, reads a private document, or scans physical devices.

## Windows runtime evidence

Run this command only as a step on a disposable GitHub-hosted Windows runner,
after installing the package and its PySide6 dependencies:

```powershell
python tools/windows_ink_check.py --run-isolated-ci --report windows-ink-report.json
```

The tool requires actual Windows Python, the explicit command flag, `CI=true`,
`GITHUB_ACTIONS=true`, `RUNNER_ENVIRONMENT=github-hosted`, and `RUNNER_OS=Windows`.
It refuses local desktops, self-hosted runners, Linux and macOS before loading Qt
or the input API. These runner variables are defined in the [GitHub Actions
variables reference](https://docs.github.com/en/actions/reference/workflows-and-actions/variables).
Do not override those variables to run it on someone's computer.

The probe reads whether the process window station is interactive and opens and
closes the input desktop with read-only rights. It never switches desktops,
elevates, changes permissions, registers a global input redirection target, or
installs a driver. Windows can refuse desktop access; [OpenInputDesktop documents
the access requirements](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openinputdesktop).

The receiver is one visible Qt Widgets window using the native `windows` QPA
plugin. Native pixel coordinates come from its own client rectangle. Every send
checks that `WindowFromPoint` still identifies the receiver itself. Its native
event filter observes only messages addressed to that HWND and returns false so
Qt can translate them. Qt documents the [Windows MSG event filter
contract](https://doc.qt.io/qt-6/qabstractnativeeventfilter.html).

One synthetic pen produces hover, contact, two contact updates, and final up.
The three contact samples request raw adapter pressures 1024/2048/3072 and tilts
(-30,20)/(0,-15)/(35,30). Expected Windows pressures are 256/512/768; expected Qt
pressures are 0.25/0.5/0.75. Windows defines [pen pressure as 0..1024 and tilt as
-90..90](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-pointer_pen_info).
The [synthetic input API](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-injectsyntheticpointerinput)
must accept the actual adapter's ctypes packets.

API acceptance is recorded separately from receipt. In the native callback,
`GetPointerPenInfo` reads pressure/tilt from each actual `WM_POINTER` message;
the [pointer information must be read while that message is
current](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getpointerpeninfo).
The widget also records `QTabletEvent` pressure/tilt and checks that Qt marks those
events spontaneous. The tool never posts fake Win32 messages or constructs Qt
tablet events. It pumps the event loop after final up while the device handle is
still alive, then destroys the synthetic device in `finally`. Failure paths also
attempt release and destruction. Reports omit native handle values and contain
no credentials or environment dump.

The JSON artifact includes every native pen observation, Qt tablet observation,
injection attempt, the expected sample values, and device creation/destruction
counts. `device_rects_available` helps diagnose Qt's native translation when
Windows reports a pen but Qt does not emit tablet events.

| Status | Meaning | Default exit |
|---|---|---|
| `verified` | Both receivers preserved all pressure/tilt samples, received final up, and the device was destroyed | 0 |
| `unsupported` | The host lacks/refuses the required API or desktop, or no native messages were delivered; runtime remains unverified | 0 |
| `failed` | Receiver values, Qt delivery, final up, cleanup, or the probe itself failed | 1 |

`--require-verified` also returns exit 1 for unsupported hosts. Keep and inspect
the JSON artifact even when a build succeeds. An unsupported report is an honest
capability limit, not a passing pressure/tilt test. The probe establishes delivery
to this Qt receiver; it does not establish Krita/Inkscape/Blender configuration,
multi-touch, or physical reMarkable behavior.

## Portable unit checks

```sh
python -m pytest -q tests/test_native_platform.py
```

All 29 checks use deterministic fake APIs and generated in-memory observations.
They verify fixed-width Win32 types and Win64 structure offsets, CI refusal,
native message decoding, failure handling, final up before destruction, and
rejection of missing or constructed receiver evidence. They can run on Linux,
macOS, or Windows without injecting input. They complement the runtime artifact
and never count as Windows runtime verification.

No real Windows runtime result was collected on the Linux development machine.
Only a `verified` artifact from the hosted Windows step permits that claim.
