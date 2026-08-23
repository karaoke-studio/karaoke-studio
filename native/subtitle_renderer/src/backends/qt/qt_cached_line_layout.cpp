#include "qt_cached_line_layout.h"

#include "qt_display_plan.h"
#include "qt_font_factory.h"
#include "qt_line_layout.h"
#include "qt_render_cache.h"
#include "qt_style_metrics.h"

namespace krok::subtitle::native::legacy_qt {

using protocol::RenderConfig;
using protocol::ResolvedStyle;
using protocol::TimingChar;
using protocol::TimingLine;

QString timingLineLayoutTextKey(const TimingLine &line) {
    QString key;
    for (const TimingChar &ch : line.chars) {
        key += QStringLiteral("|%1:%2").arg(ch.text.size()).arg(ch.text);
    }
    return key;
}

QString layoutLineCacheKey(
    const RenderConfig &cfg,
    const ResolvedStyle &lineStyle,
    const TimingLine &line,
    int lane,
    int visibleLineCount
) {
    const QFont font = buildLineFont(lineStyle);
    return QStringLiteral("layout|text=%1|font=%2|w=%3|h=%4|lane=%5|visible=%6|rtl=%7|y=%8|h_layout=%9|dual=%10|ym=%11|gap=%12|upper=%13|lower=%14|sw=%15|s2w=%16|deco=%17|sx=%18|sy=%19|glow=%20|spacing=%21")
        .arg(timingLineLayoutTextKey(line))
        .arg(fontCacheKey(font))
        .arg(cfg.width)
        .arg(cfg.height)
        .arg(lane)
        .arg(visibleLineCount)
        .arg(cfg.rightToLeft ? 1 : 0)
        .arg(cfg.lineYPosition)
        .arg(cfg.lineHorizontalLayout)
        .arg(cfg.dualLineLayout ? 1 : 0)
        .arg(cfg.lineYMarginPx)
        .arg(cfg.lineGapPx)
        .arg(cfg.upperLineLeftMarginPx)
        .arg(cfg.lowerLineRightMarginPx)
        .arg(lineStyle.strokeWidthPx)
        .arg(lineStyle.stroke2WidthPx)
        .arg(lineStyle.decorationKind)
        .arg(lineStyle.shadowOffsetX)
        .arg(lineStyle.shadowOffsetY)
        .arg(glowRadius(lineStyle, false))
        .arg(lineStyle.letterSpacingPx);
}

LineLayout cachedLayoutLine(
    const RenderConfig &cfg,
    const ResolvedStyle &lineStyle,
    const TimingLine &line,
    int lane,
    int visibleLineCount
) {
    if (lineHasRoleLabels(line)) {
        return layoutLine(cfg, lineStyle, line, lane, visibleLineCount);
    }
    const QString key = layoutLineCacheKey(cfg, lineStyle, line, lane, visibleLineCount);
    if (const auto cached = lookupLayoutCache(key)) {
        return *cached;
    }
    LineLayout layout = layoutLine(cfg, lineStyle, line, lane, visibleLineCount);
    layout.lineStyle = nullptr;
    layout.charStyles.clear();
    storeLayoutCache(key, layout);
    return layout;
}

// One horizontal band per character that has started, in layout order.
//
// A character whose wipe runs past the next character's start leaves two fronts
// alive at once (SUG writes this where a lead phrase's release timestamp crosses
// into the following harmony phrase).  Stopping at the first unfinished glyph --
// which is what this used to do -- can only ever show the first of them.  With
// sequential timing the bands are contiguous and their union is exactly the
// single rect this produced before.

}  // namespace krok::subtitle::native::legacy_qt
