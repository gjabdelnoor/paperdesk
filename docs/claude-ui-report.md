# Claude UI report

Runtime model identifier: `claude-opus-5-5` (Claude Opus 5.5, Claude Code CLI). Date: 2026-10-12.
Covers the interface as it stands after `contracts/CLAUDE_INTEGRATION.md` and the layout repair
in `contracts/CLAUDE_WINDOWS_LAYOUT.md`.

## Home reflow under larger fonts (Claude, `contracts/CLAUDE_HOME_LAYOUT.md`)

**Failure.** Native Windows CI at `d54f652`: the three font-stress cases failed on Home only
(content 782 of 690 pixels at 1.2×, 876 at 1.35×). The baseline 900 × 650 test passed.

**Cause.** One row. Each line of "What this tablet can do" was a dot, a one-line name, a
stretch and a one-line status, so the card could never be narrower than its longest name plus
its longest status: "Documents, upload, download and PDF export" beside "Connect to find out".
With Linux fonts that card needs 439 pixels (495 with page margins), which is why Linux passed.
The Windows runner's fallback font draws wider glyphs. Widening every glyph by half on Linux
puts the same row at 771 pixels at 1.2× and 863 at 1.35×, close to the CI figures. No other
Home card was near the limit.

**Fix (`page_home.py` only).** Both captions in a capability row now wrap. The name takes the
spare width and wraps first; the status keeps to one line up to 230 pixels and wraps beyond
that. No wording changed, nothing is truncated, and the scroll bar policy and minimum window
size are untouched. At ordinary metrics the page looks as it did.

**Home at 900 pixels after the fix** (content minimum of 688–690 available; Linux, offscreen):

| Style, font scale, glyph width | Connected (simulator) | Disconnected (live) | Old row alone |
|---|---|---|---|
| default, 1.0×, normal | 291 | 302 | 495 |
| default, 1.2×, 150 % | 414 | 435 | 771 |
| Fusion, 1.35×, 150 % | 455 | 479 | — |
| Windows, 1.35×, 150 % | 455 | 479 | 863 |
| Fusion, 1.6×, 150 % | 503 | 516 | 1003 |

Card minima at Windows 1.35× with 150 % glyphs: Tablet 355, What this tablet can do 399 to 423,
Where to next 292 (add 56 for page margins). The widest single item left on Home is the
"Redraw tablet screen" button.

**Checks run.** Full suite on Linux, screenshots enabled: 312 passed, including the three
unchanged stress cases and the unchanged 900 × 650 test. `tests/test_ui.py` was not edited.
The widened-glyph figures come from a scratch measuring script, not from a test in the
repository. Home was reviewed by eye at 900 × 650 at ordinary metrics (connected) and at
1.35× with widened glyphs (disconnected), and at 1.0× with widened glyphs (connected).

**Limits.** Nothing here ran on Windows; native CI is root's acceptance gate. Widened letter
spacing imitates a wide fallback font and is not that font. With very wide glyphs the dot sits
a pixel or two above the first line of a wrapped name.

## Windows 900 × 650 layout repair (Claude)

**Failure.** On native Windows CI,
`test_no_page_needs_sideways_scrolling_at_the_smallest_supported_size` reported Type › Write as
wider than a 900 pixel window.

**Cause.** Write was not the wide pane. Type's sections sit in a stack, and a stack takes the
minimum width of its widest pane whether or not that pane is showing. The widest was Figure as
ink: its placement row put four captions and four spin boxes side by side in a layout that
cannot wrap, and its one-line check box could not shrink either. Measured on Linux at 900
pixels wide, the page had 690 pixels of room and needed 620, of which the ink pane was 564; so
there were 70 pixels to spare, and Windows fonts and spin boxes used them up. The test names
Write only because it is the first Type section it visits. Art was as close (629 of 690),
because of three one-line check boxes, and would have been the next failure.

Reproduced on Linux with the Windows widget style and a font 1.35 times larger: Type needed
757 of 688 pixels (ink pane 701) and Art 799, its widest item a 705 pixel check box.

**Fix.** Wrapping, not a wider window and not a hidden scroll bar.

- `widgets.py`: `WrapCheckBox`, a check box whose sentence wraps onto further lines, with the
  same API, keyboard handling and native indicator as a plain one, and the whole label
  clickable. `pair()` keeps a caption with its control as one item of a wrapping row.
- `page_ink.py`: the placement and tracing rows are wrapping rows of caption-and-control pairs;
  the inspected-mark confirmation is a `WrapCheckBox`.
- `page_art.py`: the macOS pen acknowledgement, touch, plugin-enabled and canvas-display boxes
  are `WrapCheckBox`.
- `page_setup.py`: the trust confirmation is a `WrapCheckBox`.
- `page_type.py` was not edited. No input, clipboard, touch, worker or teardown code changed.

**After.** Same stress (Windows style, 1.35×): Type needs 479 of 688 pixels, Art 452. At 1.6×
with Fusion every page still fits; the tightest is Home at 598 of 690.

**Tests.** The 900 × 650 test keeps its assertion unchanged; on failure it now also prints a
bounded report (platform, style, font, viewport, every pane's minimum width including those
stacked out of sight, the widgets that will not shrink) and saves a picture under
`evidence/local/layout-failures/`, or `evidence/layout-failures/` when `CI` is set. New:
`test_pages_fit_900_pixels_with_larger_fonts_and_other_widget_styles` (default style at 1.2×,
Fusion and Windows styles at 1.35×; it requires both no sideways scroll range and a content
minimum within the viewport) and `test_long_check_boxes_wrap_and_still_work`. The stress test
was not run against the code before this repair; the before figures above come from a
measuring script at the same style and font. Full suite on Linux: 309 passed, 1 skipped (the optional
screenshot test); UI files with screenshots enabled: 60 passed. The counts in the Verification
table further down are from the integration pass and are older.

**Limits.** This was diagnosed and verified on Linux only. Qt's "Windows" style here is the
classic one, not the native Windows 11 style, and the font was scaled rather than replaced by
Segoe UI, so the stress is an approximation with margin, not a reproduction. The native CI run
is the proof. `WrapCheckBox` paints its own indicator and text through the platform style; that
was checked by eye on Linux in unchecked, checked and disabled states, and not on Windows or
macOS. The "Placement" and "Tracing" captions now sit at the top of their rows, a few pixels
above the centre line of the controls.

## Maintainer integration after Claude's review

The final integration adds real synthetic folder records (including an empty
folder), so the simulator exercises the same UUID destination path as a real
inventory. Sticker uploads use the Library destination. The first setup step
now distinguishes Paper Pro developer mode from reMarkable 1/2 SSH access, and
the empty-address Tailscale message asks for an address rather than a password.
These are maintainer changes after the Claude contract, without a visual redesign.

The assembled source passed 311 tests, with one optional screenshot-export test
skipped. A separate Linux desktop launch used the active native X11 display and
closed cleanly after capturing its simulated Home screen. This does not establish
physical tablet behavior or accessibility quality. Final platform package results
are recorded with the release.

Library also exposes the backend's uniquely most-recent export fallback when the
active-page extension is unavailable. Copy passes stop between files after a
cancellation, preserving already confirmed copies and starting no further imports.

## Outcome

The Qt Widgets interface runs end to end on the genuine `SuiteService.demo` simulator and the
full test suite passes. The integration pass added real tablet folders to the upload choice,
RMDOC upload, the copy-pass wording, a read-only archive status card, an explicit Tailscale
connection type, a plugin check on the Art page, corrected clipboard shortcuts, and first
connection notes. Nothing in this work touched a physical tablet, a network address, Tailscale,
Krita or the owner's archive timer.

## Who did what

**Claude (this report's author): design and page-level interface.** The information
architecture, visual system, wording, and the pages `page_home.py`, `page_library.py`,
`page_art.py`, `page_ink.py`, `page_setup.py`, `page_sandbox.py`, plus `widgets.py`, `theme.py`,
`icons.py`, `tests/test_ui.py`, `docs/ux-design.md` and this report.

**Root: safety and integration.** Operation routing, cancellation and teardown in
`main_window.py`; `worker.py`; the functional input in `page_type.py` (live keyboard, clipboard
paste, touch drag); `tests/test_ui_lifecycle.py`; the backend, transport, installer, art bridge,
archive status reader, packaging, README and release. Root also corrected
`test_worker_teardown_is_clean_even_with_work_in_flight`; it is unchanged here.

In `main_window.py` this pass edited only the `SHORTCUTS` list and the `transport_mode` line in
`saved_config` and `_save_connection`, as the contract allowed.

## Public surface

`paperdesk/ui/__init__.py` exports exactly `MainWindow` and `create_window`.

- `create_window(service=None, demo=False) -> MainWindow`; a `QApplication` must exist.
- `MainWindow(service=None, demo=False, parent=None, *, provider=None, settings=None)`.
- Read by `paperdesk/__main__.py` and the tests: `connection_mode` (`unavailable`,
  `disconnected`, `connecting`, `live`, `simulation`), `connected`, `simulated`, `status`,
  `capabilities`, `status_text`, `uncertain`, `shutdown()`.
- The default window makes no backend call until the user asks for something.

## What the interface exposes

| Place | Tools |
| --- | --- |
| **Home** | Connect, first-time setup, simulator, disconnect, refresh status, redraw tablet screen; tablet facts; per-capability readiness. |
| **Library** | Documents and folders with filter and sort; upload PDF, EPUB or RMDOC into a chosen tablet folder; download; export the open document as PDF; copy pass; archive sync status. |
| **Type › Write** | Send text, quick keys, live keyboard. |
| **Type › Keyboard** | Clickable US layout. |
| **Type › Clipboard and capture** | Clipboard text, clipboard image paste, image as a native sticker notebook, screenshot to clipboard or file. |
| **Type › Pages** | Cornell, grid, lined, blank. |
| **Type › Touch** | Touch surface (taps, and root's drag and keyboard placement). |
| **Type › Figure as ink** | Choose figure, prepare preview, 1 check pen, 2 calibration mark, 3 draw, stop. Experimental. |
| **Art** | Application choice with what was discovered, choose executable, look again, open application or file; tablet mode start and stop; input method; rotation; pressure, curve, tilt; eraser; touch; Krita canvas display; review plugin copy, copy plugin, replace keeping a backup, check plugin; six capability rows. |
| **Setup** | Developer mode guide; connect (connection type, address, trust) with first-connection notes; features; review and install; recovery and uninstall; diagnostics; a simulation-only sandbox. |

Menus (File, Tablet, View, Help) repeat these. Shortcuts: Ctrl+1–5, Ctrl+K, Ctrl+Shift+K, F5,
Shift+F5, Ctrl+U, Ctrl+D, Ctrl+E, Ctrl+I, Ctrl+G, Ctrl+Shift+S, Ctrl+Shift+C, Ctrl+Enter,
Ctrl+L, F1. In the live keyboard: Ctrl+C / Ctrl+P / Ctrl+W act on the tablet, Ctrl+V and
Ctrl+Shift+V (Cmd+V on macOS) paste this computer's clipboard.

## Integration pass, item by item

1. **Upload folders** (`page_library.py`). "Upload into" is built from the tablet's rows of type
   `folder`: the name is the label and the folder id is the value passed to `upload`. Empty
   folders are therefore selectable. Names that occur twice show the first eight characters of
   the id. One folder named Inbox is the default; two are ambiguous and Top level stays
   selected. A choice the user made survives reloads. Folder names mentioned by documents are
   used only when a service reports no folder rows at all, which is the case for the simulator
   and the test double. Folder rows are listed, counted separately, and cannot be downloaded.
   RMDOC is in the file filter, the tooltip and the shortcut list.
2. **Archive status.** A card titled "Existing archive sync" with one button, Refresh status.
   It calls `archive_sync_status()` once per press with `needs_connection=False`, never on a
   timer and never at start. It words available, active, large, stale, unreadable and absent,
   shows bytes done of total with a percentage when given, and adds the USB-C sentence for a
   large active record if the backend's message did not already say it. A stale record is never
   shown as active and shows no progress. The card is hidden in simulation and the method is
   not called there.
3. **Copy pass.** The card is titled "Copy pass" and the button "Run copy pass". The text
   states the two halves of the pass, that nothing is deleted on either side, that it does not
   keep the two in step, and that it is separate from any archive timer. The result line shows
   the backend's message, the counts and the folder the tablet copies went to.
4. **Tailscale** (`ConnectPanel`, `saved_config`, `_save_connection`). A "Connect by" selector
   with "USB / network SSH" and "Tailscale SSH". Tailscale disables the password, Show, key
   file and Choose controls and sends an empty password in `DeviceConfig`. The note says it
   uses the installed, signed-in client, never installs or starts Tailscale on the tablet,
   never opens a sign-in page, never switches type by itself and never looks the address up.
   The fingerprint step is kept with wording that does not rely on a cable. Only the type is
   added to what is remembered; a missing or unknown saved value means SSH.
5. **Plugin check** (`page_art.py`). "Check plugin" runs `art_plugin_probe()` once on the
   worker with no connection needed and shows the bridge's reply or its error. It never polls
   and never ticks the user's "I enabled it" confirmation. The platform guess noted in the
   previous report is no longer needed: the bridge now reports `platform`, which the page
   already preferred.
6. **Shortcuts.** The list now has Ctrl+C / Ctrl+P / Ctrl+W for the tablet and a separate line
   for Ctrl+V / Ctrl+Shift+V / Cmd+V pasting this computer's clipboard. `page_type.py` was not
   edited.
7. **First connection.** A card under the Connect form explains Settings › Storage › USB web
   interface and that reMarkable 1 and 2 have no developer-mode switch, with reMarkable's
   developer-mode page as selectable text, not a link the app opens. It does not mention
   firmware updates, resets or remounting.
8. **Teardown test.** Left exactly as root corrected it.

## Verification

`.venv/bin/python` (Python 3.12.3, PySide6 6.12.0), `QT_QPA_PLATFORM=offscreen`, Linux.

| Command | Result |
| --- | --- |
| `PAPERDESK_UI_SHOTS=evidence/local/final-claude … -m pytest tests -q -p no:cacheprovider` | 288 passed |
| `… -m pytest tests/test_ui.py tests/test_ui_lifecycle.py -q -p no:cacheprovider` (same variable) | 56 passed |
| `… -m paperdesk --smoke-test` | `{"ok": true, "platform": "linux", "gui": "Qt Widgets", "simulated": true}` |

Root was adding tests elsewhere in `tests/` while this pass ran, so the total moved between
runs (283, then 288); every run passed.

New tests in `tests/test_ui.py`:

- `test_upload_folders_are_the_tablets_real_folder_rows`: exact labels and ids, Inbox default,
  the id forwarded to `upload`, selection kept after reload, folder rows not downloadable, two
  Inboxes not guessed, name fallback only without folder rows.
- `test_copy_pass_wording_and_archive_status_is_read_only_on_request`: wording, card hidden and
  uncalled in simulation, one read per press while disconnected, large, stale, unreadable,
  absent, idle and failing reads.
- `test_tailscale_is_an_explicit_choice_that_never_sends_the_password`: SSH by default,
  disabled fields, empty password in the config that reaches the service, fingerprint step
  kept, mode persisted without the password, unknown saved mode falls back to SSH.
- `test_plugin_check_shortcuts_and_first_connection_notes`.
- `test_shortcuts_and_sync_result_are_visible` extended for the copy-pass result line.

### What kind of evidence each check is

**Genuine backend, simulated tablet.** `test_full_path_through_the_window_on_the_real_backend`,
`test_real_backend_refuses_extras_on_a_fresh_remarkable_2`,
`test_real_backend_demo_when_available` and the 900 × 650 and 1180 × 800 page screenshots use
`SuiteService.demo` in a temporary folder with `socket.create_connection` and
`subprocess.Popen` blocked. The simulator reports no folder rows and hides the archive card, so
it exercises the name fallback, not the new folder path.

**Recording test double.** Folder rows, the archive card, Tailscale mode, the plugin check and
the per-platform art options are tested against `FakeService` in `tests/test_ui.py`. This shows
the interface forwards and words things correctly. It does not show that Tailscale connects,
that the archive status file is read correctly, or that Krita answers; root's own tests cover
the transport and the status reader.

**Hardware.** None. No physical tablet, pen, Tailscale node or art application was involved.

**Native packaging.** Not covered here. These are source checks under offscreen Qt on Linux;
proof that the packaged Windows, macOS and Linux builds start and behave is root's and comes
later.

### Screenshots

53 PNGs in `evidence/local/final-claude/`. The page shots at both widths come from the real
demo. Twelve `*-live-*` shots come from the test double, to show the not-simulated wording:
Connect in SSH and Tailscale mode, the first-connection card, and Library with folder rows and
an archive status. Opened and checked by eye in this pass: Connect in Tailscale mode at both
widths, the first-connection card at 900, Library with folders at 900, the lower half of
Library at both widths and in simulation at 900, and the Art plugin card at 900. The rest were
regenerated and not individually inspected. All are offscreen Qt renders, not hardware tests.

## Notes for root

1. **Developer mode step is unchanged.** Ownership limited `page_setup.py` edits to
   `ConnectPanel`, so the USB web interface and reMarkable 1/2 notes are on step 2, Connect.
   Step 1 is still titled "Your tablet should already be in developer mode", which does not fit
   a reMarkable 1 or 2. Rewording it is a text-only change outside what this pass could edit.
2. **`request_connect` message.** With an empty address in Tailscale mode, the menu and Home
   path still says "Enter the tablet's password or choose a key file". That string is in
   `main_window.py` outside the permitted lines. The Connect page itself words it correctly.
3. **Simulator has no folder rows**, so the real-backend tests cannot reach the folder-id path.
   Passing a folder id to the simulator's `upload` would file the document under that id as a
   name; the name fallback avoids this today.
4. **Ink refusals hold further changes**, as noted before: ink jobs run as possibly-partial, so
   a clean refusal still raises "Outcome unknown" until dismissed.
5. **Setup's blank space below short sections** is from the section stack taking the height of
   its tallest section. It predates this pass and lives in `widgets.py`; it is visible as a
   long scroll range on Connect.
6. **`UNICODE_NOTE`** in `page_type.py` was reworded in the previous pass; nothing in that file
   changed in this one.

## Genuine limits

- Verified only offscreen on Linux. macOS and Windows wording and enablement were exercised
  through test-double capabilities, never on those systems.
- The statement that a reMarkable 1 or 2 already lists its password under Settings › Help ›
  Copyrights and licenses was written from general knowledge and not checked on a device.
- Not checked: a real display, high-DPI scaling, native menus and dialogs on macOS and Windows,
  screen readers. Controls carry accessible names, but no assistive technology was run.
- Dead keys and input methods are not forwarded by the live keyboard; the page says so.
- Closing with an operation in flight keeps the window open, says it is waiting, and closes
  once the operation and cleanup finish. Nothing is killed.
- Connection type, address, port, user name, key file path and copy-pass folder are remembered
  in `QSettings`. The password never is.
