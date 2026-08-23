#include "qt_line_layout.h"

#include "qt_display_plan.h"
#include "qt_font_factory.h"
#include "qt_style_metrics.h"
#include "../../protocol/render_config_parser.h"

#include <QtCore/QPointF>
#include <QtGui/QFontMetricsF>

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::legacy_qt {

using protocol::RenderConfig;
using protocol::ResolvedStyle;
using protocol::TimingLine;
using protocol::resolvedStyleForCharacter;

double baselineYForLine(const RenderConfig &cfg, const ResolvedStyle &style, const QFontMetricsF &metrics, int lane, int visibleLineCount) {
    const double pad = visualStrokeExtent(style);
    if (cfg.dualLineLayout) {
        const double mainHeight = metrics.ascent() + metrics.descent() + pad * 2.0;
        const double mainAscent = metrics.ascent() + pad;
        const double mainDescent = metrics.descent() + pad;
        double upperBaseline = 0.0;
        double lowerBaseline = 0.0;
        if (cfg.lineYPosition == QStringLiteral("top")) {
            upperBaseline = cfg.lineYMarginPx + mainAscent;
            lowerBaseline = upperBaseline + mainHeight + cfg.lineGapPx;
        } else if (cfg.lineYPosition == QStringLiteral("center")) {
            const double totalHeight = mainHeight * 2.0 + cfg.lineGapPx;
            const double upperMainTop = std::floor((cfg.height - totalHeight) / 2.0);
            upperBaseline = upperMainTop + mainAscent;
            lowerBaseline = upperBaseline + mainHeight + cfg.lineGapPx;
        } else {
            lowerBaseline = cfg.height - cfg.lineYMarginPx - mainDescent;
            upperBaseline = lowerBaseline - mainHeight - cfg.lineGapPx;
        }
        return (cfg.dualLineLayout && std::min(lane, 1) == 1) ? lowerBaseline : upperBaseline;
    }

    if (cfg.lineYPosition == QStringLiteral("top")) {
        return cfg.lineYMarginPx + pad + metrics.ascent();
    }
    if (cfg.lineYPosition == QStringLiteral("center")) {
        const double blockHeight = metrics.height() + pad * 2.0;
        return std::floor((cfg.height - blockHeight) / 2.0) + pad + metrics.ascent();
    }
    return cfg.height - cfg.lineYMarginPx - pad - metrics.descent();
}

double lineXForLine(const RenderConfig &cfg, double lineWidth, double pad, int lane) {
    if (cfg.lineHorizontalLayout == QStringLiteral("center")) {
        return (cfg.width - lineWidth) * 0.5;
    }
    if (cfg.dualLineLayout && std::min(lane, 1) == 0) {
        return cfg.upperLineLeftMarginPx + pad;
    }
    if (cfg.dualLineLayout && std::min(lane, 1) == 1) {
        return cfg.width - cfg.lowerLineRightMarginPx - lineWidth - pad;
    }
    return (cfg.width - lineWidth) * 0.5;
}

LineLayout layoutLine(const RenderConfig &cfg, const ResolvedStyle &lineStyle, const TimingLine &line, int lane, int visibleLineCount) {
    const QString text = lineText(line);
    const bool inlineStyles = lineHasRoleLabels(line);

    LineLayout layout;
    layout.text = text;
    layout.font = buildLineFont(lineStyle);
    layout.lineStyle = &lineStyle;
    layout.hasInlineStyles = inlineStyles;

    const QFontMetricsF metrics(layout.font);

    layout.charWidths.reserve(line.chars.size());
    layout.charFonts.reserve(line.chars.size());
    layout.charStyles.reserve(line.chars.size());
    double totalWidth = 0.0;
    double maxAscent = 0.0;
    double maxDescent = 0.0;
    double maxVisualPad = visualStrokeExtent(lineStyle);
    double maxAfterClipExtent = afterClipVerticalExtent(lineStyle);
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        const auto &ch = line.chars[i];
        const ResolvedStyle &charStyle = inlineStyles ? resolvedStyleForCharacter(cfg, line, ch) : lineStyle;
        QFont charFont = isEmojiText(ch.text)
            ? buildEmojiFont(charStyle)
            : (inlineStyles ? buildLineFont(charStyle) : layout.font);
        const QFontMetricsF charMetrics(charFont);
        const double width = std::max(1.0, charMetrics.horizontalAdvance(ch.text));
        layout.charWidths.push_back(width);
        layout.charFonts.push_back(charFont);
        layout.charStyles.push_back(&charStyle);
        totalWidth += width;
        maxAscent = std::max(maxAscent, charMetrics.ascent());
        maxDescent = std::max(maxDescent, charMetrics.descent());
        maxVisualPad = std::max(maxVisualPad, visualStrokeExtent(charStyle));
        maxAfterClipExtent = std::max(maxAfterClipExtent, afterClipVerticalExtent(charStyle));
        if (i + 1 < line.chars.size()) {
            totalWidth += charStyle.letterSpacingPx;
        }
    }
    layout.ascent = inlineStyles ? maxAscent : metrics.ascent();
    layout.descent = inlineStyles ? maxDescent : metrics.descent();
    layout.height = layout.ascent + layout.descent;
    layout.afterClipExtent = maxAfterClipExtent;
    layout.width = std::max(1.0, totalWidth);
    const double visualPad = inlineStyles ? maxVisualPad : visualStrokeExtent(lineStyle);
    layout.x = lineXForLine(cfg, layout.width, visualPad, lane);

    layout.baselineY = baselineYForLine(cfg, lineStyle, metrics, lane, visibleLineCount);

    layout.charLefts.resize(line.chars.size());
    if (cfg.rightToLeft) {
        double cursor = layout.x + layout.width;
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            cursor -= layout.charWidths[i];
            layout.charLefts[i] = cursor;
            cursor -= (i + 1 < layout.charStyles.size()) ? layout.charStyles[i]->letterSpacingPx : 0;
        }
    } else {
        double cursor = layout.x;
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            layout.charLefts[i] = cursor;
            cursor += layout.charWidths[i] + ((i + 1 < layout.charStyles.size()) ? layout.charStyles[i]->letterSpacingPx : 0);
        }
    }

    // C2 keeps one complete line path for both before/after layers. Karaoke
    // progress is expressed only by clipping the after layer, not by rebuilding
    // a prefix string path that can drift under kerning/shaping.
    if (inlineStyles) {
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            layout.path.addText(QPointF(layout.charLefts[i], layout.baselineY), layout.charFonts[i], line.chars[i].text);
        }
    } else {
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            layout.path.addText(QPointF(layout.charLefts[i], layout.baselineY), layout.charFonts[i], line.chars[i].text);
        }
    }
    return layout;
}

}  // namespace krok::subtitle::native::legacy_qt
