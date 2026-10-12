// Runs in the existing DocumentView context, using the same controllers,
// paper size, selection cleanup and completion callback as its Add page action.
constexpr auto NativePagesQml = R"QML(
import QtQuick
import QtQuick.Controls
import com.remarkable
import xofm.libs.library
import xofm.libs.devicescreen

Item {
    id: notes
    objectName: "directPasteNotesPages"
    property var documentView
    property var nativeToolbar
    property var nativeSceneView
    property bool firmwareReady: false
    property bool busy: false
    property string resultJson: "{}"
    readonly property bool ready: firmwareReady && documentView && documentView.visible
        && documentView.documentLoaded && !documentView.isBusy && !documentView.isWorking
        && !documentView.addingPage && !documentView.subDialogOpen
        && documentView.portrait && documentView.document
        && !documentView.shouldRestrictMethods
        && (documentView.document.fileType === Document.Notebook
            || documentView.document.fileType === Document.Pdf)
    anchors.fill: parent
    z: 100

    function addNotesPage(templateName, requestId) {
        if (!ready || busy || ["P Cornell", "P Grid medium", "P Lines medium", "Blank"].indexOf(templateName) < 0)
            return false;
        const view = documentView;
        const doc = view.document;
        if (doc.status === Entry.Exporting) {
            view.notifyPageExportWarning();
            return false;
        }
        const sourcePage = view.currentPageId;
        const index = view.currentPage + 1;
        const count = doc.pageCount;
        if (index < 1 || index > count) return false;
        const priorIds = [];
        for (let i = 0; i < count; ++i) priorIds.push(doc.idForPage(i));
        const request = requestId || ("native-" + Date.now());
        const base = { request_id: request, document_uuid: String(doc.id),
            source_page_uuid: sourcePage, template: templateName, page_index: index };
        busy = true;
        view.addingPage = true;
        resultJson = JSON.stringify(Object.assign({}, base, {state: "invoked-unconfirmed"}));
        if (nativeToolbar) nativeToolbar.closeFoldout();
        if (view.formatMenu) view.formatMenu.contract();
        if (nativeSceneView) nativeSceneView.endItemSelection();
        try {
            DocumentController.addPageWithTemplateAndPageSize(doc.id, index, templateName,
                DeviceScreenInfo.paperSize, function() {
                    const pageId = String(doc.idForPage(index));
                    const success = doc.pageCount === count + 1 && priorIds.indexOf(pageId) < 0
                        && doc.templateForPage(index) === templateName;
                    resultJson = JSON.stringify(Object.assign({}, base,
                        {state: success ? "persisted" : "invoked-unconfirmed", page_uuid: pageId}));
                    busy = false;
                    view.addingPage = false;
                    if (success && view.visible && view.document === doc && view.currentPageId === sourcePage)
                        view.openPage(index);
                });
        } catch (error) {
            busy = false;
            view.addingPage = false;
            resultJson = JSON.stringify(Object.assign({}, base,
                {state: "failed-unconfirmed", reason: String(error)}));
        }
        return true;
    }

}
)QML";

// Rows in the existing document toolbar menu, immediately before
// the stock Add page action. Ark supplies the same font, spacing and touch
// behavior as its neighboring menu items.
constexpr auto NotesActionQml = R"QML(
import QtQuick
import QtQuick.Layouts
import ark.controls as ArkControls

ArkControls.FoldoutItem {
    required property var notes
    required property var stockLoader
    required property string pageLabel
    required property string pageTemplate
    required property bool firstAction
    label: pageLabel
    iconSource: "qrc:/ark/icons/page_add"
    topDivider: firstAction
    bottomDivider: false
    focusPolicy: Qt.NoFocus
    Layout.fillWidth: true
    visible: stockLoader.visible && notes.documentView.portrait
    enabled: notes.ready && !notes.busy
    MouseArea {
        anchors.fill: parent
        onClicked: notes.addNotesPage(pageTemplate, "")
    }
}
)QML";
