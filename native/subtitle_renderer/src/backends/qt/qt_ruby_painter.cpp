#include "qt_ruby_painter.h"

#include "qt_cached_ruby_layer.h"
#include "qt_font_factory.h"
#include "qt_style_metrics.h"

#include <QtCore/QPointF>
#include <QtCore/QRectF>
#include <QtGui/QFontMetricsF>

namespace krok::subtitle::native::legacy_qt {

using protocol::PaintFillSpec;
using protocol::ResolvedStyle;

void paintRubyDiagnostics(
    QPainter &painter,
    const ResolvedStyle &style,
    const std::vector<RubyDiagnostics> &rubies,
    const PaintFillSpec &base,
    const PaintFillSpec &fill,
    const PaintFillSpec &beforeStroke,
    const PaintFillSpec &afterStroke,
    const PaintFillSpec &beforeStroke2,
    const PaintFillSpec &afterStroke2,
    const PaintFillSpec &beforeShadow,
    const PaintFillSpec &afterShadow
) {
    if (rubies.empty()) {
        return;
    }
    const QFont rubyFont = buildRubyFont(style);
    const QFontMetricsF rubyMetrics(rubyFont);
    const double scale = rubyScale(style);
    const int strokeWidth = scaledPx(style.strokeWidthPx, scale);
    const int stroke2Width = scaledPx(style.stroke2WidthPx, scale);
    const int shadowOffsetX = scaledSignedPx(style.shadowOffsetX, scale);
    const int shadowOffsetY = scaledSignedPx(style.shadowOffsetY, scale);
    for (const RubyDiagnostics &ruby : rubies) {
        const RubyLayerImage beforeLayer = cachedRubyTextLayer(
            ruby,
            rubyFont,
            rubyMetrics,
            QStringLiteral("before"),
            base,
            beforeStroke,
            beforeStroke2,
            beforeShadow,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            scaledPx(glowRadius(style, false), scale)
        );
        painter.drawImage(QPointF(ruby.x, ruby.baselineY) + beforeLayer.offset, beforeLayer.image);
        if (ruby.progress <= 0.0) {
            continue;
        }
        const RubyLayerImage afterLayer = cachedRubyTextLayer(
            ruby,
            rubyFont,
            rubyMetrics,
            QStringLiteral("after"),
            fill,
            afterStroke,
            afterStroke2,
            afterShadow,
            style,
            strokeWidth,
            stroke2Width,
            shadowOffsetX,
            shadowOffsetY,
            scaledPx(glowRadius(style, true), scale)
        );
        painter.save();
        painter.setClipRect(
            QRectF(
                ruby.afterClipLeft,
                ruby.afterClipTop,
                ruby.afterClipRight - ruby.afterClipLeft,
                ruby.afterClipHeight
            ),
            Qt::IntersectClip
        );
        painter.drawImage(QPointF(ruby.x, ruby.baselineY) + afterLayer.offset, afterLayer.image);
        painter.restore();
    }
}

}  // namespace krok::subtitle::native::legacy_qt
