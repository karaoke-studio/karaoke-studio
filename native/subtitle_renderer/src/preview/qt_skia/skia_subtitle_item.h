#pragma once
#include <QtQuick/QQuickRhiItem>
#include <QtQml/qqmlregistration.h>

namespace krok::subtitle::preview {
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
public:
    explicit SkiaSubtitleItem(QQuickItem* parent = nullptr);
    QString graphicsBackend() const { return backend_; }
    bool graphicsBackendAccepted() const { return accepted_; }
    // Render thread, while the GUI thread is blocked in synchronize().
    void publishGraphicsBackend(const QString& backend, bool accepted);
signals:
    void graphicsBackendChanged();
protected:
    QQuickRhiItemRenderer* createRenderer() override;
private:
    QString backend_;
    bool accepted_ = false;
};
}
