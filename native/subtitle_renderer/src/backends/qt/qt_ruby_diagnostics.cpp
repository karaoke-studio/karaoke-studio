#include "qt_ruby_diagnostics.h"

#include "qt_character_animation.h"
#include "qt_font_factory.h"
#include "qt_ruby_layout.h"
#include "qt_ruby_target.h"
#include "qt_ruby_timing.h"
#include "qt_style_metrics.h"

#include <QtCore/QRectF>
#include <QtGui/QFontMetricsF>

#include <algorithm>

namespace krok::subtitle::native::legacy_qt {

using protocol::RenderConfig;
using protocol::ResolvedStyle;
using protocol::RubyAnnotation;
using protocol::TimingLine;

std::vector<RubyDiagnostics> rubyDiagnosticsForLine(
    const RenderConfig &cfg,
    const ResolvedStyle &style,
    const TimingLine &line,
    const LineLayout &layout,
    int tMs
) {
    std::vector<RubyDiagnostics> diagnostics;
    if (cfg.rubies.empty()) {
        return diagnostics;
    }
    const QFont rubyFont = buildRubyFont(style);
    const QFontMetricsF rubyMetrics(rubyFont);
    const auto intervals = lineIntervals(line);
    const double rubyBaselineY = layout.baselineY - layout.ascent - style.rubyGapPx;
    const double pad = rubyVisualPadding(style);

    for (const RubyAnnotation &ruby : cfg.rubies) {
        const auto indices = rubyTargetIndices(ruby, line, intervals);
        if (indices.empty()) {
            continue;
        }
        const auto targetRange = rubyTargetXRange(ruby, line, layout, intervals);
        if (!targetRange.has_value()) {
            continue;
        }
        const RubyAnnotation paintRuby = effectiveRubyForTarget(ruby, indices, intervals);
        const double x = targetRange->first;
        const double targetWidth = std::max(targetRange->second - targetRange->first, 1.0);
        const double readingWidth = rubyLayoutWidth(paintRuby.reading, rubyMetrics, targetWidth);
        const double ratio = rubyProgressRatio(paintRuby, tMs);
        const double ratioC = std::min(ratio, 1.0);
        const QRectF rect(x, rubyBaselineY - rubyMetrics.ascent(), readingWidth, rubyMetrics.height());
        const double clipLeft = cfg.rightToLeft
            ? rect.left() + rect.width() * (1.0 - ratioC) - pad
            : rect.left() - pad;
        const double clipWidth = rect.width() * ratioC + pad;

        RubyDiagnostics item;
        item.kanji = paintRuby.kanji;
        item.reading = paintRuby.reading;
        item.indices = indices;
        item.x = x;
        item.baselineY = rubyBaselineY;
        item.targetWidth = targetWidth;
        item.readingWidth = readingWidth;
        item.progress = ratio;
        item.afterClipLeft = clipLeft;
        item.afterClipRight = clipLeft + clipWidth;
        item.afterClipTop = rect.top() - pad;
        item.afterClipHeight = rect.height() + pad * 2.0;
        diagnostics.push_back(item);
    }
    return diagnostics;
}

}  // namespace krok::subtitle::native::legacy_qt
