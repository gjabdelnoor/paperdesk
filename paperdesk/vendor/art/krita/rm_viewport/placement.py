"""A visible rotation grip and reference context menu using Krita's native transform.

Only the small grip receives pointer events; the native canvas keeps its move/resize
handles. Changes go through the tool's Qt slots, retaining its preview and undo.
"""
import math
from krita import Krita
try:
    from PyQt5.QtCore import Qt, QEvent, QMetaObject, Q_ARG, QPoint, QPointF, QTimer
    from PyQt5.QtGui import QColor, QPainter, QPen
    from PyQt5.QtWidgets import QDoubleSpinBox, QMenu, QRadioButton, QSpinBox, QToolButton, QWidget
except ImportError:
    from PyQt6.QtCore import Qt, QEvent, QMetaObject, Q_ARG, QPoint, QPointF, QTimer
    from PyQt6.QtGui import QColor, QPainter, QPen
    from PyQt6.QtWidgets import QDoubleSpinBox, QMenu, QRadioButton, QSpinBox, QToolButton, QWidget

def enum(group, name): return getattr(Qt, name, None) or getattr(getattr(Qt, group), name)
def kind(name): return getattr(QEvent, name, None) or getattr(QEvent.Type, name)
def global_point(event): return event.globalPos() if hasattr(event, 'globalPos') else event.globalPosition().toPoint()
DIRECT = enum('ConnectionType', 'DirectConnection')
LEFT = enum('MouseButton', 'LeftButton')
RIGHT = enum('MouseButton', 'RightButton')
TRANSLUCENT = enum('WidgetAttribute', 'WA_TranslucentBackground')
TABLET = enum('WidgetAttribute', 'WA_TabletTracking')
NOFOCUS = enum('FocusPolicy', 'NoFocus')
ANTIALIAS = getattr(QPainter, 'Antialiasing', None) or QPainter.RenderHint.Antialiasing

class Placement(QWidget):
    def __init__(self, dock):
        super().__init__(dock.parentWidget()); self.dock = dock
        self.setObjectName('rm_reference_rotation_handle')
        self.resize(30, 54); self.setAttribute(TRANSLUCENT); self.setAttribute(TABLET)
        self.setFocusPolicy(NOFOCUS); self.setCursor(enum('CursorShape', 'OpenHandCursor'))
        self.setToolTip('Drag to rotate freely. Right-click for aspect ratio lock.')
        self.locked = True; self.pending_lock = False; self.drag = None; self.suppress_context = False; self.hide()
        self.last_geometry = None
        self.controls = None; self.pending_angle = None
        self.timer = QTimer(self); self.timer.setInterval(60); self.timer.timeout.connect(self.sync)
        self.rotation_timer = QTimer(self); self.rotation_timer.setSingleShot(True)
        self.rotation_timer.setInterval(16); self.rotation_timer.timeout.connect(self.flush_rotation)

    def options(self):
        # Searching the main window takes ~25 ms here. Resolve once per edit,
        # never for every mouse event or every stationary-handle timer tick.
        if self.controls is None:
            widget = self.dock.parentWidget().findChild(QWidget, 'KisToolTransform option widget')
            if widget is None: raise RuntimeError('Transform controls unavailable')
            controls = {'options': widget}
            for name in ('aZBox', 'aspectButton', 'translateXBox', 'translateYBox', 'scaleYBox', 'freeRotationRadioButton'):
                controls[name] = widget.findChild(QWidget, name)
                if controls[name] is None: raise RuntimeError('Transform control unavailable: '+name)
            controls['angle'] = controls['aZBox'].findChild(QDoubleSpinBox)
            widget.destroyed.connect(self.clear_controls)
            self.controls = controls
        return self.controls['options']

    def clear_controls(self, *args): self.controls = None

    def control(self, name):
        self.options(); return self.controls[name]

    def invoke(self, slot, value=None):
        args = [] if value is None else [Q_ARG('bool' if isinstance(value, bool) else 'double', value)]
        QMetaObject.invokeMethod(self.options(), slot, DIRECT, *args)

    def angle(self):
        return self.control('angle').value()

    def set_angle(self, angle):
        # setAngle updates both the native angle control and the transform config.
        control = self.control('aZBox')
        QMetaObject.invokeMethod(control, 'setAngle', DIRECT, Q_ARG('double', float(angle)))

    def lock(self, on):
        self.invoke('slotSetKeepAspectRatio', bool(on)); self.locked = bool(on)
        if on: self.lock_icon = self.aspect_icon()

    def aspect_icon(self):
        # KoAspectButton has a private keepAspectRatio() getter, and isChecked()
        # always returns false. Its chain icon reflects the native state, including
        # changes made in Krita's own Tool Options rather than in our menu.
        return self.control('aspectButton').icon().pixmap(19, 34).toImage()

    def begin(self, bounds):
        # In-place preview temporarily empties the layer's paint device. Capture
        # its bounds before activation so the grip does not collapse to the pivot.
        self.source_bounds = bounds
        self.pending_lock = True; self.lock(True); self.timer.start(); self.sync()

    def end(self):
        self.timer.stop(); self.pending_lock = False
        if self.drag is not None: self.release()
        self.hide(); self.last_geometry = None

    def scene(self):
        from .viewport import Scene
        if Krita.instance().activeWindow().qwindow() != self.dock.parentWidget(): return None
        scene = Scene(4/3)
        if not self.dock.editing(scene.document): return None
        return scene

    def geometry_points(self, scene):
        number = lambda name: self.control(name).value()
        centre = QPointF(number('translateXBox'), number('translateYBox'))
        # Scale/rotation/translation follow the native preview, with stable source bounds.
        bounds = self.source_bounds
        height = bounds.height()*abs(number('scaleYBox'))/100
        angle = math.radians(self.angle())
        top = centre + QPointF(math.sin(angle)*height/2, -math.cos(angle)*height/2)
        c, t = scene.to_widget.map(centre), scene.to_widget.map(top)
        length = math.hypot(t.x()-c.x(), t.y()-c.y())
        direction = QPointF((t.x()-c.x())/max(length, 1), (t.y()-c.y())/max(length, 1))
        return c, t, t+direction*34

    def sync(self):
        try:
            scene = self.scene()
            if scene is None: self.hide(); self.last_geometry = None; return
            self.locked = self.aspect_icon() == self.lock_icon
            # Krita initializes a new transform asynchronously, resetting its
            # arguments after activation. Enforce the default until its first
            # pointer gesture, then respect the user's context-menu choice.
            if self.pending_lock and not self.locked: self.lock(True)
            centre, top, grip = self.geometry_points(scene)
            # Keep the grip available even if the image reaches the canvas edge.
            grip.setX(max(15, min(scene.widget.width()-15, grip.x())))
            grip.setY(max(15, min(scene.widget.height()-15, grip.y())))
            self.centre = scene.widget.mapToGlobal(centre.toPoint())
            # Keep ownership in the main window: closing a document destroys its
            # canvas viewport, but this dock/handle must work for the next image.
            origin = QPointF(scene.widget.mapTo(self.parentWidget(), QPoint(0, 0)))
            x, y = round(origin.x()+grip.x()-15), round(origin.y()+grip.y()-15)
            stem = origin+top-QPointF(x, y); geometry = (x, y, round(stem.x(), 2), round(stem.y(), 2))
            if geometry != self.last_geometry:
                self.last_geometry = geometry; self.move(x, y); self.stem = stem
                self.show(); self.raise_(); self.update()
        except (RuntimeError, AttributeError): self.hide()

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(ANTIALIAS)
        painter.setPen(QPen(QColor('#2987d8'), 2))
        painter.drawLine(QPointF(15, 15), self.stem)
        painter.setBrush(QColor('#ffffff')); painter.drawEllipse(QPointF(15, 15), 10, 10)
        painter.setPen(QPen(QColor('#2987d8'), 2)); painter.drawArc(9, 9, 12, 12, 25*16, 280*16)

    def press(self, position):
        if self.pending_lock: self.lock(True); self.pending_lock = False
        self.sync()
        self.drag = (self.angle(), math.degrees(math.atan2(position.y()-self.centre.y(), position.x()-self.centre.x())))
        self.grabMouse(); self.setCursor(enum('CursorShape', 'ClosedHandCursor'))

    def move_pointer(self, position):
        if self.drag is None: return
        angle, start = self.drag
        current = math.degrees(math.atan2(position.y()-self.centre.y(), position.x()-self.centre.x()))
        self.pending_angle = (angle+current-start+180)%360-180
        # Replace pending samples, rather than queueing expensive native previews.
        if not self.rotation_timer.isActive(): self.rotation_timer.start()

    def flush_rotation(self):
        self.rotation_timer.stop()
        if self.pending_angle is None: return
        angle, self.pending_angle = self.pending_angle, None
        self.set_angle(angle); self.sync()

    def release(self):
        self.flush_rotation()  # Apply the exact final pointer position before Done.
        self.drag = None; self.releaseMouse(); self.setCursor(enum('CursorShape', 'OpenHandCursor'))

    def mousePressEvent(self, event):
        if event.button() == LEFT: self.press(global_point(event)); event.accept()
        else: event.ignore()
    def mouseMoveEvent(self, event): self.move_pointer(global_point(event)); event.accept()
    def mouseReleaseEvent(self, event): self.release(); event.accept()
    def tabletEvent(self, event):
        position = event.globalPosF() if hasattr(event, 'globalPosF') else event.globalPosition()
        if event.type() == kind('TabletPress'): self.press(position)
        elif event.type() == kind('TabletMove'): self.move_pointer(position)
        elif event.type() == kind('TabletRelease'): self.release()
        event.accept()

    def menu(self, position):
        if self.pending_lock: self.lock(True); self.pending_lock = False
        self.locked = self.aspect_icon() == self.lock_icon
        menu = QMenu(self)
        lock = menu.addAction('Lock aspect ratio'); lock.setCheckable(True); lock.setChecked(self.locked)
        lock.toggled.connect(self.lock)
        rotate = menu.addAction('Free rotate')
        def free_rotate():
            self.control('freeRotationRadioButton').click()
            self.dock.say('Drag the round handle above the image to rotate freely. Corners resize; Done applies.')
            self.sync()
        rotate.triggered.connect(free_rotate)
        menu.addAction('Reset rotation').triggered.connect(lambda: self.set_angle(0))
        menu.addSeparator(); menu.addAction('Done').triggered.connect(self.dock.finish)
        menu.exec(position)
        menu.deleteLater()

    def filter_event(self, target, event):
        if event.type() not in (kind('ContextMenu'), kind('MouseButtonPress'), kind('MouseButtonRelease'), kind('TabletPress'), kind('TabletRelease')) or self.dock.moving is None: return False
        if not isinstance(target, QWidget) or target.window() != self.dock.window(): return False
        current = target; canvas = target is self
        while current is not None:
            if current.metaObject().className() in ('KisCanvasController', 'KisOpenGLCanvas2', 'KisQPainterCanvas'):
                canvas = True; break
            current = current.parentWidget()
        document = self.dock.document()
        if not canvas or document is None or not self.dock.editing(document): return False
        if event.type() == kind('ContextMenu') and self.suppress_context:
            self.suppress_context = False; return True
        if event.type() in (kind('MouseButtonRelease'), kind('TabletRelease')):
            return event.button() == RIGHT
        context = event.type() == kind('ContextMenu') or event.button() == RIGHT
        if context:
            if event.type() != kind('ContextMenu'): self.suppress_context = True
            self.menu(global_point(event)); return True
        if self.pending_lock: self.lock(True); self.pending_lock = False
        return False
