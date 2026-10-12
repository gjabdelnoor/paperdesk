"""Reference images as ordinary layers, worked from one small docker in Krita.

A reference is a separate paint layer named "ref: <file>". Imports select it and open the
Transform tool immediately. Done applies the transform, locks the reference for tracing,
and returns to the painting layer. Nothing here touches files: the image is copied in."""
import os
from krita import Krita
try:
    from PyQt5.QtCore import Qt, QTimer, QEvent
    from PyQt5.QtGui import QImage, QKeySequence
    from PyQt5.QtWidgets import QApplication, QCheckBox, QDockWidget, QFileDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QToolButton, QVBoxLayout, QWidget
    RIGHT, HORIZONTAL, ARGB, KEEP, SMOOTH = Qt.RightDockWidgetArea, Qt.Horizontal, QImage.Format_ARGB32, Qt.KeepAspectRatio, Qt.SmoothTransformation
except ImportError:
    from PyQt6.QtCore import Qt, QTimer, QEvent
    from PyQt6.QtGui import QImage, QKeySequence
    from PyQt6.QtWidgets import QApplication, QCheckBox, QDockWidget, QFileDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QToolButton, QVBoxLayout, QWidget
    RIGHT, HORIZONTAL, ARGB, KEEP, SMOOTH = Qt.DockWidgetArea.RightDockWidgetArea, Qt.Orientation.Horizontal, QImage.Format.Format_ARGB32, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation

PREFIX = 'ref: '
NAME = 'rm_viewport_references'
BRUSH = 'KritaShape/KisToolBrush'
TRANSFORM = 'KisToolTransform'
SHORTCUT = getattr(QEvent, 'ShortcutOverride', None) or QEvent.Type.ShortcutOverride
KEYPRESS = getattr(QEvent, 'KeyPress', None) or QEvent.Type.KeyPress
PASTE = getattr(QKeySequence, 'Paste', None) or QKeySequence.StandardKey.Paste
HELP = 'Drag inside to move, a corner to resize, or the round handle above the image to rotate. Right-click for Lock aspect ratio. Done returns to painting.'

def references(document):
    return [node for node in reversed(document.rootNode().childNodes()) if node.name().startswith(PREFIX)] if document else []

def add(document, image, title, opacity=0.5):
    """Copy a picture into its own unlocked layer on top, fitted into 60% of the canvas.
    The caller selects it and activates Transform. Returns the new layer."""
    if image.isNull(): raise ValueError('not a readable picture')
    if document.colorModel() != 'RGBA' or document.colorDepth() != 'U8': raise ValueError('references need an 8-bit RGB image')
    fit = image.scaled(int(document.width()*0.6), int(document.height()*0.6), KEEP, SMOOTH) if image.width() > document.width()*0.6 or image.height() > document.height()*0.6 else image
    fit = fit.convertToFormat(ARGB); bits = fit.bits(); bits.setsize(fit.height()*fit.bytesPerLine())
    rows = bytes(bits); width = fit.width()*4
    data = rows if fit.bytesPerLine() == width else b''.join(rows[y*fit.bytesPerLine():y*fit.bytesPerLine()+width] for y in range(fit.height()))
    taken = {node.name() for node in references(document)}; name = PREFIX+title[:40]; count = 1
    while name in taken: count += 1; name = f'{PREFIX}{title[:40]} {count}'   # the docker and the tablet tell them apart by name
    shift = 48*(len(taken) % 5)   # each new one a little down and right, so it never hides exactly behind the last
    node = document.createNode(name, 'paintLayer')
    node.setPixelData(data, (document.width()-fit.width())//2+shift, (document.height()-fit.height())//2+shift, fit.width(), fit.height())
    document.rootNode().addChildNode(node, None); node.setOpacity(round(255*opacity)); node.setLocked(False)
    document.refreshProjection(); return node

class ReferenceDock(QDockWidget):
    def __init__(self, parent=None):
        super().__init__('Tablet references', parent); self.setObjectName(NAME); self.moving = None; self.shown = None
        body = QWidget(self); self.column = QVBoxLayout(body); self.setWidget(body); self.setAcceptDrops(True)
        top = QHBoxLayout(); self.column.addLayout(top)
        for text, slot in (('Add image…', self.pick), ('Paste', self.paste)):
            button = QPushButton(text, body); button.clicked.connect(slot); top.addWidget(button)
        self.note = QLabel('Paste (Ctrl+V), add, or drop an image to resize, rotate and move it on its own layer.', body); self.note.setWordWrap(True); self.column.addWidget(self.note)
        self.rows = QVBoxLayout(); self.column.addLayout(self.rows); self.column.addStretch(1)
        self.timer = QTimer(self); self.timer.setInterval(700); self.timer.timeout.connect(self.refresh); self.timer.start()
        from .placement import Placement
        self.placement = Placement(self)
        QApplication.instance().installEventFilter(self)

    def document(self):
        window = next((w for w in Krita.instance().windows() if w.qwindow() == self.parentWidget()), None)
        view = window.activeView() if window else None
        return view.document() if view else None
    def say(self, text): self.note.setText(text)

    def editing(self, document):
        if self.moving is None or document is None: return False
        root, target = self.moving[0].rootNode(), document.rootNode()
        if root is None or target is None or root.uniqueId() != target.uniqueId(): return False
        # Tool actions are not checkable in Krita; the toolbox buttons are.
        button = getattr(self, 'transform_button', None)
        if button is None:
            button = self.parentWidget().findChild(QToolButton, TRANSFORM)
            if button is not None:
                self.transform_button = button
                button.destroyed.connect(lambda: setattr(self, 'transform_button', None))
        return button is not None and button.isChecked()

    def eventFilter(self, target, event):
        if self.placement.filter_event(target, event): return True
        # Handle external image paste only on this window's canvas or this docker. Text
        # fields, vector clipboard data, selections copied inside Krita and other windows
        # retain Krita's normal paste. ShortcutOverride prevents native Ctrl+V firing too.
        if event.type() not in (SHORTCUT, KEYPRESS) or not event.matches(PASTE): return False
        if not isinstance(target, QWidget) or target.window() != self.window(): return False
        if QApplication.activeModalWidget() is not None: return False
        data = QApplication.clipboard().mimeData()
        if data is None or not data.hasImage() or any('krita' in f.lower() for f in data.formats()): return False
        current = target; canvas = False
        while current is not None:
            if any(current.inherits(name) for name in ('QLineEdit', 'QTextEdit', 'QPlainTextEdit', 'QAbstractSpinBox')): return False
            if current is self or current.metaObject().className() in ('KisCanvasController', 'KisOpenGLCanvas2', 'KisQPainterCanvas'):
                canvas = True; break
            current = current.parentWidget()
        if not canvas or self.document() is None: return False
        if event.type() == KEYPRESS and not event.isAutoRepeat(): self.paste()
        event.accept(); return True

    def take(self, image, title):
        document = self.document()
        if document is None: return self.say('Open an image first.')
        try:
            self.finish()
            back = document.activeNode(); node = add(document, image, title)
            self.edit(node, back)
        except Exception as error: self.say(f'{title}: {error}')
        self.refresh()
    def pick(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Reference images', os.path.expanduser('~/Pictures'), 'Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)')
        for path in paths: self.take(QImage(path), os.path.basename(path))
    def paste(self):
        image = QApplication.clipboard().image()
        if image.isNull(): return self.say('The clipboard holds no picture.')
        self.take(image, 'pasted')
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasImage(): event.acceptProposedAction()
    def dropEvent(self, event):
        data = event.mimeData()
        if data.hasUrls():
            for url in data.urls():
                if url.isLocalFile(): self.take(QImage(url.toLocalFile()), os.path.basename(url.toLocalFile()))
        elif data.hasImage(): self.take(QImage(data.imageData()), 'dropped')
        event.acceptProposedAction()

    def tool(self, name):
        action = Krita.instance().action(name)
        if action is None: raise RuntimeError(f'Tool unavailable: {name}')
        action.trigger()
    def finish(self):
        if self.moving is None: return
        document, node, back, selection = self.moving; self.moving = None
        self.placement.end()
        if document.rootNode() is None: return
        # Switching away applies Krita's live transform before locking or changing layers.
        current = self.document()
        if current is not None and current.rootNode().uniqueId() == document.rootNode().uniqueId(): self.tool(BRUSH)
        node.setLocked(True)
        if back is not None and document.nodeByUniqueID(back.uniqueId()) is not None: document.setActiveNode(back)
        if selection is not None: document.setSelection(selection)
        self.shown = None; self.say('Reference placed. Transform edits it again; Opacity fades the whole layer.')
    def edit(self, node, back=None):
        """Select exactly this paint layer, with an unrestricted transform box."""
        document = self.document()
        if document is None: return
        if self.moving is not None:
            same = self.moving[1].uniqueId() == node.uniqueId()
            self.finish()
            if same: return self.refresh()
        if back is None: back = document.activeNode()
        if back is not None and back.uniqueId() == node.uniqueId():
            back = next((n for n in reversed(document.rootNode().childNodes()) if n.uniqueId() != node.uniqueId() and not n.locked() and n.type() == 'paintlayer'), None)
        selection = document.selection()
        if selection is not None: document.setSelection(None)
        # Re-activate from the brush so an old transform cannot keep its previous target.
        self.tool(BRUSH); node.setLocked(False); node.setVisible(True); document.setActiveNode(node)
        self.moving = (document, node, back, selection)
        bounds = node.bounds()
        self.tool(TRANSFORM)
        self.tool('KisToolTransformFree')
        self.placement.begin(bounds)
        self.say(HELP); self.shown = None; self.refresh()
    def remove(self, node):
        if self.moving is not None and self.moving[1].uniqueId() == node.uniqueId(): self.finish()
        node.remove(); document = self.document()
        if document is not None: document.refreshProjection()
        self.shown = None; self.refresh()
    def fade(self, node, value):
        node.setOpacity(round(value*2.55)); document = self.document()
        if document is not None: document.refreshProjection()
    def show(self, node, on):
        node.setVisible(bool(on)); document = self.document()
        if document is not None: document.refreshProjection()

    def refresh(self):
        if not self.isVisible() and self.shown is not None: return
        try: nodes = references(self.document())
        except Exception: nodes = []
        if self.moving is not None:
            root = self.moving[0].rootNode()
            if root is None or all(d.rootNode().uniqueId() != root.uniqueId() for d in Krita.instance().documents()):
                self.moving = None; self.placement.end()
        moving = self.moving[1].uniqueId().toString() if self.moving is not None else ''
        key = tuple((node.uniqueId().toString(), node.name(), node.visible()) for node in nodes)+(moving,)
        if key == self.shown:
            # Tablet opacity discs and Krita's Layers docker can change this too. Keep
            # the existing controls (and keyboard/mouse focus) instead of rebuilding.
            for slider, node in zip(self.findChildren(QSlider), nodes):
                value = round(node.opacity()/2.55)
                if not slider.isSliderDown() and slider.value() != value:
                    slider.blockSignals(True); slider.setValue(value); slider.blockSignals(False)
                    slider.parentWidget().findChild(QLabel, 'opacity_percent').setText(f'{value}%')
            return
        self.shown = key
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget() is not None: item.widget().deleteLater()
        for node in nodes:
            row = QWidget(self); layout = QVBoxLayout(row); layout.setContentsMargins(0, 0, 0, 0)
            line = QHBoxLayout(); layout.addLayout(line)
            eye = QCheckBox(node.name()[len(PREFIX):][:18], row); eye.setChecked(node.visible()); eye.setToolTip('Show or hide'); eye.toggled.connect(lambda on, n=node: self.show(n, on))
            fade = QHBoxLayout(); layout.addLayout(fade)
            slider = QSlider(HORIZONTAL, row); slider.setRange(0, 100); slider.setValue(round(node.opacity()/2.55)); slider.setToolTip('Opacity of this entire image layer')
            label = QLabel(f'{slider.value()}%', row); label.setObjectName('opacity_percent'); label.setMinimumWidth(38)
            slider.valueChanged.connect(lambda v, n=node, l=label: (self.fade(n, v), l.setText(f'{v}%')))
            fade.addWidget(QLabel('Opacity', row)); fade.addWidget(slider, 1); fade.addWidget(label)
            drag = QPushButton('Done' if node.uniqueId().toString() == moving else 'Transform', row); drag.setToolTip('Move, resize and rotate this image'); drag.clicked.connect(lambda _=False, n=node: self.edit(n))
            # Plain text avoids Qt's font fallback for the Unicode cross. On the desktop
            # that fallback crashes in FcCharSetHasChar during QPushButton::sizeHint.
            gone = QPushButton('Remove', row); gone.setToolTip('Remove this reference'); gone.clicked.connect(lambda _=False, n=node: self.remove(n))
            for widget in (eye, drag, gone): line.addWidget(widget)
            line.setStretch(0, 1); self.rows.addWidget(row)

KEPT = []   # strong references: without one, Python drops its half of the docker and its buttons go dead

def ensure(qwindow):
    """One docker per Krita window, added once (also when the plugin is loaded into a running Krita)."""
    for held in KEPT:
        try:
            if held.parentWidget() == qwindow and held.objectName() == NAME: return held
        except RuntimeError: pass  # A closed window's C++ docker has been destroyed.
    dock = qwindow.findChild(QDockWidget, NAME)
    if dock is not None and not isinstance(dock, ReferenceDock):   # a leftover whose Python half is gone
        dock.setObjectName(''); qwindow.removeDockWidget(dock); dock.deleteLater(); dock = None
    if dock is None:
        dock = ReferenceDock(qwindow); qwindow.addDockWidget(RIGHT, dock); KEPT.append(dock)
    return dock
