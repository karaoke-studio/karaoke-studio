#include "qt_inline_text.h"

#include "qt_style_metrics.h"
#include "qt_text_layer.h"

#include <QtCore/QPointF>
#include <QtCore/QRectF>
#include <QtGui/QFontMetricsF>
#include <QtGui/QPainterPath>

namespace krok::subtitle::native::legacy_qt {

using protocol::ResolvedStyle;
using protocol::TimingLine;

void paintInlineTextLayerStack(
    QPainter &painter,
    const TimingLine &line,
    const LineLayout &layout,
    bool after
) {
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        if (i >= layout.charLefts.size() || i >= layout.charWidths.size() || i >= layout.charFonts.size() || i >= layout.charStyles.size()) {
            continue;
        }
        const ResolvedStyle &style = *layout.charStyles[i];
        const QFontMetricsF metrics(layout.charFonts[i]);
        QPainterPath path;
        path.addText(QPointF(layout.charLefts[i], layout.baselineY), layout.charFonts[i], line.chars[i].text);
        const QRectF rect(
            layout.charLefts[i],
            layout.baselineY - metrics.ascent(),
            layout.charWidths[i],
            metrics.height()
        );
        paintTextLayerStackWithWidths(
            painter,
            path,
            rect,
            after ? style.afterFill : style.baseFill,
            after ? style.afterStrokeFill : style.beforeStrokeFill,
            after ? style.afterStroke2Fill : style.beforeStroke2Fill,
            after ? style.afterShadowFill : style.beforeShadowFill,
            style,
            style.strokeWidthPx,
            style.stroke2WidthPx,
            style.shadowOffsetX,
            style.shadowOffsetY,
            glowRadius(style, after)
        );
    }
}

}  // namespace krok::subtitle::native::legacy_qt
