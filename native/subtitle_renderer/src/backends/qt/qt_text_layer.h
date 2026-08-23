#pragma once

#include "qt_render_types.h"

#include <QtCore/QRectF>
#include <QtCore/QString>
#include <QtGui/QPainter>
#include <QtGui/QPainterPath>
#include <QtGui/QTransform>

namespace krok::subtitle::native::legacy_qt {

void blitTransformedGlowLayerWithWidths(
    QPainter &painter, const QPainterPath &uprightPath,
    const protocol::PaintFillSpec &fill, const QRectF &uprightRect,
    int radius, int strokeWidth, int stroke2Width,
    const QTransform &transform,
    const QString &scope = QStringLiteral("transformed_text")
);
void paintTextLayerStackWithWidths(
    QPainter &painter, const QPainterPath &path, const QRectF &rect,
    const protocol::PaintFillSpec &fill,
    const protocol::PaintFillSpec &stroke,
    const protocol::PaintFillSpec &stroke2,
    const protocol::PaintFillSpec &shadow,
    const protocol::ResolvedStyle &style,
    int strokeWidth, int stroke2Width,
    int shadowOffsetX, int shadowOffsetY, int glowRadiusValue,
    bool drawGlow = true,
    const QString &glowScope = QStringLiteral("text")
);
TextLayerImage buildTextLayerStackWithWidths(
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
