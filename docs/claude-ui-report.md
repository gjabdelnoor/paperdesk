# Claude UI report

Runtime model identifier: `claude-opus-5-5` (Claude Opus 5.5, Claude Code CLI). Date: 2026-10-12.

## Outcome

The Qt Widgets interface is implemented and its own tests pass. It could **not** be run against
the real `SuiteService`: `paperdesk/backend.py` did not exist when this contract finished, so the
demo path, the real-backend test and the launcher smoke test are unverified. Until that module
lands the window opens in a clearly labelled "Device backend unavailable" state and does nothing.

## Files written

| File | Lines | Purpose |
| --- | ---: | --- |
| `paperdesk/ui/__init__.py` | 4 | Exports `create_window`, `MainWindow`. |
| `paperdesk/ui/main_window.py` | 859 | Window, navigation, menus, banners, connection and pairing flow, simulation switch, operation routing, teardown. |
| `paperdesk/ui/worker.py` | 194 | `OperationRunner`: one worker thread, serialized jobs, duplicate refusal, uncertain-error detection, shutdown. |
| `paperdesk/ui/services.py` | 129 | `ServiceProvider` (where services come from), native dialogs, feature list, error wording. |
| `paperdesk/ui/theme.py` | 136 | Palette and stylesheet. |
| `paperdesk/ui/widgets.py` | 450 | Cards, banners, wrapping layout, section tabs, sidebar button, data tree. |
| `paperdesk/ui/icons.py` | 98 | QPainter sidebar glyphs and notebook page previews. |
| `paperdesk/ui/page_home.py` | 152 | Home. |
| `paperdesk/ui/page_library.py` | 273 | Library. |
| `paperdesk/ui/page_type.py` | 692 | Type: text, quick keys, live keyboard, on-screen keyboard, clipboard, screenshot, pages, touch. |
| `paperdesk/ui/page_art.py` | 249 | Art. |
| `paperdesk/ui/page_setup.py` | 601 | Setup: walkthrough, connect and trust, features, plan and install, recovery, diagnostics. |
| `tests/test_ui.py` | 732 | Offscreen tests with a recording test double of the service API. |
| `docs/ux-design.md` | | Design. |
| `docs/claude-ui-report.md` | | This report. |

No file outside those paths was edited. `tools/gui_capture.py` was not written; screenshots come
from the test suite instead (see below).

## Public surface

- `create_window(service=None, demo=False) -> MainWindow`; a `QApplication` must exist.
- `MainWindow(service=None, demo=False, parent=None, *, provider=None, settings=None)`.
- `window.connection_mode`: `unavailable`, `disconnected`, `connecting`, `live` or `simulation`.
  Also `connected`, `simulated`, `status` (last status dict), `capabilities`, `status_text`,
  `uncertain`, `shutdown()`. `paperdesk/__main__.py` already reads `connection_mode`, `connected`
  and `status`, and those names match.
- The default window builds `SuiteService(DeviceConfig(...))` from remembered address, port, user
  name and key path, and makes no backend call until the user connects.

## Tests run

All with `.venv/bin/python` (Python 3.12.3, PySide6 6.12.0), `QT_QPA_PLATFORM=offscreen`, Linux.

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_ui.py -q` | 17 passed, 2 skipped |
| same with `PAPERDESK_UI_SHOTS=/tmp/paperdesk-shots` | 18 passed, 1 skipped; 30 screenshots at 900×650 and 1180×800 written and inspected |
| `python -m paperdesk --smoke-test` | **Fails**: `{"ok": false, "error": "The bundled simulator did not connect"}`, because `paperdesk.backend` is missing |

Skips: `test_real_backend_demo_when_available` (no `paperdesk.backend`) and the screenshot test
when no directory is given.

The tests use `FakeService`, a test double inside `tests/test_ui.py` whose return shapes copy
`simulator.py`, `installer.py`, `art.py` and `transport.py` as they stood. They cover: default
start makes zero backend calls; demo reads Simulation in banner, title, Home and sidebar and
launches no art application; every page and section is reachable at 900×650; completion and
failure text; `ok: false` shown as "Not completed"; an unknown mutation is called once, holds
further changes and still allows reads; one operation at a time on a non-GUI thread with
duplicates refused; live keys keep order and drop keys behind a failure; pairing order
(connect refused, probe, trust, connect) and one-step reconnect; the password absent from
settings, log and `repr`; a changed host identity refused with no trust control; unsupported
capabilities disabled and enabled after install; installer refusal blocking Install; touch as one
down and one up; art mode stopped before disconnect on close; connection loss without reconnect;
teardown with work in flight.

## Unresolved backend integration

Written against `ARCHITECTURE.md` and the modules present; each of these needs checking once
`SuiteService` exists.

1. **`probe_host_key` / `trust_host_key`** are called on the service instance as
   `service.probe_host_key(config)`. That works for a static, class or instance method taking
   `config`, and fails if `SuiteService` does not expose them at all.
2. **Pairing detection.** The UI calls `connect()` first and treats an exception whose class
   name contains "trust" (`HostTrustRequired`) as "pair first". It decides "identity changed"
   from the word "changed" in that message, since the exception carries no flag. A reworded
   message would send a changed host down the first-time pairing path. A `changed` attribute on
   the exception would remove the guess; the UI already honours one.
3. **Unknown mutation** is recognised by class name (`UnknownMutation`, `UnknownMutationError`,
   or names containing "uncertain", "unknownoutcome", "indeterminate") or by attributes
   `uncertain` / `retry_safe=False`. If `SuiteService` wraps these in a differently named
   exception, the failure shows as an ordinary error with no hold.
4. **Capabilities.** Keys are lower-cased with `_` read as `-`. `keyboard`, `touch`,
   `direct-paste`, `art-display` must be `True` to enable their controls. `upload`, `download`,
   `export`, `sync`, `screenshot`, `pages`, `refresh` disable their controls only when explicitly
   `False`. Pressure and tilt come from the `capabilities` in `start_art`'s result, or
   `art_pressure` / `art_tilt` in status.
5. **Touch** is not in the `SuiteService` API. The surface is enabled only if the service has a
   callable `touch(phase, x, y)` (as the simulator does) or `tap(x, y)`, and sends taps only.
6. **Art options** sent are `rotation` (`auto`/`portrait`/`left`/`right`/`inverted`), `pressure`,
   `curve`, and `display: True` for Krita when ticked, matching `art.validate_options`.
7. **`download` / `export_current` / `screenshot` destinations** are passed as new file paths
   from a save dialog. Downloads are suggested as `.pdf` unless the type is `epub`.
8. **`refresh()`** is presented as "Redraw tablet screen", inferred from the simulator's
   suspend/wake wording.
9. **Simulator pairing.** Demo start calls `connect()` directly. Setup › Connect in simulation
   tries `probe_host_key` first and falls back to a direct connect if the demo service has none
   or it fails, so the fingerprint step is reviewable only if `SuiteService.demo()` supports it.

## Gaps

- Not exposed, because `SuiteService` has no call for them: choosing an art application's
  executable (`ArtManager.set_program_path`), Krita plugin installation, listing which
  applications are installed, forgetting a remembered host identity, opening a specific tablet
  document, swipes.
- Live keyboard forwards plain key presses, Enter, Tab, Escape, Backspace, arrows and
  Ctrl+C/V/W. Dead keys and input methods are not forwarded; the page says so.
- Verified only offscreen on Linux. Not checked: a real display, high-DPI scaling, macOS and
  Windows native menus and dialogs, screen readers. Controls have accessible names, but no
  assistive technology was run.
- The menu bar keeps the platform's own colours rather than the ecru palette, deliberately.
- Closing the window waits up to 8 seconds for an operation in flight; a backend call that never
  returns leaves its thread parked rather than killed.
- Remembered address, port, user name, key file path and sync folder go to `QSettings`
  (`PaperDesk/PaperDesk`). The password never does.
