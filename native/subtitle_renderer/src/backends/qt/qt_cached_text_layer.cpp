#include "qt_cached_text_layer.h"

#include "qt_render_cache.h"
#include "qt_text_layer.h"

namespace krok::subtitle::native::legacy_qt {

using protocol::PaintFillSpec;
using protocol::ResolvedStyle;

void paintCachedTextLayerStackWithWidths(
    QPainter &painter,
    const QString &cacheKey,
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    const ResolvedStyle &style,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
) {
    if (!textLayerCacheEnabled()) {
        const TextLayerImage layer = buildTextLayerStackWithWidths(
            path, rect, fill, stroke, stroke2, shadow, style,
            strokeWidth, stroke2Width, shadowOffsetX, shadowOffsetY,
            glowRadiusValue
        );
        painter.drawImage(layer.offset, layer.image);
        return;
    }
    if (const auto cached = lookupTextLayerCache(cacheKey)) {
        painter.drawImage(cached->offset, cached->image);
        return;
    }
    const TextLayerImage layer = buildTextLayerStackWithWidths(
        path, rect, fill, stroke, stroke2, shadow, style,
        strokeWidth, stroke2Width, shadowOffsetX, shadowOffsetY,
        glowRadiusValue
    );
    storeTextLayerCache(cacheKey, layer);
    painter.drawImage(layer.offset, layer.image);
}

}  // namespace krok::subtitle::native::legacy_qt
