#include "qt_text_layer.h"

#include "qt_fill_brush.h"
#include "qt_render_cache.h"
#include "qt_style_metrics.h"

#include <QtGui/QPen>

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::legacy_qt {

using protocol::PaintFillSpec;
using protocol::ResolvedStyle;

namespace {

void paintKaraokePathWithWidths(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    int strokeWidth,
    int stroke2Width
) {
    if (stroke2Width > 0) {
        painter.strokePath(path, QPen(brushForFill(stroke2, rect), strokeWidth + stroke2Width, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    }
    if (strokeWidth > 0) {
        painter.strokePath(path, QPen(brushForFill(stroke, rect), strokeWidth, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    }
    painter.fillPath(path, brushForFill(fill, rect));
}


GlowLayerImage buildGlowLayerWithWidths(
    const QPainterPath &path,
    const PaintFillSpec &fill,
    const QRectF &rect,
    int radius,
    int strokeWidth,
    int stroke2Width,
    const QString &scope = QStringLiteral("unknown")
) {
    const int glowRadius = std::max(radius, 1);
    const int baseWidth = stroke2Width > 0 ? strokeWidth + stroke2Width : std::max(strokeWidth, 0);
    const int glowWidth = std::max(1, baseWidth + glowRadius);
    const QRectF bounds = path.boundingRect();
    if (bounds.isEmpty()) {
        return GlowLayerImage{};
    }
    const double pad = std::ceil(glowWidth / 2.0 + glowRadius * 3.0) + 2.0;
    const QRectF layerRect = bounds.adjusted(-pad, -pad, pad, pad);
    const int imageWidth = std::max(1, static_cast<int>(std::ceil(layerRect.width())));
    const int imageHeight = std::max(1, static_cast<int>(std::ceil(layerRect.height())));

    QImage source(imageWidth, imageHeight, QImage::Format_ARGB32_Premultiplied);
    source.fill(Qt::transparent);

    QPainterPath localPath(path);
    localPath.translate(-layerRect.left(), -layerRect.top());
    const QRectF localRect = rect.translated(-layerRect.left(), -layerRect.top());

    QPainter layerPainter(&source);
    layerPainter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing);
    layerPainter.strokePath(localPath, QPen(brushForFill(fill, localRect), glowWidth, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    layerPainter.end();

    return GlowLayerImage{
        cachedBlurImage(source, glowRadius, scope),
        QPointF(layerRect.left(), layerRect.top()),
    };
}

void paintGlowPathWithWidths(
    QPainter &painter,
    const QPainterPath &path,
    const PaintFillSpec &fill,
    const QRectF &rect,
    int radius,
    int strokeWidth,
    int stroke2Width,
    const QString &scope = QStringLiteral("text")
) {
    const GlowLayerImage layer = buildGlowLayerWithWidths(path, fill, rect, radius, strokeWidth, stroke2Width, scope);
    if (!layer.image.isNull()) {
        painter.drawImage(layer.offset, layer.image);
    }
}

}  // namespace

void blitTransformedGlowLayerWithWidths(
    QPainter &painter,
    const QPainterPath &uprightPath,
    const PaintFillSpec &fill,
    const QRectF &uprightRect,
    int radius,
    int strokeWidth,
    int stroke2Width,
    const QTransform &transform,
    const QString &scope
) {
    const GlowLayerImage layer = buildGlowLayerWithWidths(
        uprightPath,
        fill,
        uprightRect,
        radius,
        strokeWidth,
        stroke2Width,
        scope
    );
    if (layer.image.isNull()) {
        return;
    }
    painter.save();
    painter.setRenderHint(QPainter::SmoothPixmapTransform, true);
    painter.setTransform(transform, true);
    painter.drawImage(layer.offset, layer.image);
    painter.restore();
}

void paintTextLayerStackWithWidths(
    QPainter &painter,
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
    int glowRadiusValue,
    bool drawGlow,
    const QString &glowScope
) {
    if (style.decorationKind == QStringLiteral("glow") && drawGlow) {
        paintGlowPathWithWidths(
            painter,
            path,
            shadow,
            rect,
            glowRadiusValue,
            strokeWidth,
            stroke2Width,
            glowScope
        );
    } else if (style.decorationKind == QStringLiteral("shadow")
               && (shadowOffsetX != 0 || shadowOffsetY != 0)) {
        QPainterPath shadowPath(path);
        shadowPath.translate(shadowOffsetX, shadowOffsetY);
        painter.fillPath(shadowPath, brushForFill(shadow, rect.translated(shadowOffsetX, shadowOffsetY)));
    }

    paintKaraokePathWithWidths(
        painter,
        path,
        rect,
        fill,
        stroke,
        stroke2,
        strokeWidth,
        stroke2Width
    );
}

TextLayerImage buildTextLayerStackWithWidths(
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
    const QRectF bounds = path.boundingRect().united(rect);
    if (bounds.isEmpty()) {
        return TextLayerImage{};
    }
    const double strokeExtent = visualStrokeExtentForWidths(strokeWidth, stroke2Width);
    const double glowExtra = style.decorationKind == QStringLiteral("glow")
        ? glowExtentForWidths(strokeWidth, stroke2Width, glowRadiusValue)
        : 0.0;
    const int extent = static_cast<int>(std::max({
        strokeExtent,
        glowExtra,
        static_cast<double>(std::abs(shadowOffsetX)),
        static_cast<double>(std::abs(shadowOffsetY)),
        2.0,
    })) + 4;
    const int padLeft = std::max(0, -shadowOffsetX) + extent;
    const int padRight = std::max(0, shadowOffsetX) + extent;
    const int padTop = std::max(0, -shadowOffsetY) + extent;
    const int padBottom = std::max(0, shadowOffsetY) + extent;

    const QRectF layerRect(
        std::floor(bounds.left() - padLeft),
        std::floor(bounds.top() - padTop),
        std::ceil(bounds.width() + padLeft + padRight),
        std::ceil(bounds.height() + padTop + padBottom)
    );
    const int imageWidth = std::max(1, static_cast<int>(std::ceil(layerRect.width())));
    const int imageHeight = std::max(1, static_cast<int>(std::ceil(layerRect.height())));

    QImage image(imageWidth, imageHeight, QImage::Format_ARGB32_Premultiplied);
    image.fill(Qt::transparent);

    QPainterPath localPath(path);
    localPath.translate(-layerRect.left(), -layerRect.top());
    const QRectF localRect = rect.translated(-layerRect.left(), -layerRect.top());

    QPainter layerPainter(&image);
    layerPainter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing);
    paintTextLayerStackWithWidths(
        layerPainter,
        localPath,
        localRect,
        fill,
        stroke,
        stroke2,
        shadow,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        glowRadiusValue
    );
    layerPainter.end();

    return TextLayerImage{
        image,
        QPointF(layerRect.left(), layerRect.top()),
    };
}

}  // namespace krok::subtitle::native::legacy_qt
