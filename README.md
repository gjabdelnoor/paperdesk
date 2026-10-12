# PaperDesk

A native Qt Widgets desktop companion that brings reMarkable Type, file transfers,
developer-mode setup and desktop art tools into one workspace. No Electron or browser
runtime. Independent community software; not affiliated with reMarkable.

This project is under active implementation. The first release will be a preview.
Hardware-specific features remain firmware gated. Platform and test results will be
documented with the release rather than inferred from successful builds.

## Run from source

Python 3.11 or newer:

```sh
python -m venv .venv
# Linux/macOS:
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m paperdesk --demo
# Windows PowerShell:
.venv\Scripts\python.exe -m pip install -e '.[dev]'
.venv\Scripts\python.exe -m paperdesk --demo
```

`--demo` uses a disposable simulated tablet. The normal launcher starts disconnected.
It does not enter developer mode, factory-reset a tablet, or install extensions automatically.

The simulator exercises setup and communication behavior. It does not emulate proprietary
firmware, the display controller or physical input devices. Simulation results do not
establish hardware compatibility.

## Development

```sh
QT_QPA_PLATFORM=offscreen python -m pytest
python -m paperdesk --smoke-test --screenshot evidence/gui.png
python packaging/build.py
```

Native bundles are built separately on Linux, Windows and macOS. macOS bundles are unsigned
until a maintainer supplies signing credentials. Source is AGPL-3.0-or-later;
dependency notices and corresponding extension source are included.
