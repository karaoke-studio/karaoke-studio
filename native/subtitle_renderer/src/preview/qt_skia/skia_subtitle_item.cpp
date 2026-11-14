#include "skia_subtitle_item.h"
#include <rhi/qrhi.h>

namespace krok::subtitle::preview {
namespace {
// Minimal renderer: owns no GPU resources yet and leaves a transparent texture, so the item
// composites over whatever the host draws underneath.
class SkiaSubtitleRenderer final : public QQuickRhiItemRenderer {
protected:
    void initialize(QRhiCommandBuffer*) override {}
    void synchronize(QQuickRhiItem*) override {}
    void render(QRhiCommandBuffer* cb) override {
        cb->beginPass(renderTarget(), Qt::transparent, {1.0f, 0});
        cb->endPass();
    }
};
}

SkiaSubtitleItem::SkiaSubtitleItem(QQuickItem* parent) : QQuickRhiItem(parent) {
    setAlphaBlending(true);
}

QQuickRhiItemRenderer* SkiaSubtitleItem::createRenderer() {
    return new SkiaSubtitleRenderer;
}
}
