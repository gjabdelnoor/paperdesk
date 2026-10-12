// Direct paste: native screenshot insertion into the open reMarkable page.
//
// A Qt extension loaded into xochitl by the XOVI LD_PRELOAD loader (see
// README.md). Every 1.5 s it publishes a compact status.json describing the
// single visible notebook page. The laptop-side host (host.py) stages an
// image plus a request manifest under incoming/<uuid>/, and the extension
// inserts the image with xochitl's own insertImageFileAsSceneItem, then
// confirms persistence by observing the page's new asset file inside the
// saved scene.
//
// Hard guards, by design:
//   * firmware 3.28.0.172 and the exact xochitl binary sha256 are verified
//     on every tick; anything else is reported not-ready and never inserts;
//   * exactly one visible SceneView (a single open page) is ever targeted;
//   * the page's portrait paper must be one of the known sizes below;
//   * a request must name the protocol, the extension instance, the active
//     document and page, the paper size, the image sha256 and dimensions,
//     and a live expiry window — all re-verified against a second live page
//     read immediately before the single insertion attempt;
//   * a receipt.json is written before the attempt, so a crash can never
//     replay the insertion;
//   * staged images are capped at 92% of the paper width and 90% of its
//     height, and placed at the page centre.
//
// Build modes: default = production (insertion enabled). Build with
// -DDIRECT_PASTE_PROBE_ONLY for a read-only diagnostics build that reports
// insert_compiled = false and never inserts.
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QGuiApplication>
#include <QImage>
#ifndef DIRECT_PASTE_PROBE_ONLY
#include <QImageReader>
#include <QUrl>
#include <QPointF>
#endif
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMetaMethod>
#include <QQmlContext>
#include <QQmlEngine>
#include <QQmlComponent>
#include <QPointer>
#include <QQuickItem>
#include <QQuickWindow>
#include <QRegularExpression>
#include <QSaveFile>
#include <QSet>
#include <QTimer>
#include <QThread>
#include <QUuid>
#include <QVariant>
#include <QtGlobal>
#include <algorithm>
#include <cstdio>
#include <thread>
#include <chrono>
#include <unistd.h>
#include "native_pages.h"

namespace {
constexpr auto Root = "/home/root/.cache/remarkable-direct-paste";
constexpr auto RequiredFirmware = "3.28.0.172";
constexpr auto RequiredXochitlSha = "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021";
// Must match PROTOCOL in host.py; the manifest and status handshake on it.
constexpr int Protocol = 2;
// Notebook paper sizes in portrait scene units: 1404x1872 is reMarkable 2
// paper (imported and rmdoc-built documents), 1620x2160 is the Paper Pro's
// native notebook paper. Any other geometry is refused.
struct PaperSize { double width; double height; };
constexpr PaperSize KnownPapers[] = {{1404.0, 1872.0}, {1620.0, 2160.0}};
// Screenshots rotate through capture-1.png .. capture-<CaptureSlots>.png.
constexpr int CaptureSlots = 4;
// A staged image may cover at most this fraction of the active page's paper.
constexpr double FitWidth = 0.92;
constexpr double FitHeight = 0.90;
// Matches MIN_IMAGE_DIMENSION in host.py: a practically invisible object is
// refused rather than inserted.
constexpr int MinImageDimension = 8;
const QRegularExpression UuidRe("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$");

const PaperSize *paperFor(const QRectF &portrait) {
    for (const PaperSize &paper : KnownPapers)
        if (portrait.x() == -paper.width / 2.0 && portrait.y() == 0.0
            && portrait.width() == paper.width && portrait.height() == paper.height)
            return &paper;
    return nullptr;
}

QByteArray readLimited(const QString &path, qint64 maxBytes) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly) || file.size() > maxBytes) return {};
    return file.readAll();
}

bool saveJson(const QString &path, const QJsonObject &value) {
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly)) return false;
    if (file.write(QJsonDocument(value).toJson(QJsonDocument::Compact)) < 0) return false;
    return file.commit();
}

QString uuidValue(QObject *object, const char *name) {
    if (!object) return {};
    QVariant value = object->property(name);
    if (!value.isValid() || value.isNull()) return {};
    QString text = value.toString().trimmed().toLower();
    if (text.startsWith('{') && text.endsWith('}')) text = text.mid(1, text.size() - 2);
    return UuidRe.match(text).hasMatch() ? text : QString{};
}

QString firstUuid(QObject *first, QObject *second, const char *const *names, int count) {
    for (int i = 0; i < count; ++i) {
        QString value = uuidValue(first, names[i]);
        if (value.isEmpty()) value = uuidValue(second, names[i]);
        if (!value.isEmpty()) return value;
    }
    return {};
}

struct ActivePage { QObject *controller = nullptr; QObject *view = nullptr; QObject *wrapper = nullptr; QString document; QString page; int candidates = 0; bool guiThread = false; QSet<QObject *> seen; };

void inspectItem(QQuickItem *item, ActivePage &found) {
    if (!item) return;
    const QString className = item->metaObject()->className();
    if (item->isVisible() && item->inherits("SceneView")) {
        QVariant property = item->property("controller");
        QObject *controller = property.value<QObject *>();
        if (controller && QString(controller->metaObject()->className()) == "SceneController" && !found.seen.contains(controller)) {
            found.seen.insert(controller);
            ++found.candidates;
            found.controller = controller;
            found.view = item;
            found.wrapper = item->property("document").value<QObject *>();
        }
    }
    for (QQuickItem *child : item->childItems()) inspectItem(child, found);
}

ActivePage activePage() {
    ActivePage found;
    for (QWindow *window : QGuiApplication::allWindows()) {
        auto *quick = qobject_cast<QQuickWindow *>(window);
        if (quick && quick->isVisible()) inspectItem(quick->contentItem(), found);
    }
    if (found.candidates != 1) { found.controller = nullptr; return found; }
    static const char *documentNames[] = {"docUUID", "documentUuid", "documentUUID", "documentId", "documentID", "id"};
    found.document = firstUuid(found.wrapper, found.view, documentNames, 6);
    found.page = uuidValue(found.view, "pageId");
    found.guiThread = found.controller->thread() == QThread::currentThread();
    return found;
}

QQuickItem *namedItem(QQuickItem *root, const QString &name) {
    if (!root) return nullptr;
    if (root->objectName() == name) return root;
    for (QQuickItem *child : root->childItems())
        if (QQuickItem *found = namedItem(child, name)) return found;
    return nullptr;
}

QJsonArray propertyNames(QObject *object) {
    QJsonArray names;
    if (!object) return names;
    const QMetaObject *meta = object->metaObject();
    for (int i = 0; i < meta->propertyCount(); ++i)
        names.append(QString::fromLatin1(meta->property(i).name()));
    for (const QByteArray &name : object->dynamicPropertyNames()) names.append(QString::fromLatin1(name));
    return names;
}

QJsonArray methodNames(QObject *object) {
    QJsonArray names;
    if (!object) return names;
    const QMetaObject *meta = object->metaObject();
    for (int i = 0; i < meta->methodCount(); ++i)
        names.append(QString::fromLatin1(meta->method(i).methodSignature()));
    return names;
}

QJsonObject objectDetails(QObject *object) {
    if (!object) return {};
    QJsonObject values;
    const QMetaObject *meta = object->metaObject();
    for (int i = 0; i < meta->propertyCount(); ++i) {
        const char *name = meta->property(i).name();
        const QVariant value = object->property(name);
        if (value.metaType() == QMetaType::fromType<QString>()
            || value.metaType() == QMetaType::fromType<int>()
            || value.metaType() == QMetaType::fromType<bool>())
            values[name] = QJsonValue::fromVariant(value);
        else if (QObject *target = value.value<QObject *>())
            values[name] = QString::fromLatin1(target->metaObject()->className());
    }
    return {{"class", meta->className()}, {"name", object->objectName()},
            {"properties", propertyNames(object)}, {"values", values},
            {"methods", methodNames(object)}};
}

QJsonArray nativeImageMethods(QObject *object) {
    QJsonArray details;
    if (!object) return details;
    const QMetaObject *meta = object->metaObject();
    for (int i = 0; i < meta->methodCount(); ++i) {
        const QMetaMethod method = meta->method(i);
        const QString signature = QString::fromLatin1(method.methodSignature());
        if (signature.contains("insertImage") || signature.contains("selectImage") || signature.contains("pasteClipboard"))
            details.append(QJsonObject{{"signature", signature},
                                       {"return_type", QString::fromLatin1(method.typeName())},
                                       {"method_type", int(method.methodType())}});
    }
    return details;
}

QJsonObject rectValue(QObject *object, const char *name) {
    if (!object) return {};
    const QVariant value = object->property(name);
    if (!value.isValid() || !value.canConvert<QRectF>()) return {};
    const QRectF rect = value.toRectF();
    if (!rect.isValid()) return {};
    return {{"x", rect.x()}, {"y", rect.y()},
            {"width", rect.width()}, {"height", rect.height()}};
}

class Extension : public QObject {
public:
    Extension() {
        instance = QUuid::createUuid().toString(QUuid::WithoutBraces);
        QDir().mkpath(QString::fromLatin1(Root));
#ifndef DIRECT_PASTE_PROBE_ONLY
        QDir().mkpath(QString::fromLatin1(Root) + "/incoming");
#endif
        const QByteArray release = readLimited("/etc/os-release", 65536);
        firmwareOk = release.contains(QByteArray("IMG_VERSION=\"") + RequiredFirmware + "\"");
        QFile binary("/usr/bin/xochitl");
        if (binary.open(QIODevice::ReadOnly))
            binaryOk = QString::fromLatin1(QCryptographicHash::hash(binary.readAll(), QCryptographicHash::Sha256).toHex()) == RequiredXochitlSha;
        auto *timer = new QTimer(this);
        connect(timer, &QTimer::timeout, this, [this]() { tick(); });
        timer->start(1500);
        tick();
    }

    // A timer slot must never throw into Qt's event loop; on any unexpected
    // error keep the heartbeat alive and publish what went wrong instead.
    void tick() {
        try {
            tickInner();
            lastError.clear();
        } catch (const std::exception &error) {
            lastError = QString::fromUtf8(error.what());
            publishError();
        } catch (...) {
            lastError = QStringLiteral("unknown exception in extension tick");
            publishError();
        }
    }

private:
    void tickInner() {
        // Qt's own window capture is requested explicitly, at most one per
        // tick, into a ring of CaptureSlots files so repeated screenshots
        // never grow the cache. It is never triggered by a signal and never
        // edits a document.
        const QString captureRequest = QString::fromLatin1(Root) + "/capture.request";
        if (QFileInfo::exists(captureRequest)) {
            ++captureCount;
            QFile::remove(captureRequest);
            int visible = 0;
            QQuickWindow *target = nullptr;
            for (QWindow *window : QGuiApplication::allWindows()) {
                auto *quick = qobject_cast<QQuickWindow *>(window);
                if (quick && quick->isVisible()) { ++visible; target = quick; }
            }
            if (visible == 1 && target) {
                QImage image = target->grabWindow();
                if (!image.isNull() && image.width() > 0 && image.height() > 0
                    && qint64(image.width()) * image.height() <= 10000000
                    && image.save(QString::fromLatin1(Root) + "/capture-" + QString::number((captureCount - 1) % CaptureSlots + 1) + ".png", "PNG")) {
                    captureState = "saved";
                    captureWidth = image.width();
                    captureHeight = image.height();
                } else captureState = "grab-failed";
            } else captureState = "visible-window-count-not-one";
        }
        // The full introspection dump is written only on explicit request,
        // keeping the per-tick status small (flash-wear friendly).
        const QString probeRequest = QString::fromLatin1(Root) + "/probe.request";
        if (QFileInfo::exists(probeRequest)) {
            QFile::remove(probeRequest);
            saveJson(QString::fromLatin1(Root) + "/probe.json", probeDetails(activePage()));
        }
        const ActivePage active = activePage();
#ifndef DIRECT_PASTE_PROBE_ONLY
        ensureNotesUi(active);
        ensureNotesActions();
        verifyPageReceipt();
        insertNotesPage(active);
#endif
        const QJsonArray nativeMethods = nativeImageMethods(active.controller);
        const bool insertMethodFound = std::any_of(nativeMethods.begin(), nativeMethods.end(), [](const QJsonValue &value) {
            return value.toObject().value("signature").toString().startsWith("insertImageFileAsSceneItem(");
        });
        const bool probePassed = firmwareOk && binaryOk && insertMethodFound && active.guiThread
            && !active.document.isEmpty() && !active.page.isEmpty();
        const PaperSize *paper = nullptr;
#ifdef DIRECT_PASTE_PROBE_ONLY
        constexpr bool insertCompiled = false;
        const bool ready = false;
#else
        constexpr bool insertCompiled = true;
        paper = paperFor(active.controller ? active.controller->property("paperPortraitBounds").toRectF() : QRectF{});
        const bool ready = probePassed && paper != nullptr;
#endif
        QJsonObject status{{"protocol", Protocol}, {"firmware", RequiredFirmware}, {"xochitl_sha256", RequiredXochitlSha},
                           {"firmware_verified", firmwareOk}, {"binary_verified", binaryOk},
                           {"ready", ready}, {"probe_passed", probePassed}, {"insert_compiled", insertCompiled},
                           {"method_found", insertMethodFound}, {"gui_thread", active.guiThread},
                           {"native_methods", nativeMethods}, {"candidate_count", active.candidates},
                           {"instance_id", instance}, {"pid", qint64(getpid())},
                           {"heartbeat_ms", QDateTime::currentMSecsSinceEpoch()},
                           {"error", lastError},
                           {"native_pages_ready", notesUi && notesUi->property("ready").toBool()},
                           {"native_pages_error", notesUiError},
                           {"capture_state", captureState}, {"capture_count", captureCount},
                           {"capture_slots", CaptureSlots},
                           {"capture_width", captureWidth}, {"capture_height", captureHeight},
                           {"document_uuid", active.document}, {"page_uuid", active.page},
                           {"paper_width", paper ? paper->width : 0.0},
                           {"paper_height", paper ? paper->height : 0.0},
                           {"view_class", active.view ? active.view->metaObject()->className() : ""},
                           {"wrapper_class", active.wrapper ? active.wrapper->metaObject()->className() : ""},
                           {"paper_portrait_bounds", rectValue(active.controller, "paperPortraitBounds")},
                           {"paper_note_bounds", rectValue(active.controller, "paperNoteBounds")}};
        saveJson(QString::fromLatin1(Root) + "/status.json", status);
#ifndef DIRECT_PASTE_PROBE_ONLY
        verifyReceipts();
        if (ready) insertOne(active);
#endif
    }

    QJsonObject probeDetails(const ActivePage &active) const {
        QJsonObject sources;
        for (const QString &path : {QString(":/qt/qml/xofm/libs/toolbar/qml/Toolbar.qml"),
             QString(":/qt/qml/xofm/libs/toolbar/qml/ToolbarTool.qml"),
             QString(":/qml/device/view/documentview/PagesActions.qml")})
            sources[path] = QString::fromUtf8(readLimited(path, 200000));
        QJsonArray ancestors;
        for (auto *item = qobject_cast<QQuickItem *>(active.view); item; item = item->parentItem())
            ancestors.append(objectDetails(item));
        return {{"sources", sources}, {"ancestors", ancestors}, {"notes_ui", objectDetails(notesUi)},
                {"cornell_ui", objectDetails(cornellItem)},
                {"protocol", Protocol}, {"instance_id", instance}, {"pid", qint64(getpid())},
                {"heartbeat_ms", QDateTime::currentMSecsSinceEpoch()},
                {"document_uuid", active.document}, {"page_uuid", active.page},
                {"view_class", active.view ? active.view->metaObject()->className() : ""},
                {"view_properties", propertyNames(active.view)}, {"view_methods", methodNames(active.view)},
                {"wrapper_class", active.wrapper ? active.wrapper->metaObject()->className() : ""},
                {"wrapper_properties", propertyNames(active.wrapper)}, {"wrapper_methods", methodNames(active.wrapper)},
                {"controller_properties", propertyNames(active.controller)},
                {"controller_methods", methodNames(active.controller)},
                {"paper_portrait_bounds", rectValue(active.controller, "paperPortraitBounds")},
                {"paper_note_bounds", rectValue(active.controller, "paperNoteBounds")}};
    }

    void publishError() {
        saveJson(QString::fromLatin1(Root) + "/status.json",
                 QJsonObject{{"protocol", Protocol}, {"firmware", RequiredFirmware},
                             {"xochitl_sha256", RequiredXochitlSha},
                             {"firmware_verified", firmwareOk}, {"binary_verified", binaryOk},
                             {"instance_id", instance}, {"pid", qint64(getpid())},
                             {"heartbeat_ms", QDateTime::currentMSecsSinceEpoch()},
                             {"error", lastError}});
    }

#ifndef DIRECT_PASTE_PROBE_ONLY
    void ensureNotesUi(const ActivePage &active) {
        QQuickItem *documentView = nullptr;
        for (auto *item = qobject_cast<QQuickItem *>(active.view); item; item = item->parentItem()) {
            if (QString::fromLatin1(item->metaObject()->className()).startsWith("DocumentView_")) {
                documentView = item;
                break;
            }
        }
        if (!documentView || !firmwareOk || !binaryOk) return;
        if (notesUi && notesUi->parentItem() == documentView) return;
        if (cornellItem) { cornellItem->deleteLater(); cornellItem = nullptr; }
        if (gridItem) { gridItem->deleteLater(); gridItem = nullptr; }
        if (linedItem) { linedItem->deleteLater(); linedItem = nullptr; }
        if (notesUi) { notesUi->deleteLater(); notesUi = nullptr; }
        QQmlEngine *engine = qmlEngine(documentView);
        QQmlContext *context = qmlContext(documentView);
        if (!engine || !context) return;
        QQmlComponent component(engine);
        component.setData(NativePagesQml, QUrl("qrc:/direct-paste/NativePages.qml"));
        QObject *created = component.beginCreate(context);
        auto *item = qobject_cast<QQuickItem *>(created);
        if (!item) {
            notesUiError = component.errorString();
            delete created;
            return;
        }
        item->setParent(documentView);
        item->setParentItem(documentView);
        item->setProperty("documentView", QVariant::fromValue<QObject *>(documentView));
        QQuickItem *stockAdd = namedItem(documentView, "addPageButton");
        if (stockAdd) item->setProperty("nativeToolbar", stockAdd->property("toolbar"));
        for (auto *ancestor = qobject_cast<QQuickItem *>(active.view); ancestor; ancestor = ancestor->parentItem())
            if (QString::fromLatin1(ancestor->metaObject()->className()).startsWith("DeviceSceneView_"))
                item->setProperty("nativeSceneView", QVariant::fromValue<QObject *>(ancestor));
        item->setProperty("firmwareReady", true);
        component.completeCreate();
        notesUi = item;
        notesUiError = component.errorString();
    }

    void ensureNotesActions() {
        if (!notesUi || (cornellItem && gridItem && linedItem)) return;
        QQuickItem *documentView = notesUi->parentItem();
        QQuickItem *stockAdd = namedItem(documentView, "addPageButton");
        if (!stockAdd || !stockAdd->parentItem() || !stockAdd->parentItem()->parentItem()) return;
        QQuickItem *loader = stockAdd->parentItem();
        QQuickItem *layout = loader->parentItem();
        if (!QString::fromLatin1(layout->metaObject()->className()).contains("ColumnLayout")) return;
        notesUi->setProperty("nativeToolbar", stockAdd->property("toolbar"));
        struct Action { const char *name; const char *label; const char *paper; QPointer<QQuickItem> *item; };
        const Action actions[] = {
            {"addCornellPageButton", "Add Cornell page", "P Cornell", &cornellItem},
            {"addGridPageButton", "Add grid page", "P Grid medium", &gridItem},
            {"addLinedPageButton", "Add lined page", "P Lines medium", &linedItem}};
        for (const auto &action : actions) {
            if (*action.item) continue;
            QQmlComponent component(qmlEngine(documentView));
            component.setData(NotesActionQml, QUrl("qrc:/direct-paste/NotesAction.qml"));
            QObject *created = component.beginCreate(qmlContext(documentView));
            auto *item = qobject_cast<QQuickItem *>(created);
            if (!item) {
                notesUiError = component.errorString();
                delete created;
                return;
            }
            component.setInitialProperties(created, {
                {"notes", QVariant::fromValue<QObject *>(notesUi)},
                {"stockLoader", QVariant::fromValue<QObject *>(loader)},
                {"pageLabel", QString::fromLatin1(action.label)},
                {"pageTemplate", QString::fromLatin1(action.paper)},
                {"firstAction", action.item == &cornellItem}});
            item->setObjectName(QString::fromLatin1(action.name));
            item->setParent(layout);
            item->setParentItem(layout);
            item->stackBefore(loader);
            component.completeCreate();
            *action.item = item;
            notesUiError = component.errorString();
        }
        stockAdd->setProperty("foldoutTopDivider", false);
        layout->polish();
    }

    void verifyPageReceipt() {
        if (!notesUi) return;
        const QJsonObject result = QJsonDocument::fromJson(notesUi->property("resultJson").toString().toUtf8()).object();
        const QString id = result.value("request_id").toString();
        if (!UuidRe.match(id).hasMatch()) return;
        const QString path = QString::fromLatin1(Root) + "/pages/" + id + "/receipt.json";
        const QJsonObject receipt = QJsonDocument::fromJson(readLimited(path, 8192)).object();
        if (receipt.value("state") != "invoked-unconfirmed" || result.value("state") == "invoked-unconfirmed") return;
        if (result.value("state") == "persisted") {
            const QString document = result.value("document_uuid").toString();
            const QString page = result.value("page_uuid").toString();
            if (!UuidRe.match(document).hasMatch() || !UuidRe.match(page).hasMatch()) return;
            const QJsonObject content = QJsonDocument::fromJson(readLimited(
                "/home/root/.local/share/remarkable/xochitl/" + document + ".content", 8 * 1024 * 1024)).object();
            const QJsonArray pages = content.value("cPages").toObject().value("pages").toArray();
            const bool saved = std::any_of(pages.begin(), pages.end(), [&result, &page](const QJsonValue &value) {
                const QJsonObject entry = value.toObject();
                return entry.value("id").toString() == page
                    && entry.value("template").toObject().value("value") == result.value("template");
            });
            if (!saved) return;
        }
        saveJson(path, result);
    }

    void insertNotesPage(const ActivePage &active) {
        if (!notesUi || !firmwareOk || !binaryOk || !active.guiThread) return;
        const QDir queue(QString::fromLatin1(Root) + "/pages");
        for (const QFileInfo &entry : queue.entryInfoList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name)) {
            const QString id = entry.fileName();
            if (!UuidRe.match(id).hasMatch() || entry.isSymLink()) continue;
            const QString base = entry.absoluteFilePath();
            if (QFileInfo::exists(base + "/receipt.json")) continue;
            const QJsonObject request = QJsonDocument::fromJson(readLimited(base + "/request.json", 8192)).object();
            if (request.isEmpty()) continue;
            const qint64 now = QDateTime::currentMSecsSinceEpoch();
            const QString pageTemplate = request.value("template").toString();
            const ActivePage fresh = activePage();
            const bool valid = notesUi->property("ready").toBool() && !notesUi->property("busy").toBool()
                && request.value("protocol").toInt() == Protocol
                && request.value("request_id").toString() == id
                && request.value("instance_id").toString() == instance
                && request.value("document_uuid").toString() == active.document
                && request.value("page_uuid").toString() == active.page
                && request.value("xochitl_sha256").toString() == RequiredXochitlSha
                && request.value("created_ms").toInteger() <= now
                && request.value("expires_ms").toInteger() > now
                && (pageTemplate == "P Cornell" || pageTemplate == "P Grid medium"
                    || pageTemplate == "P Lines medium" || pageTemplate == "Blank")
                && fresh.document == active.document && fresh.page == active.page && fresh.guiThread;
            QJsonObject receipt{{"request_id", id}, {"state", valid ? "attempting" : "rejected"},
                {"document_uuid", active.document}, {"source_page_uuid", active.page}, {"template", pageTemplate}};
            if (!valid) receipt["reason"] = "page, session, readiness or expiry mismatch";
            if (!saveJson(base + "/receipt.json", receipt) || !valid) continue;
            QVariant accepted;
            const bool invoked = QMetaObject::invokeMethod(notesUi, "addNotesPage", Qt::DirectConnection,
                Q_RETURN_ARG(QVariant, accepted), Q_ARG(QVariant, QVariant(pageTemplate)), Q_ARG(QVariant, QVariant(id)));
            receipt["state"] = !invoked ? "failed-unconfirmed" : accepted.toBool() ? "invoked-unconfirmed" : "rejected";
            saveJson(base + "/receipt.json", receipt);
            // At most one request per tick; the native busy guard stops double activation.
            break;
        }
    }

    QStringList pageAssets(const QString &document, const QString &page) const {
        const QDir folder("/home/root/.local/share/remarkable/xochitl/" + document + "/" + page);
        QStringList names;
        for (const QFileInfo &file : folder.entryInfoList(QDir::Files | QDir::NoDotAndDotDot, QDir::Name)) {
            const QString suffix = file.suffix().toLower();
            if (suffix == "png" || suffix == "jpg" || suffix == "jpeg") names.append(file.fileName());
        }
        return names;
    }

    void verifyReceipts() {
        const QDir inbox(QString::fromLatin1(Root) + "/incoming");
        for (const QFileInfo &entry : inbox.entryInfoList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name)) {
            if (!UuidRe.match(entry.fileName()).hasMatch() || entry.isSymLink()) continue;
            const QString path = entry.absoluteFilePath() + "/receipt.json";
            const QJsonObject receipt = QJsonDocument::fromJson(readLimited(path, 8192)).object();
            if (receipt.value("state").toString() != "invoked-unconfirmed") continue;
            const QString document = receipt.value("document_uuid").toString();
            const QString page = receipt.value("page_uuid").toString();
            if (!UuidRe.match(document).hasMatch() || !UuidRe.match(page).hasMatch()) continue;
            QSet<QString> before;
            for (const QJsonValue &item : receipt.value("assets_before").toArray()) before.insert(item.toString());
            const QByteArray scene = readLimited("/home/root/.local/share/remarkable/xochitl/" + document + "/" + page + ".rm", 80 * 1024 * 1024);
            if (scene.isEmpty()) continue;
            for (const QString &name : pageAssets(document, page)) {
                if (!before.contains(name) && scene.contains(name.toUtf8())) {
                    QJsonObject confirmed = receipt;
                    confirmed["state"] = "persisted";
                    confirmed["asset_name"] = name;
                    confirmed["persisted_ms"] = QDateTime::currentMSecsSinceEpoch();
                    saveJson(path, confirmed);
                    break;
                }
            }
        }
    }

    void insertOne(const ActivePage &active) {
        const PaperSize *paper = paperFor(active.controller ? active.controller->property("paperPortraitBounds").toRectF() : QRectF{});
        const int capWidth = paper ? int(paper->width * FitWidth + 0.5) : 0;
        const int capHeight = paper ? int(paper->height * FitHeight + 0.5) : 0;
        const QDir inbox(QString::fromLatin1(Root) + "/incoming");
        for (const QFileInfo &entry : inbox.entryInfoList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name)) {
            const QString id = entry.fileName();
            if (!UuidRe.match(id).hasMatch() || entry.isSymLink()) continue;
            const QString base = entry.absoluteFilePath();
            if (QFileInfo::exists(base + "/receipt.json")) continue;
            const QByteArray manifestBytes = readLimited(base + "/request.json", 8192);
            if (manifestBytes.isEmpty()) continue;
            QJsonParseError parseError;
            const QJsonDocument parsed = QJsonDocument::fromJson(manifestBytes, &parseError);
            const QJsonObject request = parsed.object();
            const QString imagePath = base + "/image.png";
            const QFileInfo imageInfo(imagePath);
            const QByteArray payload = readLimited(imagePath, 40 * 1024 * 1024);
            const QString digest = QString::fromLatin1(QCryptographicHash::hash(payload, QCryptographicHash::Sha256).toHex());
            QImageReader reader(imagePath);
            const QSize size = reader.size();
            const qint64 now = QDateTime::currentMSecsSinceEpoch();
            const bool valid = parseError.error == QJsonParseError::NoError && parsed.isObject()
                && request.value("protocol").toInt() == Protocol
                && request.value("request_id").toString() == id
                && request.value("instance_id").toString() == instance
                && request.value("document_uuid").toString() == active.document
                && request.value("page_uuid").toString() == active.page
                && request.value("xochitl_sha256").toString() == RequiredXochitlSha
                && request.value("image_name").toString() == "image.png"
                && request.value("created_ms").toInteger() <= now
                && request.value("expires_ms").toInteger() > now
                && !imageInfo.isSymLink() && !payload.isEmpty()
                && request.value("sha256").toString() == digest
                && reader.canRead() && size.width() >= MinImageDimension && size.height() >= MinImageDimension
                && qint64(size.width()) * size.height() <= 30000000
                && request.value("image_width").toInt() == size.width()
                && request.value("image_height").toInt() == size.height()
                && paper
                && request.value("paper_width").toDouble(1e9) == paper->width
                && request.value("paper_height").toDouble(1e9) == paper->height
                && size.width() <= capWidth && size.height() <= capHeight
                && request.value("center_x").toDouble(1e9) == 0.0
                && request.value("center_y").toDouble(1e9) == paper->height / 2.0;
            QJsonArray assetsBefore;
            for (const QString &name : pageAssets(active.document, active.page)) assetsBefore.append(name);
            QJsonObject receipt{{"request_id", id}, {"state", valid ? "attempting" : "rejected"},
                                {"document_uuid", active.document}, {"page_uuid", active.page},
                                {"assets_before", assetsBefore}};
            if (!valid) receipt["reason"] = "invalid request or page/paper/session mismatch";
            if (!saveJson(base + "/receipt.json", receipt) || !valid) continue;
            // The receipt above prevents replay if this invocation crashes.
            const ActivePage fresh = activePage();
            const PaperSize *freshPaper = fresh.controller
                ? paperFor(fresh.controller->property("paperPortraitBounds").toRectF()) : nullptr;
            if (fresh.controller != active.controller || !fresh.guiThread
                || fresh.document != active.document || fresh.page != active.page
                || freshPaper != paper) {
                receipt["state"] = "rejected";
                receipt["reason"] = "active page or paper changed immediately before insertion";
                saveJson(base + "/receipt.json", receipt);
                continue;
            }
            // Native coordinates are scene units and QPointF is the image
            // centre; a staged image is placed at the page centre.
            const bool invoked = QMetaObject::invokeMethod(fresh.controller, "insertImageFileAsSceneItem",
                Qt::DirectConnection, Q_ARG(QUrl, QUrl::fromLocalFile(imagePath)),
                Q_ARG(QPointF, QPointF(request.value("center_x").toDouble(), request.value("center_y").toDouble())));
            receipt["state"] = invoked ? "invoked-unconfirmed" : "failed-unconfirmed";
            saveJson(base + "/receipt.json", receipt);
        }
    }
#endif
    bool firmwareOk = false;
    bool binaryOk = false;
    QString instance;
    QString lastError;
    QPointer<QQuickItem> notesUi;
    QPointer<QQuickItem> cornellItem;
    QPointer<QQuickItem> gridItem;
    QPointer<QQuickItem> linedItem;
    QString notesUiError;
    int captureCount = 0;
    QString captureState = "not-requested";
    int captureWidth = 0;
    int captureHeight = 0;
};
} // namespace

extern "C" void _xovi_construct() {
    std::thread([] {
        for (int attempt = 0; attempt < 100; ++attempt) {
            if (auto *app = QCoreApplication::instance()) {
                QMetaObject::invokeMethod(app, [] { new Extension(); }, Qt::QueuedConnection);
                return;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        }
        std::fprintf(stderr, "[direct-paste] no Qt application; probe unavailable\n");
    }).detach();
}
