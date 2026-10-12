"""Library: documents on the tablet, upload, download, PDF export and folder sync."""
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

UPLOAD_FILTER = "Documents (*.pdf *.epub);;All files (*)"
_EXTENSIONS = {"pdf": ".pdf", "epub": ".epub"}


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
        self.upload_button = button("Upload…", "primary", "Send PDF or EPUB files to the tablet (Ctrl+U)")
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

        sync = Card("Local sync",
                    "Keep a folder on this computer in step with the tablet. PaperDesk reports exactly "
                    "what the tablet service says it changed.")
        line = QHBoxLayout()
        self.sync_edit = QLineEdit(str(win.settings.value("library/sync_folder", "") or ""))
        self.sync_edit.setReadOnly(True)
        self.sync_edit.setPlaceholderText("No folder chosen")
        self.sync_edit.setAccessibleName("Sync folder")
        self.sync_choose = button("Choose folder…")
        self.sync_button = button("Sync now")
        self.sync_choose.clicked.connect(self.choose_sync_folder)
        self.sync_button.clicked.connect(self.sync)
        line.addWidget(self.sync_edit, 1)
        line.addWidget(self.sync_choose)
        line.addWidget(self.sync_button)
        sync.body.addLayout(line)
        self.sync_result = sync.add(label("", "caption"))
        self.col.addWidget(sync)

        win.documents_changed.connect(self._populate)
        self._populate()

    # -- display -------------------------------------------------------
    def _populate(self) -> None:
        selected = self.selected_id()
        self.table.setSortingEnabled(False)
        self.table.clear()
        folders = set()
        for doc in self.win.documents:
            item = QTreeWidgetItem([plain(doc.get("name")), plain(doc.get("type")),
                                    plain(doc.get("pages")), when(doc.get("modified")),
                                    plain(doc.get("folder"))])
            item.setData(0, Qt.ItemDataRole.UserRole, doc)
            item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.addTopLevelItem(item)
            if doc.get("folder"):
                folders.add(str(doc["folder"]))
            if selected and doc.get("id") == selected:
                item.setSelected(True)
        self.table.setSortingEnabled(True)
        for column in (0, 1, 2, 3):
            self.table.resizeColumnToContents(column)
        self.table.setColumnWidth(0, min(max(self.table.columnWidth(0) + 16, 200), 380))
        chosen = self.folder_combo.currentData()
        self.folder_combo.clear()
        self.folder_combo.addItem("Top level", "")
        for folder in sorted(folders):
            self.folder_combo.addItem(folder, folder)
        index = self.folder_combo.findData(chosen)
        self.folder_combo.setCurrentIndex(max(index, 0))
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
        if not self.win.connected:
            text = "Connect to a tablet to see its documents."
        elif total == 0:
            text = "The tablet reported no documents."
        elif shown == total:
            text = f"{total} document" + ("" if total == 1 else "s")
        else:
            text = f"{shown} of {total} documents shown"
        self.count_label.setText(text)

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
        self.download_button.setEnabled(self.win.allows("download") and self.selected() is not None)
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
        if not self.win.documents:
            self._apply_filter()

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
        folder = self.win.dialogs.directory("Choose the folder to sync")
        if folder:
            self.sync_edit.setText(folder)
            self.win.settings.setValue("library/sync_folder", folder)
            self.sync_result.setText("")
            self.refresh_state()

    def sync(self) -> None:
        folder = self.sync_edit.text()
        if not folder:
            self.win.notify("info", "Choose a folder to sync first.")
            return
        if self.win.run("sync", lambda s: s.sync(folder), label=f"Sync with {Path(folder).name}",
                        mutation=True, on_done=self._after_sync, on_error=self._sync_failed):
            self.sync_result.setText("Syncing… the status line below shows when it finishes.")
            set_role(self.sync_result, "caption")

    def _sync_failed(self, exc: BaseException) -> None:
        self.sync_result.setText(f"The sync did not finish. {describe_error(exc)} Files already written "
                                 "stay where they are; nothing was retried.")
        set_role(self.sync_result, "error")

    def _after_sync(self, result: Any) -> None:
        if isinstance(result, dict):
            details = ", ".join(
                f"{pretty_key(key).lower()}: {len(value) if isinstance(value, (list, dict)) else plain(value)}"
                for key, value in result.items() if key not in ("ok", "message", "simulated"))
            paths = result.get("paths")
            names = [Path(str(item)).name for item in paths] if isinstance(paths, list) else []
            written = ("New files: " + ", ".join(names[:4])
                       + (f" and {len(names) - 4} more" if len(names) > 4 else "") + ".") if names else ""
            failed = result.get("ok") is False
            self.sync_result.setText(" ".join(filter(None, [
                "Not completed." if failed else "Last sync:", str(result.get("message") or ""),
                details + "." if details else "", written,
                "Simulated copies, not real annotations." if result.get("simulated") else ""])))
            set_role(self.sync_result, "error" if failed else "caption")
        self._after_change(result)

    def _after_change(self, result: Any) -> None:
        if not (isinstance(result, dict) and result.get("ok") is False):
            self.win.reload_documents(quiet=True)

    def _ready(self, what: str) -> bool:
        if not self.win.connected:
            self.win.notify("warn", f"Connect to a tablet first to {what}.")
            return False
        return True
