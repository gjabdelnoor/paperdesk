# Tablet behavioral simulator

`python -m paperdesk --demo` opens a **Simulation** session. Demo mode never
opens SSH, contacts a cloud, discovers a physical tablet, or sends Linux
input events. It creates an isolated directory and synthetic documents.
`TabletSimulator(root, model="paper-pro")` also accepts `rm2` and `rm1`.
Existing non-simulator directories are refused to protect their contents.

This is a contract-level behavioral model, **not a firmware clone, emulator,
or digital twin of the proprietary tablet software**. It contains no
reMarkable firmware, document corpus, credentials, SSH keys, or xochitl
binary. It cannot establish whether a physical device's Qt internals,
kernel drivers, Type Folio matching, pressure response, USB importer,
display, power states, or e-ink refresh work. A successful simulation is
not a claim of hardware validation.

## Modeled behavior

- Fresh developer-mode Paper Pro reports ARM64; reMarkable 1 and 2 report
  ARM32. Files work before helper setup. Keyboard, touch and advanced
  features become available according to the installed manifest.
- Native PDF/EPUB imports generate an import identity, preserve imported
  PDF bytes, and expose a document listing. Generated notebook exports
  and screenshots are marked synthetic. Exports use exclusive creation
  to preserve existing host files.
- Typing, key commands, normalized touch records, page templates, native
  image objects, annotation-copy sync and verified refresh events update
  the synthetic state and operation log. Native image objects retain
  their digest and dimensions; exported fixture PDFs describe them rather
  than reproducing the proprietary notebook scene renderer.
- Reboot removes `/run` and retains `/home` and documents. Advanced loader
  capabilities disappear until explicitly restored through setup.
- Installer operations model staging, checksum verification, atomic
  promotion, prior-install backups, current-boot loader files, recovery,
  app ownership, transaction locks, uninstall archival and rollback.

## Transport and containment

The simulator implements the installer's `run`, `read_bytes`, `write_bytes`
and `exists` boundary. `run` receives the exact installer shell script but
interprets only its JSON `paperdesk-operation` marker. It does **not** run
the script in a shell. Unknown scripts are rejected. Consequently these
tests prove installer decisions and transaction behavior against the
model; real-shell integration and hardware tests are separate evidence.

Absolute tablet paths map beneath the simulator root. Traversal, relative
paths, NUL bytes and symlinks anywhere in a tablet path are rejected.
Reboot checks all descendants before removing its simulated `/run`.
The host upload source and export destination are deliberately supplied
host paths, outside this virtual tablet boundary; exports never overwrite.

State resides in `/.simulator/state.json`; operation events append to
`/.simulator/operations.jsonl`. Neither store contains physical device data.
Call `diagnostics()` for the root, install manifest, faults, boot count and
the simulator's limits.

## Fault injection

`set_fault(name, enabled=True)` supports:

| Fault | Behavior |
| --- | --- |
| `offline` | Transport and tablet operations fail before access. |
| `upload-unknown` | Import commits but loses its receipt; no retry is allowed. |
| `wrong-firmware`, `wrong-hash` | Advanced setup fails before writes. |
| `duplicate-loader` | A conflicting preload/extension is reported. |
| `write-failure` | Every third staging write fails. |
| `manifest-corrupt` | Remote checksum verification fails. |
| `activation-failure` | Service activation fails after promotion. |
| `uninstall-failure` | Uninstall fails after archiving the current install. |
| `restore-failure` | Rollback/restoration cannot be verified. |
| `reboot-during-install` | Activation is interrupted and carries an uncertain-outcome flag. |

Uncertain outcomes set `outcome_unknown` and `no_retry` so the GUI can stop
further mutations until the user inspects the device. The simulator never
automatically replays an import or an uncertain activation.

## Verification

Run `python -m pytest tests/test_simulator.py tests/test_installer.py`.
Coverage includes fresh model architectures, persistent state, native
import/export copies, unknown receipts, keyboard/page/image behavior,
reboot restoration, symlink and traversal confinement, exact pin refusal,
checksums, prior configuration preservation, repeated setup, failed
upgrade/uninstall restoration and interrupted transactions. POSIX shell
syntax checks run on hosts with `sh`; they do not execute tablet commands.
