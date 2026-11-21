import QtQuick
import QtMultimedia
import Krok.SubtitleSkia

Rectangle {
    id: root
    color: "black"
    clip: true
    property int outputWidth: 1920
    property int outputHeight: 1080
    property string backgroundKind: "solid"
    property color backgroundColor: "black"
    property url backgroundImage
    property bool containImage: false
    signal scenePrepared(var revision, bool accepted, string error)
    signal frameSubmitted(var revision, var generation, int timeMs)

    function configureSubtitle(data, revision) {
        return subtitle.configureScene(data, revision)
    }

    function reserveSubtitle(revision) {
        return subtitle.reserveScene(revision)
    }

    function clearSubtitle(revision) {
        return subtitle.clearScene(revision)
    }

    function requestSubtitleFrame(revision, generation, timeMs) {
        return subtitle.requestFrame(revision, generation, timeMs)
    }

    Item {
        anchors.centerIn: parent
        width: root.outputWidth
        height: root.outputHeight
        scale: Math.max(root.width / width, root.height / height)
        clip: true

        Rectangle {
            anchors.fill: parent
            visible: root.backgroundKind === "solid"
            color: root.backgroundColor
        }

        Image {
            anchors.fill: parent
            visible: root.backgroundKind === "image" || root.backgroundKind === "image_sequence"
            source: root.backgroundImage
            fillMode: root.containImage ? Image.PreserveAspectFit : Image.PreserveAspectCrop
            autoTransform: true
            asynchronous: true
            cache: root.backgroundKind !== "image_sequence"
        }

        VideoOutput {
            anchors.fill: parent
            objectName: "videoOutput"
            visible: root.backgroundKind === "video"
            fillMode: VideoOutput.PreserveAspectFit
        }

        SkiaSubtitleItem {
            id: subtitle
            anchors.fill: parent
            objectName: "subtitleRenderer"
            onScenePrepared: function(revision, accepted, error) {
                root.scenePrepared(revision, accepted, error)
            }
            onFrameSubmitted: function(revision, generation, timeMs) {
                root.frameSubmitted(revision, generation, timeMs)
            }
        }
    }
}
