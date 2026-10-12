# Preview validation

This release was developed and tested without connecting to a physical tablet,
reading a private tablet document, changing a cloud account or operating the
owner's original TUI. Validation uses generated documents, disposable simulated
tablets, isolated local SSH servers, a network-disabled container and native
GitHub-hosted operating-system runners.

## What the checks establish

| Check | Evidence | Limit |
| --- | --- | --- |
| Desktop UI | Offscreen Qt renders, keyboard/focus/clipboard tests, small-window layout checks and worker cancellation/cleanup | Does not establish accessibility quality or every display/DPI configuration |
| Simulator | Setup, uploads, downloads, PDF export, typing, pages, images, stickers, figure ink, art lifecycle, reboot, recovery and uninstall | Behavioral protocol model; no proprietary firmware, e-ink or hardware input emulation |
| Actual installer shell | Network-disabled disposable Linux container, app-owned transaction paths, checksum checks, retained originals and rollback | Fake service/probe boundaries; no tablet binaries or xochitl execution |
| SSH | Real localhost-only servers: host-key review before authentication, command/stream transfer, SFTP and forwarded native web traffic | No physical USB, real tailnet login or live tablet importer test |
| Native packages | Linux x86-64, Windows x86-64, macOS ARM64 and macOS x86-64 build and frozen-app smoke/self-test | Bundled application operation, not physical tablet compatibility |
| Windows installer | Per-user installation, installed-app smoke and uninstall in a hosted Windows VM | Unsigned publisher; not every Windows configuration |
| Windows pen | Actual WM_POINTER/GetPointerPenInfo and spontaneous Qt tablet events preserve pressure and tilt; final up and device destruction verified | Hosted native receiver, without a reMarkable sensor or Krita/Inkscape/Blender validation |
| Krita canvas IPC | Real isolated Qt local servers on native hosts, fragmented image frames, mapping, cancellation and shutdown | Protocol/relay proof, without Krita rendering or an actual tablet panel |
| macOS package | Frozen startup/self-test, final ad-hoc codesign and strict deep verification; Intel OpenSSL dependency origins checked | No Apple Developer ID/notarization; macOS pen packet receipt remains unverified |

The [native workflow](../.github/workflows/native.yml) publishes per-platform
JUnit results, source and frozen screenshots, frozen self-test results and
platform diagnostics as build artifacts. The release includes a validation
archive and identifies its exact source commit and build run. A build green
check alone is not evidence of physical usability.

## Native Windows pressure proof

The hosted receiver observed pressure samples **256 / 512 / 768** in Windows
and **0.25 / 0.5 / 0.75** in Qt, with tilt samples **(-30, 20), (0, -15),
(35, 30)**. It confirmed native messages, spontaneous Qt events, release and
synthetic-device destruction. The tool never creates fake receiver events.
See [the method](platform-test-method.md) and `windows-ink.json` in the validation
archive for raw receipts. This does not verify physical tablet latency or an
art application's configuration.

## Compatibility gates

Advanced Paper Pro features require model, architecture, firmware and executable
digest observed after authentication. **Firmware 3.28.0.172** and the exact
xochitl SHA256 in [installer.md](installer.md) are required for image/page/screen
extensions and art/figure input. Unknown models, Paper Pro Move and incompatible
firmware are refused. Architecture-correct basic keyboard payloads are included
for reMarkable 1/2; physical keyboard validation is pending.

Linux pen input requires writable `/dev/uinput`. Windows native pen requires a
supported Windows Ink API and the art application's corresponding input mode.
macOS defaults to pointer input with Accessibility permission; its explicit pen
mode is experimental. None of these permissions or art-app settings is silently
changed by PaperDesk.

## Resource use

The application uses PySide6 Essentials and Qt Widgets. It includes no Electron,
Chromium or Qt WebEngine. The settled Linux offscreen sample used **130.75 MiB
RSS** and **0.049% of one CPU core** over **20.396 seconds**. Idle measurements
are short local samples after the
simulator settles, recorded in `benchmark.json`; they are not performance or
battery guarantees. Active canvas streaming and PDF rendering naturally have
different costs.

## Remaining validation

- Physical tablet installation, power recovery, native importer behavior and
  extension loading with this assembled release.
- Physical pen/touch/palm rejection, display behavior, latency and art-app receipt
  on each operating system; Mac pressure support is explicitly experimental.
- Mixed-monitor scaling, native menu behavior, accessibility and supported
  firmware/model expansion.
- Real Tailscale policy/login and userspace tablet connectivity; automatic VPN
  bring-up and transport fallback are intentionally not implemented.

Unknown remote mutations are journaled and never automatically replayed. The
copy pass preserves originals; it is separate from an existing Linux archive
timer's annotation-only incremental workflow. Read-only status integration does
not migrate or reconfigure that timer.
