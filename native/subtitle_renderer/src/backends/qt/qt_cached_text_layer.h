#pragma once

#include "qt_render_types.h"

#include <QtCore/QRectF>
#include <QtCore/QString>
#include <QtGui/QPainter>
#include <QtGui/QPainterPath>

namespace krok::subtitle::native::legacy_qt {

void paintCachedTextLayerStackWithWidths(
    QPainter &painter, const QString &cacheKey,
    const QPainterPath &path, const QRectF &rect,
    const protocol::PaintFillSpec &fill,
    const protocol::PaintFillSpec &stroke,
    const protocol::PaintFillSpec &stroke2,
    const protocol::PaintFillSpec &shadow,
    const protocol::ResolvedStyle &style,
    int strokeWidth, int stroke2Width,
    int shadowOffsetX, int shadowOffsetY, int glowRadiusValue
);

}  // namespace krok::subtitle::native::legacy_qt
