# SPDX-License-Identifier: AGPL-3.0-or-later
# Adapted for PaperDesk 2026-10-12: portable socket paths, bounded requests; no dev exec hook.
"""Serves the active canvas view to the Paper Pro bridge over a private local socket.

The bridge asks for the part of the artwork that sits under a tablet-shaped rectangle
centred in the canvas area. The frame is rendered from the document's own pixels (the
projection of all visible layers), not from the screen, so it stays sharp at any zoom.

Requests are one JSON line each; replies are one JSON line plus `size` bytes of PNG.
  {"op":"frame","w":2160,"h":1620,"turn":90,"have":"<hash>"}
      -> {"rect":[x,y,w,h], "screen":[w,h], "hash":..., "zoom":...} + PNG if the hash changed
  {"op":"view","scale":1.1,"at":[u,v],"move":[du,dv]}   pinch/drag, in fractions of the rectangle
"""
import hashlib, json, math, os, sys
from krita import Krita, Extension, ManagedColor
try:
    from PyQt5.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QObject, QPoint, QPointF, QRectF, Qt, QThread, QTimer
    from PyQt5.QtGui import QColor, QConicalGradient, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QTransform
    from PyQt5.QtNetwork import QLocalServer
    from PyQt5.QtWidgets import QAbstractScrollArea, QApplication, QMdiArea, QWidget
    USER_ONLY, RGB32, SMOOTH, WRITE = QLocalServer.UserAccessOption, QImage.Format_RGB32, QPainter.SmoothPixmapTransform, QIODevice.WriteOnly
except ImportError:
    from PyQt6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QObject, QPoint, QPointF, QRectF, Qt, QThread, QTimer
    from PyQt6.QtGui import QColor, QConicalGradient, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QTransform
    from PyQt6.QtNetwork import QLocalServer
    from PyQt6.QtWidgets import QAbstractScrollArea, QApplication, QMdiArea, QWidget
    USER_ONLY, RGB32, SMOOTH, WRITE = QLocalServer.SocketOption.UserAccessOption, QImage.Format.Format_RGB32, QPainter.RenderHint.SmoothPixmapTransform, QIODevice.OpenModeFlag.WriteOnly

def socket_path():
    override = os.environ.get('RM_VIEWPORT_SOCKET')
    if override:
        if sys.platform == 'win32': return override
        directory = os.path.dirname(os.path.abspath(override))
        if os.path.islink(directory) or not os.path.isdir(directory): raise RuntimeError('Viewport socket override needs a private directory')
        info = os.stat(directory)
        if hasattr(os, 'getuid') and info.st_uid != os.getuid(): raise RuntimeError('Viewport socket override has a different owner')
        if info.st_mode & 0o077: raise RuntimeError('Viewport socket override directory must be private (mode 0700)')
        return override
    if sys.platform == 'win32':
        identity = os.path.expanduser('~').replace('\\', '/').casefold()
        return 'PaperDeskViewport-'+hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
    elif sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Caches/PaperDesk')
    else:
        base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
        if not os.environ.get('FLATPAK_ID'): base = os.path.join(base, 'PaperDesk')
    os.makedirs(base, mode=0o700, exist_ok=True)
    if os.path.islink(base): raise RuntimeError('Viewport socket directory must not be a symlink')
    if hasattr(os, 'getuid') and os.stat(base).st_uid != os.getuid(): raise RuntimeError('Viewport socket directory has a different owner')
    os.chmod(base, 0o700)
    return os.path.join(base, 'paperdesk-viewport.sock')

SOCKET = socket_path()

SURROUND = QColor(205, 205, 205)   # outside the image
MIN_ZOOM, MAX_ZOOM = 0.02, 64.0

class NoCanvas(Exception): pass

class FrameEncoder(QThread):
    """Compress an immutable frame off the GUI thread; never touch canvas objects."""
    def __init__(self, parent, client, head, image):
        super().__init__(parent); self.client = client; self.head = head; self.image = image
        self.body = b''
    def run(self):
        try:
            data = QByteArray(); buffer = QBuffer(data); buffer.open(WRITE)
            jpeg = self.head['format'] == 'jpg'
            if not self.image.save(buffer, 'JPG' if jpeg else 'PNG', 95 if jpeg else 60):
                raise RuntimeError('Could not encode tablet preview')
            buffer.close(); self.body = bytes(data)
        except Exception as error: self.head = {'error': str(error)}
        finally: self.image = None

def address(widget):
    for name in ('PyQt5.sip', 'PyQt6.sip', 'sip'):
        try: return int(__import__(name, fromlist=['unwrapinstance']).unwrapinstance(widget))
        except Exception: continue
    return id(widget)

class Repaints(QObject):
    """Counts canvas repaints. Krita repaints the canvas whenever the picture or the view
    changes, so an unchanged count means the last frame is still right and nothing is rendered."""
    PAINT = getattr(QEvent, 'Paint', None) or QEvent.Type.Paint
    TABLET = tuple(getattr(QEvent, name, None) or getattr(QEvent.Type, name) for name in ('TabletMove', 'TabletPress'))
    def __init__(self, parent):
        super().__init__(parent); self.count = 0; self.watched = set(); self.rubber = False; self.owner = parent
    def watch(self, widget):
        # Installed afresh on every call: Qt keeps one copy of a filter per widget, so repeating it
        # costs nothing, and no record of "already watched" widgets can go stale. Such a record,
        # keyed by widget address, missed a canvas that Krita rebuilt at the address of the old one,
        # and the eraser end then went unnoticed.
        for target in [widget]+widget.findChildren(QWidget): target.installEventFilter(self)
    def eventFilter(self, target, event):
        kind = event.type()
        if kind == self.PAINT: self.count += 1
        elif kind in self.TABLET:
            # Which end of the stylus Krita is being shown. Krita swaps to that end's own brush
            # state when it handles this same event, so the eraser is set up right after it.
            rubber = int(event.pointerType()) == 3   # QTabletEvent.Eraser
            if rubber != self.rubber:
                self.rubber = rubber
                if hasattr(self.owner, 'eraser_end'): QTimer.singleShot(0, self.owner.eraser_end)
        return False

class Scene:
    """The active view, its canvas widget, and the tablet-shaped rectangle inside it."""
    def __init__(self, aspect):
        window = Krita.instance().activeWindow(); self.view = window.activeView() if window else None
        self.document = self.view.document() if self.view else None
        if not self.document: raise NoCanvas('no open image')
        self.canvas = self.view.canvas()
        # The canvas of the active view. Looked for inside the active subwindow only: searching the
        # whole main window cost ~50 ms of Krita's GUI thread on every request, changed picture or not.
        top = window.qwindow(); mdi = top.findChild(QMdiArea); active = mdi.activeSubWindow() if mdi else None
        wanted = lambda root: [a for a in root.findChildren(QAbstractScrollArea) if a.metaObject().className() == 'KisCanvasController' and a.isVisible()]
        areas = (wanted(active) if active is not None else []) or wanted(top)
        if not areas: raise NoCanvas('no visible canvas')
        self.widget = areas[0].viewport()
        w, h = self.widget.width(), self.widget.height()
        rw, rh = (h*aspect, h) if w/h > aspect else (w, w/aspect)
        self.rect = QRectF((w-rw)/2, (h-rh)/2, rw, rh)               # in canvas-widget pixels
        inverse, ok = self.view.flakeToImageTransform().inverted()
        self.to_widget = inverse*self.view.flakeToCanvasTransform()   # image px -> widget px
        self.to_image, ok = self.to_widget.inverted()
    def screen_rect(self):
        origin = self.widget.mapToGlobal(QPoint(0, 0))
        return [origin.x()+self.rect.x(), origin.y()+self.rect.y(), self.rect.width(), self.rect.height()]

class ViewportExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent); self.server = None; self.buffers = {}; self.repaints = Repaints(self); self.last = None
        self.pending = []; self.processing = False; self.scheduled = False
    def setup(self):
        QLocalServer.removeServer(SOCKET)
        self.server = QLocalServer(self); self.server.setSocketOptions(USER_ONLY)
        self.server.newConnection.connect(self.accept); self.server.listen(SOCKET)
    def createActions(self, window):
        # The reference docker belongs to the window, bridge or no bridge; the window is not built yet here.
        QTimer.singleShot(0, self.docks)
    def docks(self):
        try:
            from . import references
            for window in Krita.instance().windows(): references.ensure(window.qwindow())
        except Exception: pass
    def accept(self):
        while self.server.hasPendingConnections():
            client = self.server.nextPendingConnection(); self.buffers[client] = b''
            client.readyRead.connect(lambda c=client: self.read(c))
            client.disconnected.connect(lambda c=client: self.gone(c))
    def gone(self, client):
        """A bridge that stops or loses the tablet while the eraser end is near must not leave the pen erasing."""
        self.buffers.pop(client, None)
        if getattr(self, 'eraser_client', None) is client:
            self.eraser_client = None
            try: self.erase(False)
            except Exception: pass
        client.deleteLater()
    def read(self, client):
        self.buffers[client] = self.buffers.get(client, b'')+bytes(client.readAll())
        if len(self.buffers[client]) > 65536:
            client.disconnectFromServer(); return
        if b'\n' in self.buffers[client] and client not in self.pending: self.pending.append(client)
        self.schedule()
    def schedule(self):
        if self.pending and not self.processing and not self.scheduled:
            self.scheduled = True; QTimer.singleShot(0, self.process_request)
    def process_request(self):
        # waitForDone/Qt painting can process socket events recursively. Keep one
        # request in flight and yield between requests; otherwise several preview
        # clients nest frame renders and prevent image paste from completing.
        self.scheduled = False
        if self.processing: return
        while self.pending:
            client = self.pending.pop(0)
            if b'\n' not in self.buffers.get(client, b''): continue
            line, self.buffers[client] = self.buffers[client].split(b'\n', 1)
            self.client = client; self.processing = True
            try: head, body = self.handle(json.loads(line))
            except NoCanvas as error: head, body = {'idle': str(error)}, b''
            except Exception as error: head, body = {'error': f'{type(error).__name__}: {error}'}, b''
            if isinstance(body, QImage):
                # Still one socket request in flight. Input and painting can run
                # while the worker compresses the snapshot, without nested renders.
                self.encoder = FrameEncoder(self, client, head, body)
                self.encoder.finished.connect(self.encoded); self.encoder.start()
                return
            self.finish_request(client, head, body)
            return
        self.schedule()

    def encoded(self):
        worker = self.encoder; self.encoder = None
        self.finish_request(worker.client, worker.head, worker.body); worker.deleteLater()

    def finish_request(self, client, head, body):
        try:
            if client in self.buffers:
                head['size'] = len(body); client.write(json.dumps(head).encode()+b'\n'+body); client.flush()
        finally: self.processing = False
        if b'\n' in self.buffers.get(client, b'') and client not in self.pending: self.pending.append(client)
        self.schedule()
    def handle(self, request):
        op = request.get('op')
        if op == 'ping': return {'app': 'paperdesk-viewport', 'protocol': 1}, b''
        if op == 'frame':
            w, h = int(request['w']), int(request['h'])
            if not 1 <= w <= 4096 or not 1 <= h <= 4096: return {'error': 'frame dimensions out of range'}, b''
            return self.frame(w, h, int(request.get('turn', 0)), request.get('have'), request.get('format') == 'jpg')
        if op == 'view': return self.change_view(float(request.get('scale', 1)), request.get('at', [0.5, 0.5]), request.get('move', [0, 0]), float(request.get('aspect', 4/3))), b''
        if op == 'tap': return self.tap(request.get('at', [0, 0]), bool(request.get('drag'))), b''
        if op == 'undo': return self.undo(), b''
        if op == 'start': return self.start(request.get('mode', 'auto'), request.get('path'), request.get('size', [4320, 3240])), b''
        if op == 'erase': return self.erase(bool(request.get('on')), request.get('size')), b''
        return {'error': 'unknown op'}, b''

    ERASERS = (12, 30, 60, 120)   # the sizes offered on the tablet palette, image pixels
    RUBBER = ('a) Eraser Circle', 'Eraser circle', 'a) Eraser Small', 'Eraser hard')   # the brush that stands in while the eraser end is used as the pen device: the first one this Krita has
    NIBS = (4, 10, 25, 60)        # brush sizes offered for the pen end, image pixels
    FADES = (25, 50, 75, 100)     # opacities offered for the active layer, percent

    def eraser(self, size=None):
        """The eraser's size: set only from the palette or --eraser, never learnt from whatever the
        brush size happens to be, and always within 4..200 px so a slip cannot wipe the picture."""
        app = Krita.instance()
        if size is None and not 4 <= (getattr(self, 'eraser_size', None) or 0) <= 200:
            try: size = float(app.readSetting('rm_viewport', 'eraser_size', '0') or 0)
            except ValueError: size = 0
            size = size if 4 <= size <= 200 else 30
        if size is not None:
            self.eraser_size = float(max(4, min(200, size))); app.writeSetting('rm_viewport', 'eraser_size', str(self.eraser_size))
        return self.eraser_size

    def erase(self, on, size=None):
        """The bridge saying the Marker's eraser end came near or left.

        Two ways the eraser end can reach Krita, both handled:
        - as the same pen device (the bridge's default): Krita sees one stylus, so this request is
          the only signal. Erase mode and the eraser's size are lent to the brush state in use and
          taken back, with the pen's own size, when the eraser end leaves.
        - as a real eraser device (`--eraser-device`): Krita keeps a separate brush state per end and
          swaps them itself when the first event of the other end arrives. Then the tablet events
          decide (see eraser_end); a request that got in before Krita's swap is undone when the pen
          end returns, so the pen is never left erasing."""
        if size: self.eraser(float(size))
        self.asked = bool(on); self.eraser_client = getattr(self, 'client', None) if on else None; self.erase_sync()
        return {'erase': bool(self.repaints.rubber or getattr(self, 'lent', None) is not None), 'eraser_size': self.eraser()}

    def erase_sync(self):
        if not hasattr(self, 'erase_timer'):
            self.erase_timer = QTimer(); self.erase_timer.setInterval(150); self.erase_timer.timeout.connect(self.erase_hold)
        app = Krita.instance(); action = app.action('erase_action'); window = app.activeWindow(); view = window.activeView() if window else None
        asked = getattr(self, 'asked', False); lent = getattr(self, 'lent', None)
        if action is not None and view is not None and not self.repaints.rubber:
            if getattr(self, 'swapped', False): asked = self.asked = False; self.swapped = False   # a real eraser device was seen and the pen is back
            if asked and lent is None:
                # The pen's brush and size, to give back. A plain round eraser stands in meanwhile: erase
                # mode on a textured or faint brush barely erases.
                self.lent = [view.currentBrushPreset(), view.brushSize()]
                presets = Krita.instance().resources('preset'); rubber = next((presets[name] for name in self.RUBBER if name in presets), None)
                if rubber is not None: view.setCurrentBrushPreset(rubber)
            elif not asked and lent is not None:
                if lent[0] is not None: view.setCurrentBrushPreset(lent[0])
                if action.isChecked() and (view.currentBlendingMode() == 'erase' or getattr(self, 'forced', False)): action.trigger()
                self.forced = False
                view.setBrushSize(float(lent[1])); self.lent = None; self.nib_apply()
        elif self.repaints.rubber and asked: self.swapped = True
        if self.repaints.rubber or (asked and getattr(self, 'lent', None) is not None): self.erase_hold(); self.erase_timer.start()
        else: self.erase_timer.stop()

    def eraser_end(self):
        """The stylus end Krita is shown changed (a real eraser device). Krita has just swapped to
        that end's own brush state: the eraser end's is made to erase at the eraser's size, the pen
        end's is left as Krita restored it, apart from undoing a request that beat the swap."""
        self.erase_sync()
        if not self.repaints.rubber: self.nib_apply()

    def nib_apply(self):
        size = getattr(self, 'nib', None); window = Krita.instance().activeWindow(); view = window.activeView() if window else None
        if size is None or view is None: return
        if getattr(self, 'lent', None) is not None: self.lent[1] = float(size); self.nib = None; self.pen_size = float(size); return   # given back with the pen
        if self.repaints.rubber: return
        view.setBrushSize(float(size)); self.nib = None; self.pen_size = float(size)

    def erasing(self):
        return bool(self.repaints.rubber or (getattr(self, 'asked', False) and getattr(self, 'lent', None) is not None))

    def erase_hold(self):
        """Keeps erase mode and the eraser's size in place while the eraser end is in use, whatever is pressed meanwhile."""
        app = Krita.instance(); action = app.action('erase_action')
        window = app.activeWindow(); view = window.activeView() if window else None
        if not self.erasing() or action is None or view is None: return False
        if view.currentBlendingMode() != 'erase' and not action.isChecked():   # an eraser preset erases by itself; any other brush needs erase mode
            action.trigger(); self.forced = getattr(self, 'lent', None) is not None
        if abs(view.brushSize()-self.eraser()) > 0.01: view.setBrushSize(self.eraser())
        return True

    def frame(self, w, h, turn, have, jpeg=False):
        if not (16 <= w <= 4096 and 16 <= h <= 4096 and turn in (0, 90, 180, 270)): raise ValueError('bad frame request')
        scene = Scene(w/h); document = scene.document
        geometry = QApplication.primaryScreen().virtualGeometry()
        window = scene.view.window().qwindow(); offset = scene.widget.mapTo(window, QPoint(0, 0))
        head = {'rect': scene.screen_rect(), 'screen': [geometry.width(), geometry.height()],
                'desktop': [geometry.x(), geometry.y(), geometry.width(), geometry.height()],
                'native_window': int(window.winId()), 'pixel_ratio': float(window.devicePixelRatioF()),
                'canvas_offset': [offset.x()+scene.rect.x(), offset.y()+scene.rect.y()],
                'zoom': scene.canvas.zoomLevel(), 'name': os.path.basename(document.fileName()) or 'unsaved'}
        self.repaints.watch(scene.widget)
        if self.repaints.count != getattr(self, 'docked', None):   # not on idle polls
            if self.asking() is None: self.docked = self.repaints.count; self.docks()
        self.frame_size = (w, h)
        from . import references
        dock = references.ensure(scene.view.window().qwindow())
        editing = bool(dock is not None and hasattr(dock, 'editing') and dock.editing(document))
        head['editing_reference'] = editing
        head['zones'] = None
        # The image is told apart by its root layer's id: id(document) is a new Python wrapper on every
        # call, so the unchanged-picture shortcut below never hit and every idle poll drew a whole frame.
        state = (w, h, turn, self.repaints.count, document.rootNode().uniqueId().toString(), tuple(head['rect']), self.palette_state(scene), editing)
        head['zones'] = self.palette_layout()['zones']; head['frame'] = [w, h]
        if have and self.last == (state, have): head['hash'] = have; return head, b''
        scale = w/scene.rect.width()
        to_frame = scene.to_widget*QTransform.fromTranslate(-scene.rect.x(), -scene.rect.y())*QTransform.fromScale(scale, scale)   # image px -> frame px
        back, ok = to_frame.inverted()
        need = back.mapRect(QRectF(0, 0, w, h)).toAlignedRect().adjusted(-1, -1, 1, 1).intersected(document.bounds())
        image = QImage(w, h, RGB32); image.fill(SURROUND)
        source = None
        if editing:
            # Document projection omits transform handles and (in overlay preview mode) the
            # moving image itself. During placement mirror the actual canvas so the Paper
            # Pro shows precisely the handles under its pen. Painting still uses sharp pixels.
            # Read the window's already rendered pixels; QWidget.grab() re-enters Krita's
            # live transform paint path and can crash it. Only this Krita window is read.
            window = scene.view.window().qwindow()
            source = window.screen().grabWindow(int(window.winId())).toImage()
        if source is not None and not source.isNull():
            offset = scene.widget.mapTo(window, QPoint(0, 0)); ratio = source.devicePixelRatio()
            crop = QRectF((offset.x()+scene.rect.x())*ratio, (offset.y()+scene.rect.y())*ratio,
                          scene.rect.width()*ratio, scene.rect.height()*ratio)
            painter = QPainter(image); painter.setRenderHint(SMOOTH, True)
            painter.drawImage(QRectF(0, 0, w, h), source, crop); painter.end()
        elif not need.isEmpty():
            source = document.projection(need.x(), need.y(), need.width(), need.height())
            painter = QPainter(image); painter.setTransform(to_frame)
            # Smooth only when shrinking; magnified pixels stay exact.
            painter.setRenderHint(SMOOTH, abs(to_frame.m11())+abs(to_frame.m12()) < 1)
            painter.fillRect(QRectF(document.bounds()), Qt.white); painter.drawImage(QPointF(need.x(), need.y()), source); painter.end()
        self.draw_palette(image, scene)
        if turn: image = image.transformed(QTransform().rotate(turn))
        bits = image.constBits(); bits.setsize(image.sizeInBytes())
        head['hash'] = hashlib.blake2b(bytes(bits), digest_size=12).hexdigest()
        self.last = (state, head['hash'])
        if head['hash'] == have: return head, b''
        head['format'] = 'jpg' if jpeg else 'png'
        return head, image

    # ---- on-tablet palette: a dot that opens the last five brushes and a colour wheel ----
    # Nothing has a background: only the dot, round brush icons and the wheel sit on the picture.
    HUB, DOT, ORBIT, ICON, WHEEL, RING = (200, 200), 46, 132, 54, 150, 34

    def palette_layout(self):
        hub = QPointF(*self.HUB); lay = {'hub': hub, 'zones': [[hub.x(), hub.y(), self.DOT+12]]}
        if getattr(self, 'reference_label', None):
            lay['reference'] = QPointF(self.frame_size[0]-110, 110)
            lay['zones'].append([lay['reference'].x(), lay['reference'].y(), 82])
        if getattr(self, 'collapsed', True): return lay
        lay['icons'] = [QPointF(hub.x()+self.ORBIT*math.cos(math.radians(-90+72*k)), hub.y()+self.ORBIT*math.sin(math.radians(-90+72*k))) for k in range(5)]
        wheel = QPointF(hub.x(), hub.y()+self.ORBIT+self.ICON+34+self.WHEEL); lay['wheel'] = wheel
        reach = self.WHEEL-self.RING-8   # triangle corners: pure colour on top, black bottom-left, white bottom-right
        lay['corners'] = [QPointF(wheel.x()+reach*math.cos(math.radians(t)), wheel.y()-reach*math.sin(math.radians(t))) for t in (90, 210, 330)]
        row = wheel.y()+self.WHEEL+70; lay['erasers'] = [(QPointF(hub.x()-150+k*100, row), 14+k*8, size) for k, size in enumerate(self.ERASERS)]
        lay['nibs'] = [(QPointF(hub.x()-150+k*100, row+100), 8+k*8, size) for k, size in enumerate(self.NIBS)]
        lay['steps'] = [(QPointF(hub.x()+270, row), 'edit_undo'), (QPointF(hub.x()+270, row+100), 'edit_redo')]
        lay['zones'] += [[c.x(), c.y(), 46] for c, _ in lay['steps']]
        lay['layers'] = [(QPointF(hub.x()-150, row+215+k*92), node) for k, node in enumerate(getattr(self, 'shown_layers', ()))]
        lay['plus'] = QPointF(hub.x()-150, row+215+len(lay['layers'])*92)
        lay['fades'] = [(QPointF(lay['plus'].x()+110+k*92, lay['plus'].y()), percent) for k, percent in enumerate(self.FADES)]
        lay['zones'] += [[c.x(), c.y(), 46] for c, _ in lay['layers']+lay['fades']]+[[lay['plus'].x(), lay['plus'].y(), 46]]
        lay['zones'] += [[c.x()+reach, c.y(), 46] for c, _ in lay['layers'] for reach in (88, 176)]   # the name beside a layer selects it too
        lay['zones'] += [[c.x(), c.y(), self.ICON+6] for c in lay['icons']]+[[wheel.x(), wheel.y(), self.WHEEL+12]]+[[c.x(), c.y(), 46] for c, _, _ in lay['erasers']+lay['nibs']]
        return lay

    @staticmethod
    def weights(corners, x, y):
        """Barycentric weights of a point in the colour triangle (pure colour, black, white)."""
        (ax, ay), (bx, by), (cx, cy) = [(c.x(), c.y()) for c in corners]
        det = (by-cy)*(ax-cx)+(cx-bx)*(ay-cy)
        a = ((by-cy)*(x-cx)+(cx-bx)*(y-cy))/det; b = ((cy-ay)*(x-cx)+(ax-cx)*(y-cy))/det
        return a, b, 1-a-b

    def favourites(self):
        """Krita's "My Favorites" brushes (not erasers), read once from its resource database,
        read-only. They fill the brush ring until five brushes have actually been used."""
        found = []
        try:
            import sqlite3
            base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
            db = sqlite3.connect('file:'+os.path.join(base, 'krita', 'resourcecache.sqlite')+'?mode=ro', uri=True, timeout=0.2)
            try:
                names = [row[0] for row in db.execute("select r.name from tags t join resource_tags rt on rt.tag_id = t.id join resources r on r.id = rt.resource_id "
                                                      "where rt.active = 1 and t.url like '%Favorites%' order by r.name")]
            finally: db.close()
            presets = Krita.instance().resources('preset')
            for name in names:
                preset = presets.get(name) or presets.get(name.replace(' ', '_'))
                if preset and 'eraser' not in name.lower(): found.append({'name': preset.name(), 'preset': preset, 'image': preset.image(), 'used': 0})
        except Exception: pass
        return found

    def layers(self, document):
        """Up to six layers around the active one, topmost first, as Krita's layer box lists them."""
        found = []
        def walk(node):
            for child in reversed(node.childNodes()):
                found.append(child); walk(child)
        try: walk(document.rootNode()); active = document.activeNode()
        except Exception: return [], None
        at = next((k for k, node in enumerate(found) if active is not None and node.uniqueId() == active.uniqueId()), 0)
        first = max(0, min(at-2, len(found)-6)); self.unshown = (first, max(0, len(found)-first-6))   # layers above / below the six
        return found[first:first+6], active

    TONED_ROUND = 'b) Basic-2 Opacity'
    TONE = '0,0.04;1,1;'   # opacity against pressure: a whisper at the lightest touch, solid at full pressure
    def pressure_tone(self, view, preset, name):
        """Pressure is tone, never width: for every brush the pen uses, pressure drives opacity
        (TONE; full pressure is what the bridge puts at half of what the Marker can sense) and the
        line keeps the width set on the palette, whatever the brush shipped with. Brushes with a very
        soft tip laid down in many faint dabs (e) Marker Dry) still leave nothing below about 15%
        pressure: each dab rounds to zero in an 8-bit layer. Only Krita's copy
        in memory changes, never the brush file. Off with rm_viewport/pressure_tone = 0 in kritarc."""
        if not hasattr(self, 'toned'): self.toned = set()
        if name in self.toned: return
        self.toned.add(name)
        try:
            if Krita.instance().readSetting('rm_viewport', 'pressure_tone', '1') == '0' or view.currentBlendingMode() == 'erase': return
            from krita import Preset
            import re
            handle = Preset(preset); xml = handle.toXML()
            if 'paintopid="roundmarker"' in xml:
                # The quick-brush engine (Basic-1) cannot vary opacity at all. The first time it turns up
                # (it is Krita's starting brush) the same round brush from the pixel engine takes its place.
                stand_in = Krita.instance().resources('preset').get(self.TONED_ROUND)
                if stand_in is not None and not getattr(self, 'stood_in', False):
                    self.stood_in = True; size = view.brushSize(); view.setCurrentBrushPreset(stand_in); view.setBrushSize(size)
                return
            if 'paintopid="paintbrush"' not in xml: return   # smudge, sketch and the other engines keep their own behaviour
            for key, value in (('PressureSize', 'false'), ('PressureOpacity', 'true'), ('OpacityUseCurve', 'true'),
                               ('OpacitySensor', f'<!DOCTYPE params> <params id="pressure"> <curve>{self.TONE}</curve> </params> ')):
                new = f'<param type="string" name="{key}"><![CDATA[{value}]]></param>'
                xml, found = re.subn(rf'<param[^>]*name="{key}"[^>]*>.*?</param>', lambda match: new, xml, flags=re.S)
                if not found: xml = xml.replace('</Preset>', new+'</Preset>')
            size = view.brushSize(); handle.fromXML(xml); view.setCurrentBrushPreset(preset); view.setBrushSize(size)
        except Exception: pass

    def palette_state(self, scene):
        """Follow Krita's brush and colour; returns what the palette shows, so a change redraws the frame."""
        if not hasattr(self, 'mix'): self.recent = []; self.uses = 0; self.hue = 0.0; self.mix = (0.0, 1.0, 0.0); self.shown_rgb = None; self.collapsed = True
        from . import references
        self.reference_dock = references.ensure(scene.view.window().qwindow())
        node = scene.document.activeNode(); self.reference_node = node
        self.reference_label = ('Done' if self.reference_dock.editing(scene.document) else 'Transform') if self.reference_dock is not None and node is not None and node.name().startswith(references.PREFIX) else None
        preset = scene.view.currentBrushPreset()
        name = preset.name() if preset else ''
        if preset and not self.repaints.rubber and getattr(self, 'lent', None) is None:
            self.pressure_tone(scene.view, preset, name)
            entry = next((e for e in self.recent if e['name'] == name), None)
            if entry is None:
                entry = {'name': name, 'preset': preset, 'image': preset.image(), 'used': 0}; self.recent.append(entry)
            if getattr(self, 'current', None) != name: self.uses += 1; entry['used'] = self.uses
            self.current = name; self.pen_size = round(scene.view.brushSize(), 2)
            if len(self.recent) > 5: self.recent.remove(min(self.recent, key=lambda e: e['used']))
        if not getattr(self, 'seeded', False):
            self.seeded = True
            for entry in self.favourites():
                if len(self.recent) < 5 and all(e['name'] != entry['name'] for e in self.recent): self.recent.append(entry)
        try: colour = scene.view.foregroundColor().colorForCanvas(scene.canvas)
        except Exception: colour = QColor(0, 0, 0)
        if colour.rgb() != self.shown_rgb:
            self.shown_rgb = colour.rgb(); high, low = max(colour.redF(), colour.greenF(), colour.blueF()), min(colour.redF(), colour.greenF(), colour.blueF())
            if high-low > 0.004 and colour.hsvHueF() >= 0: self.hue = colour.hsvHueF()
            self.mix = (high-low, 1-high, low)
        self.shown_layers, active = self.layers(scene.document) if not self.collapsed else ([], None)
        self.active_layer = active.uniqueId() if active is not None else None
        self.layer_fade = round(active.opacity()/2.55) if active is not None else None
        if self.collapsed: self.unshown = (0, 0)
        stack = tuple((node.uniqueId().toString(), node.name(), node.visible()) for node in self.shown_layers)+(str(self.active_layer), self.layer_fade, self.unshown)
        return (stack, self.collapsed, tuple(e['name'] for e in self.recent), getattr(self, 'current', ''), self.shown_rgb, round(self.hue, 4), self.eraser(), getattr(self, 'pen_size', 0), self.reference_label)

    def triangle(self, corners):
        """The colour triangle for the current hue, as an image (cached per hue)."""
        key = round(self.hue, 3)
        if getattr(self, 'triangle_cache', (None,))[0] == key: return self.triangle_cache[1], self.triangle_cache[2]
        left, right = int(min(c.x() for c in corners)), int(max(c.x() for c in corners))+1
        top, bottom = int(min(c.y() for c in corners)), int(max(c.y() for c in corners))+1
        w, h = right-left, bottom-top; pure = QColor.fromHsvF(min(self.hue, 0.9999), 1, 1); pr, pg, pb = pure.red(), pure.green(), pure.blue()
        data = bytearray(w*h*4)
        for y in range(h):
            for x in range(w):
                a, b, c = self.weights(corners, left+x+0.5, top+y+0.5)
                if a < -0.01 or b < -0.01 or c < -0.01: continue
                a, c = max(0.0, a), max(0.0, c); white = 255*c; i = (y*w+x)*4
                data[i] = min(255, int(a*pb+white)); data[i+1] = min(255, int(a*pg+white)); data[i+2] = min(255, int(a*pr+white)); data[i+3] = 255
        image = QImage(bytes(data), w, h, w*4, QImage.Format_ARGB32).copy()
        self.triangle_cache = (key, image, QPointF(left, top)); return image, QPointF(left, top)

    def draw_palette(self, image, scene):
        lay = self.palette_layout(); painter = QPainter(image); painter.setRenderHint(QPainter.Antialiasing, True); painter.setRenderHint(SMOOTH, True)
        black, white = QColor(0, 0, 0), QColor(255, 255, 255)
        def ring(centre, radius, weight):   # readable on any picture: white halo, black line
            painter.setBrush(Qt.NoBrush); painter.setPen(QPen(white, weight+6)); painter.drawEllipse(centre, radius, radius)
            painter.setPen(QPen(black, weight)); painter.drawEllipse(centre, radius, radius)
        if 'reference' in lay:
            centre = lay['reference']; painter.setPen(Qt.NoPen); painter.setBrush(white); painter.drawEllipse(centre, 74, 74); ring(centre, 74, 4)
            painter.save(); font = painter.font(); font.setPixelSize(24); painter.setFont(font); painter.setPen(black)
            painter.drawText(QRectF(centre.x()-72, centre.y()-35, 144, 70), int(getattr(Qt, 'AlignCenter', None) or Qt.AlignmentFlag.AlignCenter), self.reference_label)
            painter.restore()
        if 'icons' in lay:
            for centre, entry in zip(lay['icons'], self.recent):
                r = self.ICON; box = QRectF(centre.x()-r, centre.y()-r, 2*r, 2*r)
                painter.save(); path = QPainterPath(); path.addEllipse(centre, r, r); painter.setClipPath(path)
                painter.fillRect(box, white)
                if not entry['image'].isNull(): painter.drawImage(box, entry['image'])
                painter.restore(); ring(centre, r, 11 if entry['name'] == getattr(self, 'current', '') else 4)
            wheel = lay['wheel']; mid = self.WHEEL-self.RING/2
            hues = QConicalGradient(wheel, 0)
            for k in range(13): hues.setColorAt(k/12, QColor.fromHsvF((k % 12)/12, 1, 1))
            painter.setBrush(Qt.NoBrush); painter.setPen(QPen(hues, self.RING)); painter.drawEllipse(wheel, mid, mid)
            ring(wheel, self.WHEEL, 3); ring(wheel, self.WHEEL-self.RING, 3)
            picture, origin = self.triangle(lay['corners']); painter.drawImage(origin, picture)
            outline = QPainterPath(); outline.moveTo(lay['corners'][0]); outline.lineTo(lay['corners'][1]); outline.lineTo(lay['corners'][2]); outline.closeSubpath()
            painter.setBrush(Qt.NoBrush); painter.setPen(QPen(black, 3)); painter.drawPath(outline)
            angle = self.hue*2*math.pi; a, b, c = self.mix; k = lay['corners']
            for spot in (QPointF(wheel.x()+mid*math.cos(angle), wheel.y()-mid*math.sin(angle)),
                         QPointF(a*k[0].x()+b*k[1].x()+c*k[2].x(), a*k[0].y()+b*k[1].y()+c*k[2].y())):
                painter.setPen(QPen(black, 5)); painter.drawEllipse(spot, 14, 14); painter.setPen(QPen(white, 3)); painter.drawEllipse(spot, 10, 10)
        for centre, radius, size in lay.get('erasers', ()):   # eraser sizes: white discs, the chosen one ringed heavily
            painter.setPen(Qt.NoPen); painter.setBrush(white); painter.drawEllipse(centre, radius, radius); ring(centre, radius, 10 if size == self.eraser() else 3)
        for centre, node in lay.get('layers', ()):            # layers: round thumbnails, the active one ringed and named
            r = 38; box = QRectF(centre.x()-r, centre.y()-r, 2*r, 2*r); mine = node.uniqueId() == self.active_layer
            painter.save(); path = QPainterPath(); path.addEllipse(centre, r, r); painter.setClipPath(path); painter.fillRect(box, white)
            try:
                small = node.thumbnail(2*r, 2*r)
                if not small.isNull(): painter.drawImage(box, small)
            except Exception: pass
            painter.restore(); ring(centre, r, 9 if mine else 3)
            if not node.visible():   # hidden: struck through
                painter.setPen(QPen(white, 9)); painter.drawLine(QPointF(centre.x()-r, centre.y()+r), QPointF(centre.x()+r, centre.y()-r))
                painter.setPen(QPen(black, 4)); painter.drawLine(QPointF(centre.x()-r, centre.y()+r), QPointF(centre.x()+r, centre.y()-r))
            ends = lay['layers']; above, below = getattr(self, 'unshown', (0, 0))   # more layers than fit: counted beside the first and last
            more = f'▲{above}' if above and node is ends[0][1] else f'▼{below}' if below and node is ends[-1][1] else ''
            # every layer is named (thumbnails this small look alike); the active one larger and bold
            font = painter.font(); font.setPixelSize(34 if mine else 26); font.setBold(mine); label = QPainterPath()
            label.addText(centre.x()+r+18, centre.y()+(12 if mine else 9), font, node.name()[:22]+('   '+more if more else ''))
            painter.setPen(QPen(white, 7)); painter.setBrush(Qt.NoBrush); painter.drawPath(label); painter.setPen(Qt.NoPen); painter.setBrush(black); painter.drawPath(label)
        for centre, name in lay.get('steps', ()):             # undo above, redo below: a white disc with a turning arrow
            r = 34; back = name == 'edit_undo'; painter.setPen(Qt.NoPen); painter.setBrush(white); painter.drawEllipse(centre, r, r); ring(centre, r, 3)
            arc = QPainterPath(); box = QRectF(centre.x()-17, centre.y()-15, 34, 34); arc.arcMoveTo(box, 200 if back else -20); arc.arcTo(box, 200 if back else -20, -220 if back else 220)
            painter.setBrush(Qt.NoBrush); painter.setPen(QPen(black, 6)); painter.drawPath(arc)
            tip = arc.pointAtPercent(0); side = -1 if back else 1; head = QPainterPath()
            head.moveTo(tip.x()-side*13, tip.y()-3); head.lineTo(tip.x()+side*7, tip.y()-9); head.lineTo(tip.x()+side*3, tip.y()+13); head.closeSubpath()
            painter.setPen(Qt.NoPen); painter.setBrush(black); painter.drawPath(head)
        if 'plus' in lay:                                     # new layer above the active one
            c = lay['plus']; painter.setPen(Qt.NoPen); painter.setBrush(white); painter.drawEllipse(c, 30, 30); ring(c, 30, 3)
            painter.setPen(QPen(black, 6)); painter.drawLine(QPointF(c.x()-14, c.y()), QPointF(c.x()+14, c.y())); painter.drawLine(QPointF(c.x(), c.y()-14), QPointF(c.x(), c.y()+14))
        for centre, percent in lay.get('fades', ()):          # active layer's opacity: paler disc = more see-through
            grey = round(255*(1-percent/100)); painter.setPen(Qt.NoPen); painter.setBrush(QColor(grey, grey, grey)); painter.drawEllipse(centre, 26, 26)
            ring(centre, 26, 9 if self.layer_fade is not None and abs(self.layer_fade-percent) <= 6 else 3)
        for centre, radius, size in lay.get('nibs', ()):      # brush sizes: black discs
            painter.setPen(Qt.NoPen); painter.setBrush(black); painter.drawEllipse(centre, radius, radius)
            painter.setBrush(Qt.NoBrush); painter.setPen(QPen(white, 4)); painter.drawEllipse(centre, radius+2, radius+2)
            if abs(size-getattr(self, 'pen_size', 0)) < 0.01: ring(centre, radius+12, 6)
        painter.setPen(Qt.NoPen); painter.setBrush(QColor(self.shown_rgb)); painter.drawEllipse(lay['hub'], self.DOT, self.DOT); ring(lay['hub'], self.DOT, 5)
        painter.end()

    def tap(self, at, drag=False):
        """A tap at (u, v) of the tablet picture, by finger or pen: works the palette, otherwise misses."""
        scene = Scene(self.frame_size[0]/self.frame_size[1]); self.palette_state(scene)
        x, y = float(at[0])*self.frame_size[0], float(at[1])*self.frame_size[1]; lay = self.palette_layout()
        near = lambda centre, radius: math.hypot(x-centre.x(), y-centre.y()) <= radius
        if drag and not (lay.get('wheel') and getattr(self, 'grab', None)): return {'hit': 'drag'}   # a held pen only works the wheel it started on
        if not drag: self.grab = None
        if near(lay['hub'], self.DOT+14): self.collapsed = not self.collapsed; self.last = None; return {'hit': 'dot', 'open': not self.collapsed}
        if not drag and lay.get('reference') is not None and near(lay['reference'], 82):
            label = self.reference_label; self.last = None
            if label == 'Done': self.reference_dock.finish(); self.reference_dock.refresh()
            else: self.reference_dock.edit(self.reference_node)
            return {'hit': 'reference', 'action': label}
        if 'icons' not in lay: return {'hit': False}
        for centre, entry in zip(lay['icons'], self.recent):
            if near(centre, self.ICON+6):
                view = scene.view; self.last = None
                if getattr(self, 'lent', None) is not None: self.lent[0] = entry['preset']; self.current = entry['name']   # eraser end in use: the pen gets it back
                else: (getattr(view, 'setCurrentBrushPreset', None) or view.activateResource)(entry['preset'])
                return {'hit': 'brush', 'name': entry['name']}
        for centre, radius, size in lay['erasers']:
            if not drag and near(centre, 46):
                self.eraser(size); self.last = None
                self.erase_hold()
                return {'hit': 'eraser', 'size_px': size}
        for centre, name in lay['steps']:
            if not drag and near(centre, 46):
                action = Krita.instance().action(name); done = bool(action is not None and action.isEnabled())
                if done: action.trigger()
                return {'hit': name.split('_')[1], 'done': done}
        for centre, node in lay['layers']:
            if not drag and (near(centre, 46) or (abs(y-centre.y()) <= 46 and centre.x() <= x <= centre.x()+222)):
                self.last = None
                if node.uniqueId() == self.active_layer:   # tapping the layer already in use shows or hides it
                    node.setVisible(not node.visible()); scene.document.refreshProjection()
                    return {'hit': 'layer', 'name': node.name(), 'visible': node.visible()}
                scene.document.setActiveNode(node)
                return {'hit': 'layer', 'name': node.name()}
        for centre, percent in lay['fades']:
            if not drag and near(centre, 46):
                node = scene.document.activeNode(); self.last = None
                if node is None: return {'hit': False}
                node.setOpacity(round(percent*2.55)); scene.document.refreshProjection()
                return {'hit': 'opacity', 'percent': percent, 'name': node.name()}
        if not drag and near(lay['plus'], 46):
            document = scene.document; active = document.activeNode(); self.last = None
            count = sum(1 for n in document.rootNode().childNodes() if n.name().startswith('Layer '))
            node = document.createNode(f'Layer {count+1}', 'paintLayer')
            parent = active.parentNode() if active is not None and active.parentNode() is not None else document.rootNode()
            parent.addChildNode(node, active); document.setActiveNode(node)
            return {'hit': 'new layer', 'name': node.name()}
        for centre, radius, size in lay['nibs']:
            if not drag and near(centre, 46):
                # The size belongs to the pen end's brush state; with the eraser end in use it waits for the pen.
                self.nib = size; self.last = None; self.nib_apply()
                return {'hit': 'nib', 'size_px': size}
        wheel = lay['wheel']; far = math.hypot(x-wheel.x(), y-wheel.y())
        if not drag: self.grab = None if far > self.WHEEL+14 else 'ring' if far >= self.WHEEL-self.RING-2 else 'triangle'
        grab = getattr(self, 'grab', None)   # a held pen stays on the part it started on
        if not grab: return {'hit': False}
        if grab == 'ring':
            self.hue = (math.degrees(math.atan2(-(y-wheel.y()), x-wheel.x())) % 360)/360
            if self.mix[0] < 0.05: self.mix = (1.0, 0.0, 0.0)   # from grey, turning the ring gives the pure colour
        else:
            a, b, c = (max(0.0, w) for w in self.weights(lay['corners'], x, y)); total = a+b+c or 1; self.mix = (a/total, b/total, c/total)
        pure = QColor.fromHsvF(min(self.hue, 0.9999), 1, 1); a, b, c = self.mix
        colour = QColor.fromRgbF(min(1.0, a*pure.redF()+c), min(1.0, a*pure.greenF()+c), min(1.0, a*pure.blueF()+c))
        try: managed = ManagedColor.fromQColor(colour, scene.canvas)
        except Exception:
            managed = ManagedColor('RGBA', 'U8', ''); managed.setComponents([colour.blueF(), colour.greenF(), colour.redF(), 1.0])
        scene.view.setForeGroundColor(managed); self.shown_rgb = colour.rgb(); self.last = None
        return {'hit': 'colour', 'rgb': colour.name()}

    def undo(self):
        action = Krita.instance().action('edit_undo')
        if action is None or not action.isEnabled(): return {'undo': False}
        action.trigger(); return {'undo': True}

    def asking(self):
        """The title of a dialog Krita is waiting on (file recovery at start-up, a save question),
        else None. Nothing is created or docked behind such a dialog: Krita is in the middle of
        something, and a new canvas made at that moment preceded two crashes on 2026-10-08."""
        from PyQt5.QtWidgets import QApplication
        modal = QApplication.activeModalWidget()
        return None if modal is None else (modal.windowTitle() or 'a dialog')

    def start(self, mode, path, size):
        """Begin a session: a new canvas (never saved unless you save it), or an existing piece; full screen."""
        app = Krita.instance(); window = app.activeWindow()
        if window is None: return {'idle': 'Krita is starting'}
        asking = self.asking()
        if asking is not None: return {'idle': f'Krita is asking something first ({asking}); answer it on the laptop'}
        done = 'kept the open image'
        if mode == 'open' and path:
            view = next((v for v in window.views() if v.document() and v.document().fileName() == path), None)
            if view: window.showView(view)
            else:
                document = app.openDocument(path)
                if document is None: return {'error': f'could not open {path}'}
                window.addView(document)
            done = 'opened '+os.path.basename(path)
        elif mode == 'new' or not app.documents():
            w, h = int(size[0]), int(size[1])
            if not (64 <= w <= 16384 and 64 <= h <= 16384): raise ValueError('bad canvas size')
            document = app.createDocument(w, h, '', 'RGBA', 'U8', '', 300.0)
            layers = document.rootNode().childNodes()
            if layers and bytes(layers[0].pixelData(0, 0, 1, 1))[3:4] != b'\xff':
                layers[0].setPixelData(QByteArray(b'\xff'*(w*h*4)), 0, 0, w, h); document.refreshProjection()
            window.addView(document); done = f'new {w}x{h} canvas (not saved unless you save it)'
        full = app.action('fullscreen')   # maximised, not full screen: the desktop's corners and panels keep working
        if full is not None and full.isCheckable() and full.isChecked(): full.trigger()
        if not window.qwindow().isMaximized(): window.qwindow().showMaximized()
        return {'started': done}

    def change_view(self, scale, at, move, aspect):
        """Zoom about a point of the rectangle and drag the artwork with the fingers."""
        if not (0.2 < aspect < 5 and 0.2 < scale < 5): raise ValueError('bad view request')
        scene = Scene(aspect)
        rect, canvas = scene.rect, scene.canvas
        anchor = QPointF(rect.x()+at[0]*rect.width(), rect.y()+at[1]*rect.height())      # widget px, before
        target = QPointF(anchor.x()+move[0]*rect.width(), anchor.y()+move[1]*rect.height())
        pinned = scene.to_image.map(anchor)                                                # image px that must end at target
        old = canvas.zoomLevel(); new = max(MIN_ZOOM, min(MAX_ZOOM, old*scale))
        if abs(new-old) > 1e-6: canvas.setZoomLevel(new)
        linear = Scene(aspect).to_image
        centre = QPointF(scene.widget.width()/2, scene.widget.height()/2)
        offset = QPointF(centre.x()-target.x(), centre.y()-target.y())
        delta = QPointF(linear.m11()*offset.x()+linear.m21()*offset.y(), linear.m12()*offset.x()+linear.m22()*offset.y())
        canvas.setPreferredCenter(QPointF(pinned.x()+delta.x(), pinned.y()+delta.y()))
        return {'zoom': canvas.zoomLevel()}
