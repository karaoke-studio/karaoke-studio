#include "skia_subtitle_item.h"
#include <QtCore/QLoggingCategory>
#include <QtCore/QMetaObject>
#include <rhi/qrhi.h>

namespace krok::subtitle::preview {
namespace {
Q_LOGGING_CATEGORY(lcPreview, "krok.subtitle.skia.preview")

// The one GPU API the shared Skia core drives on this platform. Platform branches belong in
// this adapter only, never in the core.
constexpr QRhi::Implementation requiredBackend() {
#ifdef Q_OS_MACOS
    return QRhi::Metal;
#else
    return QRhi::Vulkan;
#endif
}

class SkiaSubtitleRenderer final : public QQuickRhiItemRenderer {
protected:
    void initialize(QRhiCommandBuffer*) override {
        // Runs again when the item's texture is recreated (resize, new window): re-report then.
        backend_ = QString::fromLatin1(rhi()->backendName());
        accepted_ = rhi()->backend() == requiredBackend();
        reported_ = false;
        if (!accepted_)
            qCWarning(lcPreview) << "Rejecting scene graph backend" << backend_
                                 << "- Skia preview requires" << requiredName();
    }
    void synchronize(QQuickRhiItem* item) override {
        // synchronize() precedes initialize() within a frame, so a fresh result is published
        // on the following frame, which render() requests below.
        if (!backend_.isEmpty() && !reported_) {
            static_cast<SkiaSubtitleItem*>(item)->publishGraphicsBackend(backend_, accepted_);
            reported_ = true;
        }
    }
    void render(QRhiCommandBuffer* cb) override {
        // A transparent texture either way; nothing else is drawn on an unaccepted backend.
        cb->beginPass(renderTarget(), Qt::transparent, {1.0f, 0});
        cb->endPass();
        if (!reported_) update();
    }
private:
    static const char* requiredName() { return requiredBackend() == QRhi::Metal ? "Metal" : "Vulkan"; }
    QString backend_;
    bool accepted_ = false;
    bool reported_ = false;
};
}

SkiaSubtitleItem::SkiaSubtitleItem(QQuickItem* parent) : QQuickRhiItem(parent) {
    setAlphaBlending(true);
}

void SkiaSubtitleItem::publishGraphicsBackend(const QString& backend, bool accepted) {
    if (backend == backend_ && accepted == accepted_) return;
    backend_ = backend;
    accepted_ = accepted;
    // Never emit on the render thread: QML bindings and Python slots belong to the GUI thread.
    QMetaObject::invokeMethod(this, &SkiaSubtitleItem::graphicsBackendChanged, Qt::QueuedConnection);
}

QQuickRhiItemRenderer* SkiaSubtitleItem::createRenderer() {
    return new SkiaSubtitleRenderer;
}
}
