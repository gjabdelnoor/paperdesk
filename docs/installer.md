# Developer-mode helper installer

PaperDesk setup is explicit. The application starts disconnected and does
not enable developer mode, change firmware, configure SSH, install a VPN,
or restore a loader automatically. A physical device must already be in
developer mode and must be paired using the application's SSH host-key
review. The setup page offers a read-only compatibility plan before any
device changes.

The Python API is `DeviceInstaller(transport, state_dir)` with `probe()`,
`plan(features)`, `install(features)` and `uninstall()`. The transport
boundary is synchronous and works with authenticated SSH or the isolated
simulator. Feature IDs are `keyboard`, `touch`, `direct-paste`, and
`art-display`.

## Compatibility

| Model | Architecture | Files | Keyboard | Touch | Native images, page tools, screenshots | Art display |
| --- | --- | --- | --- | --- | --- | --- |
| Paper Pro | `aarch64` | Native importer/exporter | Firmware 3.x + uinput | Firmware 3.x + uinput | Exact pin below | Exact pin below |
| reMarkable 2 | `armv7l`, `armv6l`, `arm` | Native importer/exporter | Firmware 3.x + uinput; ARM32 helper | Unavailable | Unavailable | Unavailable |
| reMarkable 1 | `armv7l`, `armv6l`, `arm` | Native importer/exporter | Firmware 3.x + uinput; ARM32 helper | Unavailable | Unavailable | Unavailable |
| Unknown model/architecture | Observed value | No setup claim | Refused | Refused | Refused | Refused |

Generic keyboard helpers use architecture-correct Linux uinput records;
their binary architecture is verified before staging. **Compatibility
selection is not physical keyboard validation.** ARM32 keyboard support
remains a prerelease path pending physical device testing. Touch coordinates
are specific to Paper Pro. `art-display` can be installed independently of
`direct-paste`.

Both advanced integrations require all of:

- Paper Pro with `aarch64` architecture.
- Firmware `3.28.0.172`.
- `/usr/bin/xochitl` SHA256
  `b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021`.
- No pre-existing foreign `LD_PRELOAD` or `XOVI_ROOT` drop-in in the vendor,
  administrator or current-boot xochitl configuration.
- No symlink in any installation path.

These observations come from the device, not a user-entered model choice.
Unsupported selected features are refused before staging. Compatibility is
probed again immediately before activation. Firmware updates require a new
build and validation; changing only a version label is insufficient.

## Transaction and ownership

Persistent writes are confined to `/home/root/.local/lib/paperdesk`.
The current installation is a **real directory**, not a symlink:

```text
paperdesk/
  current/manifest.json
  current/bin/{injector,touch-injector,figure-ink,pen-bridge,recover-stock}
  current/xovi.so
  current/extensions.d/direct_paste.so
  current/lib/krita_viewport.so
  current/runtime/{99-paperdesk.conf,paperdesk-recover.service}
  backup-<transaction>/...
  uninstalled-<transaction>/...
```

Only selected helpers are present. Keyboard setup on the exactly pinned Paper Pro
also provisions the experimental ink helper; the pen check verifies it and never
installs a helper implicitly. Setup acquires an app-owned
`install.lock`, builds a unique `stage-<transaction>` directory, transfers
files, verifies every transferred SHA256, checks the device again, retains
the old `current` as `backup-<transaction>`, and atomically promotes staging.
The ownership manifest records feature IDs, observed device information,
transaction ID, file hashes and install time. Modified current files or
modified app-named runtime files cause a refusal instead of being replaced.
Backups and uninstall archives are retained for inspection.

Advanced setup writes only two current-boot systemd files:

```text
/run/systemd/system/xochitl.service.d/99-paperdesk.conf
/run/systemd/system/paperdesk-recover.service
```

It preserves other extensions and configuration by refusing conflicting
preloads. No vendor drop-in is overwritten. The app-owned drop-in resets
`OnFailure` to its recovery unit, limits restart behavior, and names only
the selected app-owned libraries. The recovery helper removes PaperDesk's
drop-in and starts stock xochitl; it does not delete documents or modify
vendor configuration. No duplicate copy of the direct-paste extension is
installed in `extensions.d`.

After one xochitl restart, setup checks service activity. Direct paste must
publish a fresh heartbeat matching protocol, firmware, binary hash and
current process identity. Art display must appear in the current xochitl
process's memory maps; its interactive session heartbeat is checked when
art starts. These checks establish runtime loading, not pressure quality,
display quality or physical usability.

## Recovery, reboot and uninstall

`/run` disappears at reboot. Persistent helper files remain, while xochitl
starts with its stock configuration. Restore advanced loading by running
setup explicitly with the desired feature selection. The application
does not silently restart a physical tablet app after reboot.

Failed staging removes that transaction's staging data. Failed activation
restores the retained installation and its prior app-owned runtime files,
then verifies stock or prior service activity. Uninstall archives `current`,
removes only its owned current-boot files, and restores the stock service.
A partial uninstall attempts to restore the prior installation and runtime.
If restoration cannot be verified, the error carries `outcome_unknown=True`
and `no_retry=True`; inspect diagnostics and backups before taking further
actions. A remaining `install.lock` is deliberately not guessed stale or
automatically deleted. Inspect the recorded owner and device state before
manual recovery.

Setup never writes the xochitl document store. The native direct-paste
extension writes its **own request/receipt cache** under
`/home/root/.cache/remarkable-direct-paste` during use and asks xochitl's
native APIs to insert images/pages. The art extension uses its own
`/tmp/paperdesk-viewport` runtime directory. These are runtime feature
operations, separate from helper installation.

## Sources and build provenance

`paperdesk/vendor/tablet` includes the generic injector source, Paper Pro
touch source, complete direct-paste extension source, native-page support,
and XOVI loader source. All shipped ARM helper binaries were built from
the included source. The proprietary reMarkable SDK/Qt/sysroot and xochitl
are **not** included. The SDK is an external build prerequisite.

See `paperdesk/vendor/tablet/README.md`, the included licenses and
`artifacts.json` for reproducible commands, provenance and artifact hashes.
The art helper and viewport sources and build script live under
`paperdesk/vendor/art`. Source and simulator validation can run without the
SDK; rebuilding advanced tablet libraries requires it.
