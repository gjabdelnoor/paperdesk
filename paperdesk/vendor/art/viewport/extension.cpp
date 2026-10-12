// SPDX-License-Identifier: AGPL-3.0-or-later
// Modified for PaperDesk 2026-10-12: isolate runtime files in app-owned tmp path.
// Krita viewport: shows frames pushed from the desktop full-screen on the Paper Pro.
//
// A Qt library preloaded into xochitl (LD_PRELOAD, next to the direct-paste XOVI
// loader; deliberately NOT an extensions.d entry). It only ever reads /tmp/paperdesk-viewport (tmpfs, created by the desktop
// bridge) and adds one QML overlay item to xochitl's window; it never touches
// documents, the scene, or xochitl's own items.
//
//   active        touched by the bridge every few seconds; the overlay is shown
//                 only while this heartbeat is younger than 5 s, so the normal
//                 tablet UI returns by itself if the desktop goes away
//   current       name of the frame to display (f<seq>.jpg|png in the same dir)
//   shot.request  ask for one window capture -> shot.png (verification)
//   refresh.request  clear e-ink ghosting: ask the panel driver for its own full blink
//                 (EPFramebuffer::ghostControl) and flash the overlay black then white
//   pen           FIFO of raw marker records from the pen helper; strokes are inked here
//                 at once, in the panel's fast Pen screen mode (the one xochitl's own ink uses),
//                 and wiped when the next real frame from Krita arrives. A new frame is held
//                 back until the pen has been up for PenRestMs, so the slow full-colour update
//                 never lands in the middle of handwriting
//   status.json   written by the extension about once a second
#include <QCoreApplication>
#include <QDateTime>
#include <cmath>
#include <QFile>
#include <QFileInfo>
#include <QGuiApplication>
#include <QColor>
#include <QImage>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMetaEnum>
#include <QMetaMethod>
#include <QMetaProperty>
#include <QPointer>
#include <QQmlComponent>
#include <QQmlContext>
#include <QQmlEngine>
#include <QPainter>
#include <QPen>
#include <QQuickItem>
#include <QQuickPaintedItem>
#include <QSocketNotifier>
#include <QQuickWindow>
#include <QRegularExpression>
#include <QSaveFile>
#include <QSet>
#include <QTimer>
#include <QUrl>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <typeinfo>
#include <thread>
#include <dlfcn.h>
#include <fcntl.h>
#include <linux/input.h>
#include <sys/stat.h>
#include <unistd.h>

namespace {
const QString Root = QStringLiteral("/tmp/paperdesk-viewport");
constexpr qint64 HeartbeatMs = 5000, PenRestMs = 600;
const char *OverlayQml = R"(import QtQuick
Rectangle {
    color: "white"; z: 1000000; visible: false
    Image { objectName: "frame"; anchors.fill: parent; cache: false; asynchronous: false; smooth: false; fillMode: Image.PreserveAspectFit }
    MouseArea { anchors.fill: parent; preventStealing: true }
    Rectangle { objectName: "flash"; anchors.fill: parent; z: 2; visible: false; color: "black" }
})";
constexpr double SensorX = 11180.0, SensorY = 15340.0, SensorPressure = 4096.0;

// Provisional ink: what the pen just did, drawn locally so the panel answers immediately.
class Ink : public QQuickPaintedItem {
public:
    explicit Ink(QQuickItem *parent) : QQuickPaintedItem(parent) { setZ(1); }
    void paint(QPainter *painter) override {
        if (image.isNull()) return;
        const QRectF dirty = painter->hasClipping() ? painter->clipBoundingRect() : QRectF(image.rect());
        painter->drawImage(dirty, image, dirty);
    }
    void line(const QPointF &from, const QPointF &to, double width, bool erase) {
        const QSize wanted = size().toSize();
        if (wanted.isEmpty()) return;
        if (image.size() != wanted) { image = QImage(wanted, QImage::Format_ARGB32_Premultiplied); image.fill(Qt::transparent); }
        QPainter painter(&image);
        painter.setPen(QPen(erase ? Qt::white : Qt::black, width, Qt::SolidLine, Qt::RoundCap));
        painter.drawLine(from, to);
        const double pad = width + 2;
        update(QRectF(from, to).normalized().adjusted(-pad, -pad, pad, pad).toAlignedRect());
        used = true;
    }
    void clear() { if (used) { image.fill(Qt::transparent); used = false; update(); } }
private:
    QImage image;
    bool used = false;
};

QQmlEngine *findEngine(QQuickItem *item, int depth = 0) {
    if (!item || depth > 16) return nullptr;
    if (QQmlEngine *engine = qmlEngine(item)) return engine;
    for (QQuickItem *child : item->childItems())
        if (QQmlEngine *engine = findEngine(child, depth + 1)) return engine;
    return nullptr;
}

class Viewport : public QObject {
public:
    Viewport() {
        auto *timer = new QTimer(this);
        QObject::connect(timer, &QTimer::timeout, this, [this] { tick(); });
        timer->start(100);
    }

private:
    QQuickWindow *window() const {
        QQuickWindow *found = nullptr;
        for (QWindow *candidate : QGuiApplication::allWindows()) {
            auto *quick = qobject_cast<QQuickWindow *>(candidate);
            if (!quick || !quick->isVisible()) continue;
            if (found) return nullptr;           // ambiguous: do nothing
            found = quick;
        }
        return found;
    }

    bool ensureOverlay(QQuickWindow *target) {
        if (overlay && overlay->window() == target) return true;
        if (overlay) { delete overlay; overlay = nullptr; }
        QQmlEngine *engine = findEngine(target->contentItem());
        if (!engine) { error = "no QML engine"; return false; }
        QQmlComponent component(engine);
        component.setData(OverlayQml, QUrl(QStringLiteral("file:///tmp/paperdesk-viewport/overlay.qml")));
        QObject *object = component.create(engine->rootContext());
        auto *item = qobject_cast<QQuickItem *>(object);
        if (!item) { error = "overlay: " + component.errorString(); delete object; return false; }
        QQmlEngine::setObjectOwnership(item, QQmlEngine::CppOwnership);
        item->setParent(this);
        item->setParentItem(target->contentItem());
        overlay = item;
        ink = new Ink(item);
        makeModeItem(engine, item);
        shown.clear();
        return true;
    }

    // The epaper scene graph picks the panel waveform per region from "screen mode" items.
    // xochitl's QML type is tried first; the plugin's exported C++ class is the fallback.
    void makeModeItem(QQmlEngine *engine, QQuickItem *parent) {
        modeNote.clear(); modeNow.clear();
        for (const char *module : {"xofm.libs.epaper", "com.remarkable.epaper", "xofm.epaper"}) {
            QQmlComponent component(engine);
            component.setData(QByteArray("import QtQuick\nimport ") + module + "\nScreenModeItem { z: 3 }", QUrl(QStringLiteral("file:///tmp/paperdesk-viewport/mode.qml")));
            QObject *object = component.isReady() ? component.create(engine->rootContext()) : nullptr;
            if (auto *item = qobject_cast<QQuickItem *>(object)) {
                QQmlEngine::setObjectOwnership(item, QQmlEngine::CppOwnership);
                item->setParent(parent); item->setParentItem(parent); modeItem = item; modeNote = QString("qml ") + module;
                break;
            }
            delete object;
        }
        if (!modeItem) {
            using Construct = void (*)(void *, QQuickItem *);
            if (auto construct = reinterpret_cast<Construct>(driverSymbol("_ZN16EPScreenModeItemC1EP10QQuickItem"))) {
                void *memory = ::operator new(1024);            // the real size is unknown; this is generous
                std::memset(memory, 0, 1024);
                construct(memory, parent);
                modeItem = static_cast<QQuickItem *>(memory); modeItem->setZ(3); modeNote = "c++ EPScreenModeItem";
            } else modeNote = "no screen-mode item available";
        }
        if (modeItem) modeNote += QString(" (") + modeItem->metaObject()->className() + ")";
        setMode("Content");
    }

    // The display driver lives in the scene-graph plugin, which Qt loads without exporting
    // its symbols globally, so they are looked up through the already-loaded library.
    void *driverSymbol(const char *name) {
        static void *library = ::dlopen("/usr/lib/plugins/scenegraph/libqsgepaper.so", RTLD_NOW | RTLD_NOLOAD);
        return library ? ::dlsym(library, name) : nullptr;
    }

    // xochitl links the panel driver statically and exports nothing. Its ghost clearing is reached
    // through live QObjects instead: the driver itself (EPFramebuffer: clearGhosting()), handed to
    // document views as a "framebuffer" property, or the app's GhostBuster service
    // (forceClearNow()), handed to windows as a "ghostBuster" property. Found by walking the tree.
    static bool isDriver(QObject *object) { return object && object->metaObject()->indexOfEnumerator("GhostControlMode") >= 0; }
    static bool isBuster(QObject *object) {
        if (!object) return false;
        for (const QMetaObject *meta = object->metaObject(); meta; meta = meta->superClass())
            if (QByteArray(meta->className()).contains("GhostBuster")) return true;
        return false;
    }

    void findCleaners(QQuickWindow *target, QQmlEngine *engine) {
        if (driver && buster) return;
        QList<QObject *> queue{QCoreApplication::instance(), target, target->contentItem()};
        QSet<QObject *> seen;
        auto consider = [this](QObject *held) {
            if (!driver && isDriver(held)) driver = held;
            if (!buster && isBuster(held)) buster = held;
        };
        if (engine) for (const char *name : {"ghostBuster", "framebuffer"}) consider(qvariant_cast<QObject *>(engine->rootContext()->contextProperty(name)));
        for (int i = 0; i < queue.size() && i < 400000 && !(driver && buster); ++i) {
            QObject *object = queue.at(i);
            if (!object || seen.contains(object)) continue;
            seen.insert(object);
            consider(object);
            const QMetaObject *meta = object->metaObject();
            for (const char *name : {"ghostBuster", "framebuffer"}) {
                const int property = meta->indexOfProperty(name);
                if (property >= 0) consider(qvariant_cast<QObject *>(meta->property(property).read(object)));
            }
            if (QQmlContext *context = qmlContext(object))
                for (const char *name : {"ghostBuster", "framebuffer"}) consider(qvariant_cast<QObject *>(context->contextProperty(name)));
            queue += object->children();
            if (auto *item = qobject_cast<QQuickItem *>(object)) for (QQuickItem *child : item->childItems()) queue += child;
        }
        scanned = seen.size();
    }

    // Calls the first method of that name taking nothing, one string, or one enum (given by key).
    static bool call(QObject *object, const char *name, const char *enumName, const char *enumKey, QString *signatures) {
        const QMetaObject *meta = object->metaObject();
        bool sent = false;
        for (int i = 0; i < meta->methodCount(); ++i) {
            const QMetaMethod method = meta->method(i);
            if (method.name() != name) continue;
            *signatures += QString::fromLatin1(method.methodSignature()) + " ";
            if (sent) continue;
            if (method.parameterCount() == 0) sent = method.invoke(object, Qt::DirectConnection);
            else if (method.parameterCount() == 1 && method.parameterMetaType(0) == QMetaType::fromType<QString>())
                sent = method.invoke(object, Qt::DirectConnection, Q_ARG(QString, QStringLiteral("rm-viewport")));
            else if (method.parameterCount() == 1 && enumName && meta->indexOfEnumerator(enumName) >= 0) {
                int value = meta->enumerator(meta->indexOfEnumerator(enumName)).keyToValue(enumKey);
                sent = method.invoke(object, Qt::DirectConnection, QGenericArgument(method.parameterTypeName(0).constData(), &value));
            }
        }
        return sent;
    }

    void clearGhosts(QQuickWindow *target) {
        findCleaners(target, findEngine(target->contentItem()));
        QString signatures; bool sent = false;
        if (driver) sent = call(driver, "clearGhosting", "GhostControlMode", "BlinkNow", &signatures);
        if (!sent && buster) sent = call(buster, "forceClearNow", nullptr, nullptr, &signatures);
        if (sent) ++blinks;
        ghostNote = QString("driver %1 · ghostbuster %2 · %3 objects · %4· %5")
            .arg(driver ? driver->metaObject()->className() : "none", buster ? buster->metaObject()->className() : "none")
            .arg(scanned).arg(signatures, sent ? "panel blink requested" : "no panel blink available");
    }

    void setMode(const char *key) {
        if (!modeItem || modeNow == QLatin1String(key)) return;
        const QMetaObject *meta = modeItem->metaObject();
        for (const char *name : {"mode", "screenMode"}) {
            const int index = meta->indexOfProperty(name);
            if (index < 0) continue;
            const QMetaProperty property = meta->property(index);
            bool ok = false;
            const int value = property.isEnumType() ? property.enumerator().keyToValue(key, &ok) : -1;
            if (ok && property.write(modeItem, value)) { modeNow = QLatin1String(key); return; }
        }
        if (!modeNote.contains("cannot set")) modeNote += QString(" · cannot set ") + key;
    }

    void tick() {
        const qint64 now = QDateTime::currentMSecsSinceEpoch();
        const QFileInfo heartbeat(Root + "/active");
        const bool rootOk = QFileInfo(Root).isDir();
        const qint64 age = heartbeat.exists() ? now - heartbeat.lastModified().toMSecsSinceEpoch() : -1;
        const bool want = rootOk && age >= -60000 && age < HeartbeatMs && heartbeat.exists();
        QQuickWindow *target = window();
        if (!want || !target) {
            if (overlay && overlay->isVisible()) overlay->setVisible(false);
        } else if (ensureOverlay(target)) {
            overlay->setSize(target->contentItem()->size());
            if (ink) ink->setSize(overlay->size());
            if (modeItem) modeItem->setSize(overlay->size());
            openPen();
            readZones();
            QFile pointer(Root + "/current");
            QString name;
            if (pointer.open(QIODevice::ReadOnly) && pointer.size() < 64) name = QString::fromLatin1(pointer.readAll()).trimmed();
            static const QRegularExpression valid("^f[0-9]{1,12}\\.(jpg|png)$");
            const bool penBusy = touching || now - penUpAt < PenRestMs;
            if (name != shown && !penBusy && valid.match(name).hasMatch() && QFileInfo(Root + "/" + name).isFile()) {
                setMode("Content");
                if (QQuickItem *frame = overlay->findChild<QQuickItem *>("frame")) {
                    frame->setProperty("source", QUrl::fromLocalFile(Root + "/" + name));
                    if (!shown.isEmpty()) QFile::remove(Root + "/" + shown);
                    shown = name;
                    if (ink) ink->clear();
                    ++frames;
                    error.clear();
                }
            }
            // Pen mode is for the moving nib only; the picture goes back to full quality once it rests.
            if (modeNow == QLatin1String("Pen") && !touching && now - penUpAt > 1500) setMode("Content");
            if (!shown.isEmpty() && !overlay->isVisible()) overlay->setVisible(true);
        }
        // A blink asked for under a moving nib waits for the nib to lift instead of being lost.
        if (rootOk && !touching && QFile::exists(Root + "/refresh.request")) {
            QFile::remove(Root + "/refresh.request");
            QQuickItem *flash = overlay && overlay->isVisible() ? overlay->findChild<QQuickItem *>("flash") : nullptr;
            if (flash && !flash->isVisible()) {
                ++refreshes;
                setMode("Content");
                clearGhosts(target);
                flash->setProperty("color", QColor(Qt::black));
                flash->setVisible(true);
                const QPointer<QQuickItem> guard(flash);
                QTimer::singleShot(350, this, [guard] { if (guard) guard->setProperty("color", QColor(Qt::white)); });
                QTimer::singleShot(700, this, [guard] { if (guard) guard->setVisible(false); });
            }
        }
        if (rootOk && target && shots < 200 && QFile::exists(Root + "/shot.request")) {
            QFile::remove(Root + "/shot.request");
            ++shots;
            const QImage image = target->grabWindow();
            if (image.isNull() || !image.save(Root + "/shot.tmp.png", "PNG") || !QFile::rename(Root + "/shot.tmp.png", Root + "/shot.png")) {
                QFile::remove(Root + "/shot.png");
                if (!QFile::rename(Root + "/shot.tmp.png", Root + "/shot.png")) error = "capture failed";
            }
        }
        if (rootOk && now - lastStatus >= 1000) {
            lastStatus = now;
            const QJsonObject status{{"loaded", true}, {"heartbeat_ms", now}, {"visible", overlay && overlay->isVisible()},
                                     {"frames", frames}, {"current", shown}, {"error", error},
                                     {"pen_open", penFd >= 0}, {"zones", int(zones.size())}, {"ink_segments", segments}, {"refreshes", refreshes},
                                     {"mode_item", modeNote}, {"mode", modeNow}, {"ghost", ghostNote}, {"blinks", blinks},
                                     {"window_width", target ? target->width() : 0}, {"window_height", target ? target->height() : 0},
                                     {"content_width", target ? target->contentItem()->width() : 0.0},
                                     {"content_height", target ? target->contentItem()->height() : 0.0}};
            QSaveFile file(Root + "/status.json");
            if (file.open(QIODevice::WriteOnly)) { file.write(QJsonDocument(status).toJson(QJsonDocument::Compact)); file.commit(); }
        }
    }

    void openPen() {
        if (penFd >= 0) return;
        const QByteArray path = (Root + "/pen").toLocal8Bit();
        ::mkfifo(path.constData(), 0600);
        struct stat info;
        if (::lstat(path.constData(), &info) != 0 || !S_ISFIFO(info.st_mode)) return;
        penFd = ::open(path.constData(), O_RDWR | O_NONBLOCK | O_CLOEXEC);   // O_RDWR: never sees EOF
        if (penFd < 0) return;
        auto *notifier = new QSocketNotifier(penFd, QSocketNotifier::Read, this);
        QObject::connect(notifier, &QSocketNotifier::activated, this, [this] { readPen(); });
    }

    // Palette circles ("x y radius" per line, window pixels): a stroke starting inside one is a
    // tap on the palette, so it leaves no ink and does not hold the next picture back.
    void readZones() {
        const QFileInfo info(Root + "/zones");
        const QDateTime stamp = info.exists() ? info.lastModified() : QDateTime();
        if (stamp == zonesStamp) return;
        zonesStamp = stamp; zones.clear();
        QFile file(info.filePath());
        if (!info.exists() || info.size() > 4096 || !file.open(QIODevice::ReadOnly)) return;
        for (const QByteArray &line : file.readAll().split('\n')) {
            const QList<QByteArray> parts = line.simplified().split(' ');
            if (parts.size() == 3) zones.append({parts[0].toDouble(), parts[1].toDouble(), parts[2].toDouble()});
        }
    }

    void readPen() {
        struct input_event events[128];
        for (;;) {
            const ssize_t count = ::read(penFd, events, sizeof events);
            if (count < static_cast<ssize_t>(sizeof events[0])) return;
            for (size_t i = 0; i < count / sizeof events[0]; ++i) {
                const input_event &event = events[i];
                if (event.type == EV_ABS) {
                    if (event.code == ABS_X) penX = event.value;
                    else if (event.code == ABS_Y) penY = event.value;
                    else if (event.code == ABS_PRESSURE) pressure = event.value;
                } else if (event.type == EV_KEY) {
                    if (event.code == BTN_TOUCH) {
                        touching = event.value; muted = false;
                        if (touching && overlay) {
                            const double x = penX / SensorX * overlay->width(), y = penY / SensorY * overlay->height();
                            for (const Zone &zone : zones) if (std::hypot(x - zone.x, y - zone.y) <= zone.r) muted = true;
                        }
                        if (muted) touching = false;
                    }
                    else if (event.code == BTN_TOOL_RUBBER) rubber = event.value;
                } else if (event.type == EV_SYN && event.code == SYN_REPORT) {
                    const bool inking = touching && pressure > 0 && ink && overlay && overlay->isVisible();
                    if (wasTouching && !touching) penUpAt = QDateTime::currentMSecsSinceEpoch();
                    wasTouching = touching;
                    if (inking) {
                        setMode("Pen");
                        // The sensor and the window share the portrait frame, whichever way the picture is turned.
                        const QPointF now(penX / SensorX * overlay->width(), penY / SensorY * overlay->height());
                        if (hasLast) { ink->line(last, now, rubber ? 14.0 : 2.0 + 2.5 * pressure / SensorPressure, rubber); ++segments; }
                        last = now;
                    }
                    hasLast = inking;
                }
            }
        }
    }

    QPointer<QQuickItem> overlay;
    QPointer<Ink> ink;
    QPointer<QQuickItem> modeItem;
    QString modeNote, modeNow, ghostNote;
    QPointer<QObject> driver, buster;
    struct Zone { double x, y, r; };
    QList<Zone> zones;
    QDateTime zonesStamp;
    bool muted = false;
    int blinks = 0, scanned = 0;
    qint64 penUpAt = 0;
    bool wasTouching = false;
    int penFd = -1, penX = 0, penY = 0, pressure = 0, segments = 0, refreshes = 0;
    bool touching = false, rubber = false, hasLast = false;
    QPointF last;
    QString shown, error;
    int frames = 0, shots = 0;
    qint64 lastStatus = 0;
};
} // namespace

__attribute__((constructor)) static void start() {
    std::thread([] {
        for (int attempt = 0; attempt < 100; ++attempt) {
            if (auto *app = QCoreApplication::instance()) {
                QMetaObject::invokeMethod(app, [] { new Viewport(); }, Qt::QueuedConnection);
                return;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        }
        std::fprintf(stderr, "[krita-viewport] no Qt application\n");
    }).detach();
}
