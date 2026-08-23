#include "qt_glyph_run.h"

#include "qt_cached_text_layer.h"
#include "qt_font_factory.h"
#include "qt_render_cache.h"
#include "qt_style_metrics.h"

#include <QtGui/QFontMetricsF>

#include <algorithm>
#include <cmath>
#include <limits>

namespace krok::subtitle::native::legacy_qt {

using protocol::PaintFillSpec;
using protocol::ResolvedStyle;
using protocol::TimingLine;

namespace {

struct GlyphRunRef {
    std::size_t start = 0;
    std::size_t end = 0;
};

const ResolvedStyle &layoutCharStyle(
    const LineLayout &layout,
    const ResolvedStyle &lineStyle,
    std::size_t index
) {
    if (layout.hasInlineStyles && index < layout.charStyles.size() && layout.charStyles[index] != nullptr) {
        return *layout.charStyles[index];
    }
    return lineStyle;
}

QFont layoutCharFont(const LineLayout &layout, std::size_t index) {
    if (index < layout.charFonts.size()) {
        return layout.charFonts[index];
    }
    return layout.font;
}

QString glyphRunVisualSignature(
    const LineLayout &layout,
    const ResolvedStyle &lineStyle,
    std::size_t index
) {
    const ResolvedStyle &style = layoutCharStyle(layout, lineStyle, index);
    return QStringLiteral("font=%1|before=%2|after=%3")
        .arg(fontCacheKey(layoutCharFont(layout, index)))
        .arg(textStackStyleCacheKey(
            style.baseFill,
            style.beforeStrokeFill,
            style.beforeStroke2Fill,
            style.beforeShadowFill,
            style,
            style.strokeWidthPx,
            style.stroke2WidthPx,
            style.shadowOffsetX,
            style.shadowOffsetY,
            glowRadius(style, false)
        ))
        .arg(textStackStyleCacheKey(
            style.afterFill,
            style.afterStrokeFill,
            style.afterStroke2Fill,
            style.afterShadowFill,
            style,
            style.strokeWidthPx,
            style.stroke2WidthPx,
            style.shadowOffsetX,
            style.shadowOffsetY,
            glowRadius(style, true)
        ));
}

std::vector<GlyphRunRef> glyphRunsForLayout(
    const TimingLine &line,
    const LineLayout &layout,
    const ResolvedStyle &lineStyle
) {
    std::vector<GlyphRunRef> runs;
    if (line.chars.empty()) {
        return runs;
    }
    if (!layout.hasInlineStyles) {
        runs.push_back(GlyphRunRef{0, line.chars.size()});
        return runs;
    }
    std::size_t start = 0;
    QString current = glyphRunVisualSignature(layout, lineStyle, 0);
    for (std::size_t index = 1; index < line.chars.size(); ++index) {
        const QString signature = glyphRunVisualSignature(layout, lineStyle, index);
        if (signature == current) {
            continue;
        }
        runs.push_back(GlyphRunRef{start, index});
        start = index;
        current = signature;
    }
    runs.push_back(GlyphRunRef{start, line.chars.size()});
    return runs;
}

QPainterPath glyphRunPath(
    const TimingLine &line,
    const LineLayout &layout,
    const GlyphRunRef &run
) {
    QPainterPath path;
    for (std::size_t index = run.start; index < run.end; ++index) {
        if (index >= line.chars.size() || index >= layout.charLefts.size()) {
            continue;
        }
        path.addText(
            QPointF(layout.charLefts[index], layout.baselineY),
            layoutCharFont(layout, index),
            line.chars[index].text
        );
    }
    return path;
}

QRectF glyphRunRect(
    const TimingLine &line,
    const LineLayout &layout,
    const GlyphRunRef &run
) {
    double left = std::numeric_limits<double>::infinity();
    double right = -std::numeric_limits<double>::infinity();
    double ascent = 0.0;
    double descent = 0.0;
    for (std::size_t index = run.start; index < run.end; ++index) {
        if (index >= line.chars.size() || index >= layout.charLefts.size() || index >= layout.charWidths.size()) {
            continue;
        }
        const QFont font = layoutCharFont(layout, index);
        const QFontMetricsF metrics(font);
        left = std::min(left, layout.charLefts[index]);
        right = std::max(right, layout.charLefts[index] + layout.charWidths[index]);
        ascent = std::max(ascent, metrics.ascent());
        descent = std::max(descent, metrics.descent());
    }
    if (!std::isfinite(left) || !std::isfinite(right)) {
        return QRectF();
    }
    return QRectF(
        left,
        layout.baselineY - ascent,
        std::max(right - left, 1.0),
        std::max(ascent + descent, 1.0)
    );
}

QString glyphRunTextLayerCacheKey(
    const TimingLine &line,
    const LineLayout &layout,
    const GlyphRunRef &run,
    const QRectF &rect,
    const QString &phase,
    const ResolvedStyle &style,
    const PaintFillSpec &fill,
    const PaintFillSpec &stroke,
    const PaintFillSpec &stroke2,
    const PaintFillSpec &shadow,
    int strokeWidth,
    int stroke2Width,
    int shadowOffsetX,
    int shadowOffsetY,
    int glowRadiusValue
) {
    QString glyphKey;
    for (std::size_t index = run.start; index < run.end; ++index) {
        if (index >= line.chars.size() || index >= layout.charLefts.size() || index >= layout.charWidths.size()) {
            continue;
        }
        glyphKey += QStringLiteral("|%1:%2@%3:%4:%5")
            .arg(index)
            .arg(line.chars[index].text)
            .arg(doubleCacheKey(layout.charLefts[index]))
            .arg(doubleCacheKey(layout.charWidths[index]))
            .arg(fontCacheKey(layoutCharFont(layout, index)));
    }
    return QStringLiteral("glyph_run|%1|glyphs=%2|x=%3|y=%4|w=%5|h=%6|style=%7")
        .arg(phase)
        .arg(glyphKey)
        .arg(doubleCacheKey(rect.left()))
        .arg(doubleCacheKey(rect.top()))
        .arg(doubleCacheKey(rect.width()))
        .arg(doubleCacheKey(rect.height()))
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

void paintGlyphRunTextLayer(
    QPainter &painter,
    const TimingLine &line,
    const LineLayout &layout,
    const GlyphRunRef &run,
    const ResolvedStyle &style,
    bool after
) {
    const QPainterPath path = glyphRunPath(line, layout, run);
    const QRectF rect = glyphRunRect(line, layout, run);
    if (path.isEmpty() || rect.isEmpty()) {
        return;
    }
    const PaintFillSpec &fill = after ? style.afterFill : style.baseFill;
    const PaintFillSpec &stroke = after ? style.afterStrokeFill : style.beforeStrokeFill;
    const PaintFillSpec &stroke2 = after ? style.afterStroke2Fill : style.beforeStroke2Fill;
    const PaintFillSpec &shadow = after ? style.afterShadowFill : style.beforeShadowFill;
    const int glow = glowRadius(style, after);
    paintCachedTextLayerStackWithWidths(
        painter,
        glyphRunTextLayerCacheKey(
            line,
            layout,
            run,
            rect,
            after ? QStringLiteral("after") : QStringLiteral("before"),
            style,
            fill,
            stroke,
            stroke2,
            shadow,
            style.strokeWidthPx,
            style.stroke2WidthPx,
            style.shadowOffsetX,
            style.shadowOffsetY,
            glow
        ),
        path,
        rect,
        fill,
        stroke,
        stroke2,
        shadow,
        style,
        style.strokeWidthPx,
        style.stroke2WidthPx,
        style.shadowOffsetX,
        style.shadowOffsetY,
        glow
    );
}

}  // namespace

void paintGlyphRunTextLayers(
    QPainter &painter,
    const TimingLine &line,
    const LineLayout &layout,
    const ResolvedStyle &lineStyle,
    bool after
) {
    const auto runs = glyphRunsForLayout(line, layout, lineStyle);
    for (const GlyphRunRef &run : runs) {
        if (run.start >= run.end) {
            continue;
        }
        const ResolvedStyle &style = layoutCharStyle(layout, lineStyle, run.start);
        paintGlyphRunTextLayer(painter, line, layout, run, style, after);
    }
}

}  // namespace krok::subtitle::native::legacy_qt
