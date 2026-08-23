#include "qt_cached_ruby_layer.h"

#include "qt_font_factory.h"
#include "qt_render_cache.h"
#include "qt_ruby_layout.h"
#include "qt_style_metrics.h"
#include "qt_text_layer.h"

#include <QtGui/QImage>
#include <QtGui/QPainterPath>

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::legacy_qt {

using protocol::PaintFillSpec;
using protocol::ResolvedStyle;

namespace {

RubyLayerImage buildRubyTextLayer(
    const RubyDiagnostics &ruby,
    const QFont &rubyFont,
    const QFontMetricsF &rubyMetrics,
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

    const int rubyWidth = std::max(1, static_cast<int>(std::ceil(ruby.readingWidth)));
    const int rubyHeight = std::max(1, static_cast<int>(std::ceil(rubyMetrics.height())));
    const int imageWidth = std::max(1, padLeft + rubyWidth + padRight);
    const int imageHeight = std::max(1, padTop + rubyHeight + padBottom);

    QImage image(imageWidth, imageHeight, QImage::Format_ARGB32_Premultiplied);
    image.fill(Qt::transparent);

    const double localBaseline = padTop + rubyMetrics.ascent();
    const QPainterPath localPath = rubyTextPath(
        ruby.reading,
        rubyFont,
        rubyMetrics,
        padLeft,
        localBaseline,
        ruby.targetWidth
    );
    const QRectF localRect(
        padLeft,
        localBaseline - rubyMetrics.ascent(),
        ruby.readingWidth,
        rubyMetrics.height()
    );

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

    return RubyLayerImage{
        image,
        QPointF(-padLeft, -(padTop + rubyMetrics.ascent())),
    };
}

QString rubyTextLayerCacheKey(
    const RubyDiagnostics &ruby,
    const QFont &rubyFont,
    const QFontMetricsF &rubyMetrics,
    const QString &phase,
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
    return QStringLiteral("ruby|%1|reading=%2|target=%3|reading_w=%4|font=%5|height=%6|ascent=%7|style=%8")
        .arg(phase)
        .arg(ruby.reading)
        .arg(doubleCacheKey(ruby.targetWidth))
        .arg(doubleCacheKey(ruby.readingWidth))
        .arg(fontCacheKey(rubyFont))
        .arg(doubleCacheKey(rubyMetrics.height()))
        .arg(doubleCacheKey(rubyMetrics.ascent()))
        .arg(textStackStyleCacheKey(
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
        ));
}

}  // namespace

RubyLayerImage cachedRubyTextLayer(
    const RubyDiagnostics &ruby,
    const QFont &rubyFont,
    const QFontMetricsF &rubyMetrics,
    const QString &phase,
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
    const QString cacheKey = rubyTextLayerCacheKey(
        ruby,
        rubyFont,
        rubyMetrics,
        phase,
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
    if (textLayerCacheEnabled()) {
        if (const auto cached = lookupTextLayerCache(cacheKey)) {
            return RubyLayerImage{cached->image, cached->offset};
        }
    }

    const RubyLayerImage layer = buildRubyTextLayer(
        ruby,
        rubyFont,
        rubyMetrics,
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
    if (textLayerCacheEnabled()) {
        storeTextLayerCache(cacheKey, TextLayerImage{layer.image, layer.offset});
    }
    return layer;
}

}  // namespace krok::subtitle::native::legacy_qt
