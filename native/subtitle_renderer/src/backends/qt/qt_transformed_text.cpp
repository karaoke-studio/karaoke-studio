#include "qt_transformed_text.h"

#include "qt_render_cache.h"
#include "qt_style_metrics.h"
#include "qt_text_layer.h"

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::legacy_qt {

using protocol::PaintFillSpec;
using protocol::ResolvedStyle;

namespace {

void paintTransformedTextStackWithFills(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const PaintFillSpec &baseFill,
    const PaintFillSpec &afterFill,
    const PaintFillSpec &beforeStrokeFill,
    const PaintFillSpec &afterStrokeFill,
    const PaintFillSpec &beforeStroke2Fill,
    const PaintFillSpec &afterStroke2Fill,
    const PaintFillSpec &beforeShadowFill,
    const PaintFillSpec &afterShadowFill,
    const ResolvedStyle &style,
    double ratio,
    bool rtl,
    int charX,
    int charWidth,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int beforeGlowRadius,
    int afterGlowRadius,
    bool forceAfter,
    const QPainterPath *uprightPath = nullptr,
    const QRectF *uprightRect = nullptr,
    const QTransform *uprightTransform = nullptr,
    const QString &glowScope = QStringLiteral("transformed_text")
) {
    const bool useCachedGlow = style.decorationKind == QStringLiteral("glow")
        && uprightPath != nullptr
        && uprightRect != nullptr
        && uprightTransform != nullptr
        && glowBitmapCacheEnabled();
    auto blitGlow = [&](const PaintFillSpec &shadowFill, int radius) {
        if (!useCachedGlow) {
            return;
        }
        blitTransformedGlowLayerWithWidths(
            painter,
            *uprightPath,
            shadowFill,
            *uprightRect,
            radius,
            strokeWidth,
            stroke2Width,
            *uprightTransform,
            glowScope
        );
    };

    const double clampedRatio = forceAfter ? 1.0 : std::clamp(ratio, 0.0, 1.0);
    if (clampedRatio <= 0.0) {
        blitGlow(beforeShadowFill, beforeGlowRadius);
        paintTextLayerStackWithWidths(
            painter,
            path,
            rect,
            baseFill,
            beforeStrokeFill,
            beforeStroke2Fill,
            beforeShadowFill,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            beforeGlowRadius,
            !useCachedGlow,
            glowScope + QStringLiteral(":before")
        );
        return;
    }
    if (clampedRatio >= 1.0) {
        blitGlow(afterShadowFill, afterGlowRadius);
        paintTextLayerStackWithWidths(
            painter,
            path,
            rect,
            afterFill,
            afterStrokeFill,
            afterStroke2Fill,
            afterShadowFill,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            afterGlowRadius,
            !useCachedGlow,
            glowScope + QStringLiteral(":after")
        );
        return;
    }

    blitGlow(beforeShadowFill, beforeGlowRadius);
    paintTextLayerStackWithWidths(
        painter,
        path,
        rect,
        baseFill,
        beforeStrokeFill,
        beforeStroke2Fill,
        beforeShadowFill,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        beforeGlowRadius,
        !useCachedGlow,
        glowScope + QStringLiteral(":before")
    );

    const double strokePad = visualStrokeExtentForWidths(strokeWidth, stroke2Width);
    const double clipX = rtl
        ? charX + charWidth * (1.0 - clampedRatio)
        : charX;
    const double clipWidth = std::max(charWidth * clampedRatio + strokePad, 1.0);
    painter.save();
    painter.setClipRect(
        QRectF(
            clipX - strokePad,
            rect.top() - strokePad,
            clipWidth,
            rect.height() + strokePad * 2.0
        ),
        Qt::IntersectClip
    );
    blitGlow(afterShadowFill, afterGlowRadius);
    paintTextLayerStackWithWidths(
        painter,
        path,
        rect,
        afterFill,
        afterStrokeFill,
        afterStroke2Fill,
        afterShadowFill,
        style,
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        afterGlowRadius,
        !useCachedGlow,
        glowScope + QStringLiteral(":after")
    );
    painter.restore();
}

}  // namespace

void paintTransformedTextStack(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const ResolvedStyle &style,
    double ratio,
    bool rtl,
    int charX,
    int charWidth,
    bool forceAfter,
    const QPainterPath *uprightPath,
    const QRectF *uprightRect,
    const QTransform *uprightTransform,
    const QString &glowScope
) {
    paintTransformedTextStackWithFills(
        painter,
        path,
        rect,
        style.baseFill,
        style.afterFill,
        style.beforeStrokeFill,
        style.afterStrokeFill,
        style.beforeStroke2Fill,
        style.afterStroke2Fill,
        style.beforeShadowFill,
        style.afterShadowFill,
        style,
        ratio,
        rtl,
        charX,
        charWidth,
        style.strokeWidthPx,
        style.stroke2WidthPx,
        style.shadowOffsetX,
        style.shadowOffsetY,
        glowRadius(style, false),
        glowRadius(style, true),
        forceAfter,
        uprightPath,
        uprightRect,
        uprightTransform,
        glowScope
    );
}

void paintRubyTransformedStack(
    QPainter &painter,
    const QPainterPath &path,
    const QRectF &rect,
    const ResolvedStyle &style,
    double ratio,
    bool rtl,
    bool forceAfter,
    const QPainterPath *uprightPath,
    const QRectF *uprightRect,
    const QTransform *uprightTransform,
    const QString &glowScope
) {
    const double scale = rubyScale(style);
    const int strokeWidth = scaledPx(style.strokeWidthPx, scale);
    const int stroke2Width = scaledPx(style.stroke2WidthPx, scale);
    const int shadowOffsetX = scaledSignedPx(style.shadowOffsetX, scale);
    const int shadowOffsetY = scaledSignedPx(style.shadowOffsetY, scale);
    paintTransformedTextStackWithFills(
        painter,
        path,
        rect,
        style.rubyBaseFill,
        style.rubyAfterFill,
        style.rubyBeforeStrokeFill,
        style.rubyAfterStrokeFill,
        style.rubyBeforeStroke2Fill,
        style.rubyAfterStroke2Fill,
        style.rubyBeforeShadowFill,
        style.rubyAfterShadowFill,
        style,
        ratio,
        rtl,
        static_cast<int>(std::round(rect.left())),
        std::max(1, static_cast<int>(std::round(rect.width()))),
        strokeWidth,
        stroke2Width,
        shadowOffsetX,
        shadowOffsetY,
        scaledPx(glowRadius(style, false), scale),
        scaledPx(glowRadius(style, true), scale),
        forceAfter,
        uprightPath,
        uprightRect,
        uprightTransform,
        glowScope
    );
}

}  // namespace krok::subtitle::native::legacy_qt
