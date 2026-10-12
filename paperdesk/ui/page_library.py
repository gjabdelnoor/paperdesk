"""Library: documents on the tablet, upload, download, PDF export, copy pass, archive status."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QLineEdit, QTreeWidget,
                               QTreeWidgetItem)

from .services import describe_error
from .widgets import Card, Page, button, flow, label, plain, pretty_key, set_role

UPLOAD_FILTER = "Documents (*.pdf *.epub *.rmdoc);;All files (*)"
_EXTENSIONS = {"pdf": ".pdf", "epub": ".epub"}
_COPY_ABOUT = ("One pass, only when you press the button. PDF, EPUB and RMDOC files in the chosen folder "
               "are imported to the tablet's top level; then every tablet document is saved as a complete "
               "PDF in a new dated folder inside device-copies. Nothing is deleted on either side. It "
               "does not keep the two in step and is separate from any archive timer.")
_ARCHIVE_ABOUT = ("Reads the status file of an archive timer already set up on this computer, if there is "
                  "one. PaperDesk never starts, stops or configures that timer, and looks only when you "
                  "press the button.")


def is_folder(doc: Any) -> bool:
    return isinstance(doc, dict) and str(doc.get("type") or "").lower() == "folder"


def upload_choices(documents: list[dict]) -> list[tuple[str, str]]:
    """(label, value) for each tablet folder an upload can go into.

    Real folder rows are used as they are, empty or not, with the folder's id as the value and
    a short id beside names that occur twice.  Only a service that reports no folder rows at
    all falls back to the folder names its documents mention.
    """
    rows = [doc for doc in documents if is_folder(doc) and doc.get("id")]
    if not rows:
        names = sorted({str(doc["folder"]) for doc in documents if doc.get("folder")}, key=str.casefold)
        return [(name, name) for name in names]
    names = [str(doc.get("name") or "") for doc in rows]
    choices = []
    for doc, name in zip(rows, names):
        ident = str(doc["id"])
        shown = name or "Unnamed folder"
        choices.append((f"{shown} ({ident[:8]})" if names.count(name) > 1 else shown, ident))
    return sorted(choices, key=lambda choice: (choice[0].casefold(), choice[1]))


def size(count: float) -> str:
    value = float(count)
    for unit in ("bytes", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def archive_text(info: Any) -> tuple[str, str]:
    """(sentence, role) for what archive_sync_status() reported."""
    if not isinstance(info, dict):
        return "The archive status could not be read.", "warn"
    message = str(info.get("message") or "").strip()
    if info.get("invalid"):
        return " ".join(filter(None, ["Unreadable.", message])), "warn"
    if not info.get("available"):
        return " ".join(filter(None, ["Not available.", message])), "muted"
    stale = bool(info.get("stale"))
    active = info.get("active") is True and not stale
    large = info.get("large") is True and active
    parts = ["Stale." if stale else "Active, large transfer." if large else "Active." if active else "Idle.",
             message]
    if large and "usb" not in message.lower():
        parts.append("Plugging the tablet in over USB-C speeds it up.")
    done, total = info.get("bytes_done") or 0, info.get("bytes_total") or 0
    if not stale and isinstance(done, (int, float)) and isinstance(total, (int, float)):
        if total > 0:
            parts.append(f"{size(done)} of {size(total)} ({min(100, round(100 * done / total))}%).")
        elif done > 0:
            parts.append(f"{size(done)} so far.")
    if info.get("updated_at"):
        parts.append(f"Status written {when(info['updated_at'])}.")
    return " ".join(filter(None, parts)), "warn" if stale else "ok" if active else "muted"


def when(value: Any) -> str:
    """Show a modification time; the backend may send a timestamp or ready-made text."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
        except (OverflowError, OSError, ValueError):
            pass
    return plain(value)


def safe_name(name: str, fallback: str = "document") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name).strip(" .")
    return cleaned[:120] or fallback


class LibraryPage(Page):
    def __init__(self, win):
        super().__init__(win, "Library",
                         "Documents on the tablet. Downloads and exports are always saved as new "
                         "files; nothing on this computer is overwritten.")
        card = Card("On the tablet")
        self.reload_button = button("Reload list", None, "Read the document list again")
        self.upload_button = button("Upload…", "primary", "Send PDF, EPUB or RMDOC files to the tablet (Ctrl+U)")
        self.download_button = button("Download…", None, "Save the selected document here (Ctrl+D)")
        self.reload_button.clicked.connect(win.reload_documents)
        self.upload_button.clicked.connect(self.upload)
        self.download_button.clicked.connect(self.download)
        card.add(flow(self.upload_button, self.download_button, self.reload_button))

        row = QHBoxLayout()
        row.setSpacing(10)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by name or folder")
        self.filter_edit.setAccessibleName("Filter documents")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        row.addWidget(self.filter_edit, 2)
        row.addWidget(label("Upload into", "muted", wrap=False))
        self.folder_combo = QComboBox()
        self.folder_combo.setAccessibleName("Tablet folder for uploads")
        self.folder_combo.setMinimumWidth(150)
        self.folder_combo.currentIndexChanged.connect(self._folder_picked)
        self._folder_chosen = self._filling = False
        row.addWidget(self.folder_combo, 1)
        card.body.addLayout(row)

        self.table = QTreeWidget()
        self.table.setAccessibleName("Tablet documents")
        self.table.setHeaderLabels(["Name", "Type", "Pages", "Modified", "Folder"])
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setUniformRowHeights(True)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setMinimumHeight(200)
        self.table.itemSelectionChanged.connect(self.refresh_state)
        self.table.itemActivated.connect(lambda *_: self.download())
        card.add(self.table)
        self.count_label = card.add(label("", "caption"))
        self.col.addWidget(card, 1)

        current = Card("Open document")
        self.current_label = label("", None)
        self.current_label.setAccessibleName("Document open on the tablet")
        self.export_button = button("Export as PDF…", None,
                                    "Save the document open on the tablet as a PDF (Ctrl+E)")
        self.export_button.clicked.connect(self.export_current)
        line = QHBoxLayout()
        line.addWidget(self.current_label, 1)
        line.addWidget(self.export_button)
        current.body.addLayout(line)
        self.col.addWidget(current)

        sync = Card("Copy pass", _COPY_ABOUT)
        line = QHBoxLayout()
        self.sync_edit = QLineEdit(str(win.settings.value("library/sync_folder", "") or ""))
        self.sync_edit.setReadOnly(True)
        self.sync_edit.setPlaceholderText("No folder chosen")
        self.sync_edit.setAccessibleName("Copy pass folder")
        self.sync_choose = button("Choose folder…")
        self.sync_button = button("Run copy pass")
        self.sync_choose.clicked.connect(self.choose_sync_folder)
        self.sync_button.clicked.connect(self.sync)
        line.addWidget(self.sync_edit, 1)
        line.addWidget(self.sync_choose)
        line.addWidget(self.sync_button)
        sync.body.addLayout(line)
        self.sync_result = sync.add(label("", "caption"))
        self.col.addWidget(sync)

        self.archive_card = Card("Existing archive sync", _ARCHIVE_ABOUT)
        self.archive_button = button("Refresh status", None,
                                     "Read the archive timer's status once; the timer is not touched")
        self.archive_button.setAccessibleName("Refresh archive sync status")
        self.archive_button.clicked.connect(self.refresh_archive)
        self.archive_label = label("Not read yet.", "muted")
        self.archive_label.setAccessibleName("Archive sync status")
        line = QHBoxLayout()
        line.addWidget(self.archive_label, 1)
        line.addWidget(self.archive_button)
        self.archive_card.body.addLayout(line)
        self.col.addWidget(self.archive_card)

        win.documents_changed.connect(self._populate)
        self._populate()

    # -- display -------------------------------------------------------
    def _populate(self) -> None:
        selected = self.selected_id()
        self.table.setSortingEnabled(False)
        self.table.clear()
        for doc in self.win.documents:
            item = QTreeWidgetItem([plain(doc.get("name")), plain(doc.get("type")),
                                    "" if is_folder(doc) else plain(doc.get("pages")),
                                    when(doc.get("modified")), plain(doc.get("folder"))])
            item.setData(0, Qt.ItemDataRole.UserRole, doc)
            item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.addTopLevelItem(item)
            if selected and doc.get("id") == selected:
                item.setSelected(True)
        self.table.setSortingEnabled(True)
        for column in (0, 1, 2, 3):
            self.table.resizeColumnToContents(column)
        self.table.setColumnWidth(0, min(max(self.table.columnWidth(0) + 16, 200), 380))
        chosen = self.folder_combo.currentData()
        self._filling = True
        self.folder_combo.clear()
        self.folder_combo.addItem("Top level", "")
        for text, value in upload_choices(self.win.documents):
            self.folder_combo.addItem(text, value)
        index = self.folder_combo.findData(chosen) if self._folder_chosen else -1
        if index < 0:
            # Nothing was picked, or the picked folder is gone: a single real Inbox is the default.
            self._folder_chosen = False
            inboxes = [doc for doc in self.win.documents if is_folder(doc) and doc.get("name") == "Inbox"]
            index = self.folder_combo.findData(str(inboxes[0].get("id"))) if len(inboxes) == 1 else 0
        self.folder_combo.setCurrentIndex(max(index, 0))
        self._filling = False
        self._apply_filter()
        self.refresh_state()

    def _apply_filter(self) -> None:
        needle = self.filter_edit.text().strip().lower()
        shown = 0
        for index in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(index)
            match = not needle or needle in item.text(0).lower() or needle in item.text(4).lower()
            item.setHidden(not match)
            shown += match
        total = self.table.topLevelItemCount()
        folders = sum(1 for doc in self.win.documents if is_folder(doc))
        documents = total - folders
        if not self.win.connected:
            text = "Connect to a tablet to see its documents."
        elif total == 0:
            text = "The tablet reported no documents."
        elif shown == total:
            text = f"{documents} document" + ("" if documents == 1 else "s")
            if folders:
                text += f" and {folders} folder" + ("" if folders == 1 else "s")
        else:
            text = f"{shown} of {total} " + ("rows" if folders else "documents") + " shown"
        if is_folder(self.selected()):
            text += ". A folder cannot be downloaded; choose it under “Upload into” to send files there."
        self.count_label.setText(text)

    def _folder_picked(self, _index: int) -> None:
        if not self._filling:
            self._folder_chosen = True

    def selected(self) -> dict | None:
        items = self.table.selectedItems()
        return items[0].data(0, Qt.ItemDataRole.UserRole) if items else None

    def selected_id(self) -> str:
        doc = self.selected()
        return str(doc.get("id")) if doc else ""

    def select_document(self, document_id: str) -> bool:
        for index in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(index)
            if str(item.data(0, Qt.ItemDataRole.UserRole).get("id")) == document_id:
                self.table.setCurrentItem(item)
                return True
        return False

    def refresh_state(self) -> None:
        connected = self.win.connected
        for widget in (self.reload_button, self.filter_edit, self.table):
            widget.setEnabled(connected)
        self.upload_button.setEnabled(self.win.allows("upload"))
        self.folder_combo.setEnabled(self.win.allows("upload"))
        chosen = self.selected()
        self.download_button.setEnabled(self.win.allows("download") and chosen is not None
                                        and not is_folder(chosen))
        doc = self.win.status.get("active_document") if connected else None
        has_doc = isinstance(doc, dict) and bool(doc.get("name"))
        if has_doc:
            pages = f" — page {doc.get('page')} of {doc.get('pages')}" if doc.get("pages") else ""
            self.current_label.setText(f"{doc['name']}{pages}")
        else:
            self.current_label.setText("No document is open on the tablet." if connected
                                       else "Connect to a tablet to export its open document.")
        self.export_button.setEnabled(has_doc and self.win.allows("export"))
        self.sync_choose.setEnabled(True)
        self.sync_button.setEnabled(self.win.allows("sync") and bool(self.sync_edit.text()))
        self._apply_filter()
        # The archive timer belongs to this computer, not to the pretend tablet: leave it alone there.
        self.archive_card.setVisible(not self.win.simulated
                                     and callable(getattr(self.win.service, "archive_sync_status", None)))

    # -- actions -------------------------------------------------------
    def upload(self) -> None:
        if not self._ready("upload documents"):
            return
        paths = self.win.dialogs.open_files("Upload to tablet", UPLOAD_FILTER)
        folder = str(self.folder_combo.currentData() or "")
        for path in paths:
            name = Path(path).name
            self.win.run("upload", lambda s, p=path: s.upload(p, folder), key=f"upload:{path}",
                         label=f"Upload {name}", mutation=True, on_done=self._after_change)

    def download(self) -> None:
        if not self._ready("download a document"):
            return
        doc = self.selected()
        if doc is None:
            self.win.notify("info", "Select a document in the list first, then download it.")
            return
        if is_folder(doc):
            self.win.notify("info", "A folder cannot be downloaded. Select a document inside it.")
            return
        name = safe_name(str(doc.get("name") or "document"))
        extension = _EXTENSIONS.get(str(doc.get("type") or "").lower(), ".pdf")
        destination = self.win.dialogs.save_file("Download from tablet", name + extension)
        if destination:
            doc_id = str(doc.get("id"))
            self.win.run("download", lambda s: s.download(doc_id, destination),
                         key=f"download:{doc_id}", label=f"Download {name}")

    def export_current(self) -> None:
        if not self._ready("export the open document"):
            return
        doc = self.win.status.get("active_document") or {}
        if not (isinstance(doc, dict) and doc.get("name")):
            self.win.notify("info", "No document is open on the tablet, so there is nothing to export.")
            return
        name = safe_name(str(doc["name"]))
        destination = self.win.dialogs.save_file("Export open document as PDF", name + ".pdf",
                                                 "PDF documents (*.pdf)")
        if destination:
            self.win.run("export", lambda s: s.export_current(destination),
                         label=f"Export {name} as PDF")

    def choose_sync_folder(self) -> None:
        folder = self.win.dialogs.directory("Choose the folder for the copy pass")
        if folder:
            self.sync_edit.setText(folder)
            self.win.settings.setValue("library/sync_folder", folder)
            self.sync_result.setText("")
            self.refresh_state()

    def sync(self) -> None:
        folder = self.sync_edit.text()
        if not folder:
            self.win.notify("info", "Choose a folder for the copy pass first.")
            return
        if self.win.run("sync", lambda s: s.sync(folder), label=f"Copy pass with {Path(folder).name}",
                        mutation=True, on_done=self._after_sync, on_error=self._sync_failed):
            self.sync_result.setText("Copy pass running… the status line below shows when it finishes.")
            set_role(self.sync_result, "caption")

    def _sync_failed(self, exc: BaseException) -> None:
        self.sync_result.setText(f"The copy pass did not finish. {describe_error(exc)} Files already written "
                                 "stay where they are; nothing was retried.")
        set_role(self.sync_result, "error")

    def _after_sync(self, result: Any) -> None:
        if isinstance(result, dict):
            details = ", ".join(
                f"{pretty_key(key).lower()}: {len(value) if isinstance(value, (list, dict)) else plain(value)}"
                for key, value in result.items()
                if key not in ("ok", "message", "simulated", "path", "paths", "mode"))
            paths = result.get("paths")
            names = [Path(str(item)).name for item in paths] if isinstance(paths, list) else []
            written = ("New files: " + ", ".join(names[:4])
                       + (f" and {len(names) - 4} more" if len(names) > 4 else "") + ".") if names else ""
            where = f"Tablet copies are in {result['path']}." if result.get("path") else ""
            failed = result.get("ok") is False
            self.sync_result.setText(" ".join(filter(None, [
                "Not completed." if failed else "Last copy pass:", str(result.get("message") or ""),
                details + "." if details else "", written, where,
                "Simulated copies, not real annotations." if result.get("simulated") else ""])))
            set_role(self.sync_result, "error" if failed else "caption")
        self._after_change(result)

    def _after_change(self, result: Any) -> None:
        if not (isinstance(result, dict) and result.get("ok") is False):
            self.win.reload_documents(quiet=True)

    def refresh_archive(self) -> None:
        if self.win.simulated:
            return
        if self.win.run("archive-status", lambda s: s.archive_sync_status(), needs_connection=False,
                        label="Read the archive sync status", quiet=True,
                        on_done=self._archive_read, on_error=self._archive_failed):
            self.archive_label.setText("Reading…")
            set_role(self.archive_label, "muted")

    def _archive_read(self, result: Any) -> None:
        text, role = archive_text(result)
        self.archive_label.setText(text)
        set_role(self.archive_label, role)

    def _archive_failed(self, exc: BaseException) -> None:
        self.archive_label.setText(f"The status could not be read. {describe_error(exc)} "
                                   "The timer was not touched.")
        set_role(self.archive_label, "error")

    def _ready(self, what: str) -> bool:
        if not self.win.connected:
            self.win.notify("warn", f"Connect to a tablet first to {what}.")
            return False
        return True
