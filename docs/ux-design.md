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
| **Library** | Tablet documents and folders (filter, sort), upload PDF, EPUB or RMDOC into a chosen tablet folder, download, export the open document as PDF, the copy pass, and a read-only status line for an archive timer that already exists on this computer. |
| **Type** | *Write* (send text, quick keys, live keyboard), *Keyboard* (clickable US layout), *Clipboard and capture* (clipboard text and image, image as a sticker notebook, screenshot to clipboard or file), *Pages* (Cornell, grid, lined, blank), *Touch*, *Figure as ink* (experimental). |
| **Art** | Krita / Inkscape / Blender with what was found on this computer, open application or file, tablet mode start and stop, input method, rotation, pressure, curve and tilt, touch, the Krita canvas on the tablet and its plugin, reported capabilities. |
| **Setup** | 1 Developer mode · 2 Connect · 3 Features · 4 Review and install · Recovery · Diagnostics · Sandbox (simulation only). |

Menus mirror the same actions (File, Tablet, View, Help) with shortcuts: Ctrl+1–5 for pages,
Ctrl+K connect, Ctrl+Shift+K disconnect, F5 refresh status, Ctrl+U / Ctrl+D / Ctrl+E for upload,
download, export, Ctrl+I image as sticker, Ctrl+G figure as ink, Ctrl+Shift+S / Ctrl+Shift+C for
screenshots, Ctrl+L for the activity log, F1 for the shortcut list. In the live keyboard,
Ctrl+C / Ctrl+P / Ctrl+W copy, paste the tablet's own clipboard and close the document on the
tablet, while Ctrl+V and Ctrl+Shift+V (Cmd+V on macOS) paste this computer's clipboard text or
image. The two clipboards are listed on separate lines so they are not confused.

## First run, tablet freshly in developer mode

1. The app opens on Home, disconnected, with three choices: **Connect to tablet**,
   **First-time setup**, **Try the simulator**. Nothing has contacted anything.
2. *First-time setup* opens Setup › Developer mode. It states that developer mode is expected to
   be on already and that PaperDesk never resets a tablet to enable it, then gives four steps:
   plug in and wake, find the password on the tablet, connect, choose extras or none.
3. Setup › Connect has the connection type, the address (prefilled `10.11.99.1`), user name
   (`root`), a password field and an optional key file. The password is typed here, never in a
   terminal. Below the form, "Before a tablet's first connection" says to switch on Settings ›
   Storage › USB web interface, which uploading and exporting need, and that reMarkable 1 and 2
   have no developer-mode switch; it gives reMarkable's own developer-mode page as plain text.
   It never suggests a firmware update, a reset or remounting anything.
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

**Connection type.** A selector with two choices, "USB / network SSH" and "Tailscale SSH". It is
never changed for the user, there is no fallback from one to the other, and the address is never
looked up. Choosing Tailscale disables the password and key fields, because that mode uses the
Tailscale client already signed in on this computer and sends neither. The note beside it says
PaperDesk never installs or starts Tailscale on the tablet and never opens a sign-in page. The
fingerprint step stays, with its advice reworded: there is no cable to vouch for the tablet, so
the tick is "I have compared this fingerprint". Only the type is remembered, alongside address,
port, user name and key file location. Settings saved before the selector existed mean SSH.

**Simulation.** A dark full-width bar reading "Simulation" stays on every page, the window title
and the sidebar chip repeat it, and Home's title starts with it. Screenshots and status in this
mode are labelled as simulator output. Entering the simulator never touches the real-tablet
service; leaving it returns to a disconnected state. The simulator is described as a rehearsal of
connecting, setup and document handling. It does not run the tablet's own software or its screen,
and the interface never says a simulated result proves anything about a physical tablet. Setup ›
Sandbox, shown only in simulation, restarts the pretend tablet, switches faults on and starts a
fresh one of a chosen model.

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
where to look, plus a link that opens Setup › Features. Document actions stay available unless
the tablet explicitly reports them off.
Firmware gates are explained beside the feature checkboxes, and a plan the installer refuses
disables Install and shows the installer's reasons per feature.

**Installing.** Nothing is installed without a reviewed plan for exactly the current selection
plus a confirmation dialog. Changing the selection invalidates the plan. Uninstall lives under
Recovery, next to plain-language advice for the four things that can go wrong.

**Art-mode exit.** While tablet mode runs, a bar with **Stop tablet mode** appears on every page.
Disconnecting or closing the window stops it first. If stopping cannot be confirmed, the page
says so and gives the fallback: restart the tablet, because tablet mode uses only temporary
settings.

**Art input method.** One selector with three choices, sent to the bridge as `mode`: *Automatic*
(sent as nothing, so the bridge keeps its own default: Windows Ink on Windows, pointer on macOS,
the pen driver on Linux), *Pen* and *Mouse pointer*. A sentence under the selector says what the
choice means on this computer. Mouse is disabled on Linux, where the bridge does not support it.
Pen on macOS is experimental pressure, so it needs a tick ("I understand macOS pen pressure is
experimental and unverified") before Start is enabled; Automatic never asks for it.

**Art capabilities.** Six rows (pointer, pressure, tilt, native touch, canvas gestures, canvas on
the tablet) repeat what the bridge reports, in the words "Reported available", "Not available
here", "Not reported yet" or "Experimental — not verified in an art application". "Reported" is
deliberate: the page relays the bridge and does not claim a pen was tried. The bridge's longer
technical note is a tooltip.

**Art touch.** Native multitouch and canvas gestures are different things and are named
separately. Touch is offered where the bridge reports native multitouch, or where gestures can
travel through the Krita canvas display. On macOS that second route is the only one, so touch is
enabled only with the canvas display ticked, which needs the user's own confirmation that the
plugin is enabled in Krita. Touch is never switched on without being asked.

**Krita canvas and plugin.** The canvas display works on Linux, macOS and Windows and needs a
small Krita plugin. PaperDesk does not install or enable it silently: the page shows where the
plugin would be copied, copies it on a confirmed click, replaces an existing one only through a
separate button that keeps a backup, and lists the steps the user takes inside Krita to enable it.
**Check plugin** asks the plugin on this computer once whether it answers and shows the bridge's
reply. It needs no tablet, is never repeated by itself, and does not tick the user's own "I
enabled it" confirmation; starting tablet mode asks again anyway.

**Typing.** Text is sent in one piece by default. Live keyboard is a separate mode that must be
started, is visibly "Live", and ends when focus leaves or on Shift+Esc. Keys keep their order,
and a failed key discards the ones behind it. The page says, before anything is sent, that curly
quotes, long dashes, the ellipsis and non-breaking spaces are typed as their plain US
equivalents, and that any other character outside a US keyboard makes the tablet refuse the
whole text.

**Figure as ink.** Experimental and laid out as a numbered path that cannot be skipped: prepare
a preview from a picture, 1 check the pen, 2 draw a small calibration mark, tick that the mark
was inspected on the tablet, 3 draw. Each numbered step has its own confirmation. The pen check
only reads: the helper it verifies was installed by Setup with the keyboard extra, and the check
copies nothing and draws nothing. Stop ends the drawing and reports it as partial; nothing is
replayed after Stop, a disconnect or closing the window. Any failure here holds further changes
until the user has looked at the tablet and dismissed the banner.

**Files.** Native open and save dialogs start in the user's Documents or Pictures folder.
Downloads, exports and screenshots are new files only; choosing an existing name is refused.

**Upload folders.** "Upload into" lists the tablet's actual folders, so an empty Inbox or Archive
can be chosen. Two folders with the same name each show the start of their identifier. A single
folder called Inbox is preselected; two are ambiguous and nothing is guessed. A choice the user
made is kept across reloads. Folders appear in the table and in the count ("3 documents and 4
folders"), and selecting one explains that it cannot be downloaded.

**Copy pass.** The folder card is named for what it does. One press imports the PDF, EPUB and
RMDOC files in the chosen folder to the tablet's top level, then saves every tablet document as
a complete PDF in a new dated folder inside `device-copies`. The card says nothing is deleted on
either side, that it does not keep the two in step, and that it is separate from any archive
timer. The word "sync" is not used for it.

**Existing archive sync.** Some owners already run an archive timer on Linux. A small card reads
its status file when **Refresh status** is pressed and at no other time. It works without a
tablet connected, and says in words whether the timer is active, idle, stale, unreadable or
absent, with bytes done when given. A large active transfer adds that USB-C speeds it up. A
status older than ten minutes is shown as stale and never as active. PaperDesk does not start,
stop or configure the timer, and the card is hidden in simulation.
