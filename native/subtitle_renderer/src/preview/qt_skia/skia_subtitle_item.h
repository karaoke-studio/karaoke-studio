#pragma once
#include "preview_scene_input.h"
#include <QtCore/QFutureWatcher>
#include <QtQuick/QQuickRhiItem>
#include <QtQml/qqmlregistration.h>
#include <optional>

namespace krok::subtitle::preview {
struct PreviewFrame {
    std::shared_ptr<const ::subtitle::skia::PreparedScene> scene;
    quint64 revision = 0;
    quint64 generation = 0;
    int timeMs = 0;
};

// Host-side subtitle preview surface. It renders on Qt's scene graph render thread into the
// texture QQuickRhiItem owns, so frames reach the screen without a CPU readback. Subtitle
// drawing itself comes from the shared Skia core; this type only bridges Qt and the GPU.
//
// Only the platform's Skia GPU API is accepted (Vulkan on Windows/Linux, Metal on macOS).
// Any other scene graph backend (Windows' default D3D11, OpenGL, software) is reported through
// graphicsBackend/graphicsBackendAccepted and nothing is drawn, instead of silently running
// on an API the shared core does not support.
class SkiaSubtitleItem : public QQuickRhiItem {
    Q_OBJECT
    QML_ELEMENT
    Q_PROPERTY(QString graphicsBackend READ graphicsBackend NOTIFY graphicsBackendChanged FINAL)
    Q_PROPERTY(bool graphicsBackendAccepted READ graphicsBackendAccepted NOTIFY graphicsBackendChanged FINAL)
    Q_PROPERTY(bool motionProbeEnabled READ motionProbeEnabled WRITE setMotionProbeEnabled NOTIFY motionProbeEnabledChanged FINAL)
public:
    explicit SkiaSubtitleItem(QQuickItem* parent = nullptr);
    QString graphicsBackend() const { return backend_; }
    bool graphicsBackendAccepted() const { return accepted_; }
    bool motionProbeEnabled() const { return motionProbeEnabled_; }
    void setMotionProbeEnabled(bool enabled);
    Q_INVOKABLE bool configureScene(const QByteArray& ir, quint64 revision);
    Q_INVOKABLE bool requestFrame(quint64 revision, quint64 generation, int timeMs);
    // Read only during synchronize() while Qt blocks the GUI thread.
    bool usesMotionProbe() const { return motionProbeEnabled_ && requestedRevision_ == 0; }
    PreviewFrame frameRequest() const { return frame_; }
    // Render thread, while the GUI thread is blocked in synchronize().
    void publishGraphicsBackend(const QString& backend, bool accepted);
signals:
    void graphicsBackendChanged();
    void motionProbeEnabledChanged();
    void scenePrepared(quint64 revision, bool accepted, const QString& error);
protected:
    QQuickRhiItemRenderer* createRenderer() override;
private:
    struct PendingConfig { QByteArray ir; quint64 revision; };
    void startPreparation();
    void finishPreparation();
    QFutureWatcher<PreviewSceneResult> preparation_;
    std::optional<PendingConfig> pendingConfig_;
    quint64 requestedRevision_ = 0;
    quint64 preparingRevision_ = 0;
    quint64 sceneRevision_ = 0;
    std::shared_ptr<const ::subtitle::skia::PreparedScene> scene_;
    PreviewFrame frame_;
    QString backend_;
    bool accepted_ = false;
    bool motionProbeEnabled_ = false;
};
}
