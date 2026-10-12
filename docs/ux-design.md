# PaperDesk UX design

PaperDesk is a calm paper workspace: it shows what is true, does one thing at a time, and never
acts on a tablet unless asked. This document records the structure and the decisions the
implementation in `paperdesk/ui/` follows.

## Who it is for

Someone who owns a reMarkable tablet that is already in developer mode, is comfortable with a
desktop application, and should never need a terminal. They want to move documents, type from a
real keyboard, and draw in Krita. They are wary of breaking the tablet, so the interface has to
say plainly what it will change and what it will not.

## Information architecture

A fixed sidebar with five places, a status line that is always visible, and banners for states
that must not be missed.

| Place | What lives there |
| --- | --- |
| **Home** | Connection state in one sentence, connect / first-time setup / simulator, tablet facts, what this tablet can do, links onward. |
| **Library** | Tablet documents (filter, sort), upload with the native importer, download, export the open document as PDF, local folder sync. |
| **Type** | *Write* (send text, quick keys, live keyboard), *Keyboard* (clickable US layout), *Clipboard and capture* (clipboard text and image, screenshot to clipboard or file), *Pages* (Cornell, grid, lined, blank), *Touch*. |
| **Art** | Krita / Inkscape / Blender, open application or file, tablet mode start and stop, rotation, pressure and curve, canvas display, reported capabilities. |
| **Setup** | 1 Developer mode · 2 Connect · 3 Features · 4 Review and install · Recovery · Diagnostics. |

Menus mirror the same actions (File, Tablet, View, Help) with shortcuts: Ctrl+1–5 for pages,
Ctrl+K connect, F5 refresh status, Ctrl+U / Ctrl+D / Ctrl+E for upload, download, export,
Ctrl+Shift+S / Ctrl+Shift+C for screenshots, Ctrl+L for the activity log.

## First run, tablet freshly in developer mode

1. The app opens on Home, disconnected, with three choices: **Connect to tablet**,
   **First-time setup**, **Try the simulator**. Nothing has contacted anything.
2. *First-time setup* opens Setup › Developer mode. It states that developer mode is expected to
   be on already and that PaperDesk never resets a tablet to enable it, then gives four steps:
   plug in and wake, find the password on the tablet, connect, choose extras or none.
3. Setup › Connect has the address (prefilled `10.11.99.1`), user name (`root`), a password
   field and an optional key file. The password is typed here, never in a terminal.
4. *Check tablet identity* tries to connect. An unpaired tablet is not an error: PaperDesk reads
   its SHA256 fingerprint and shows it under "Is this your tablet?".
5. The user ticks "plugged directly in, or I compared the fingerprint" and presses **Trust this
   tablet and connect**. Home now names the tablet; Library works immediately.
6. Optional: Setup › Features to tick extras, **Review installation plan**, read what the
   installer would change and whether this firmware is allowed, then **Install** and confirm.

Anyone without a tablet at hand presses **Try the simulator** and can walk the same path.

## Visual system

- **Palette.** Ecru ground `#F3EEE2`, paper cards `#FCFAF4`, ink text `#22201C`, muted text
  `#625C51`, hairlines `#D6CDB9`. One accent, ink blue `#27496D`, is used only for the primary
  action, selection and focus. Green, ochre and red appear only as state colours and always next
  to words that say the same thing.
- **Type.** The system font throughout; sizes are multiples of the system size (title ×1.8,
  heading ×1.18, caption ×0.92). Fingerprints use the system fixed-width font.
- **Shape.** Cards with a 10 px radius and one hairline; 14 px between cards, 28 px page margins.
  One primary button per card at most.
- **Icons.** Five sidebar glyphs and the notebook page previews are drawn with QPainter on a
  20-unit grid, so they are sharp at any scale. No emoji, no bitmap assets.
- **Selection and focus.** The selected page has a paper pill, an accent bar and bolder text.
  Keyboard focus draws a 2 px accent ring; mouse clicks do not leave rings behind.
- **Motion.** None. No spinners, no timers, no polling; the idle application does nothing.
- **Reflow.** Button rows and section tabs wrap; every page scrolls vertically. Designed at
  900 × 650 and upward.

## Decisions

**Trust.** The app starts disconnected and connects only on request. A tablet's identity is shown
as the fingerprint the backend observed, with the address and key type, before any password is
sent. Trusting takes a tick and a click on a button that says what it does. A tablet that is
already trusted connects in one step. If the remembered identity no longer matches, PaperDesk
refuses and offers no override; it explains that a factory reset is the innocent cause. The
password is held in memory only: it is not written to settings, the log or the status line.

**Simulation.** A dark full-width bar reading "Simulation" stays on every page, the window title
and the sidebar chip repeat it, and Home's title starts with it. Screenshots and status in this
mode are labelled as simulator output. Entering the simulator never touches the real-tablet
service; leaving it returns to a disconnected state.

**One thing at a time.** Every backend call runs on one worker thread, in order. A request
identical to one already running or waiting is refused with "Already in progress", not queued.
The status line names the operation in progress, how many are waiting, and then the backend's own
message. A result with `ok: false` is reported as "Not completed", never as done.

**Uncertain operations.** When the backend says a change may or may not have been applied, a red
banner states "Outcome unknown", names the operation, and says it will not be retried. Further
changes are held until the user dismisses the banner; reading state (status, document list)
stays available so they can check first. Operations waiting behind any failure are cancelled and
said to be cancelled.

**Connection loss.** After a failed operation PaperDesk asks for status once. If the tablet is
gone, the state becomes disconnected, a banner says nothing was retried and nothing is
reconnecting, and offers Reconnect. There is no automatic reconnection and no background polling.

**Capabilities.** Advanced extras (keyboard, touch, direct-paste, art-display) are usable only
when the tablet reports them; otherwise the controls are disabled with a sentence saying why and
where to look. Document actions stay available unless the tablet explicitly reports them off.
Firmware gates are explained beside the feature checkboxes, and a plan the installer refuses
disables Install and shows the installer's reasons per feature.

**Installing.** Nothing is installed without a reviewed plan for exactly the current selection
plus a confirmation dialog. Changing the selection invalidates the plan. Uninstall lives under
Recovery, next to plain-language advice for the four things that can go wrong.

**Art-mode exit.** While tablet mode runs, a bar with **Stop tablet mode** appears on every page.
Disconnecting or closing the window stops it first. If stopping cannot be confirmed, the page
says so and gives the fallback: restart the tablet, because tablet mode uses only temporary
settings. Where the art bridge reports pointer-only input, the page says pressure and tilt are
not available rather than implying support.

**Typing.** Text is sent in one piece by default. Live keyboard is a separate mode that must be
started, is visibly "Live", and ends when focus leaves or on Shift+Esc. Keys keep their order,
and a failed key discards the ones behind it. The page says which characters fall outside a US
keyboard before anything is sent.

**Files.** Native open and save dialogs start in the user's Documents or Pictures folder.
Downloads, exports and screenshots are new files only; choosing an existing name is refused.
