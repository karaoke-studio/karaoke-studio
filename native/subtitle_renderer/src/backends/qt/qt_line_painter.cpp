#include "qt_line_painter.h"

#include "qt_cached_line_layout.h"
#include "qt_character_animation.h"
#include "qt_clip_geometry.h"
#include "qt_display_plan.h"
#include "qt_glyph_run.h"
#include "qt_line_layout.h"
#include "qt_ruby_diagnostics.h"
#include "qt_ruby_painter.h"
#include "qt_style_metrics.h"
#include "qt_utopia_painter.h"
#include "../../protocol/render_config_parser.h"

#include <QtCore/QRectF>
#include <QtGui/QRegion>

namespace krok::subtitle::native::legacy_qt {

using protocol::RenderConfig;
using protocol::ResolvedStyle;
using protocol::TimingLine;
using protocol::resolvedStyleForLine;

void paintLine(QPainter &painter, const RenderConfig &cfg, const TimingLine &line, int tMs, int lane, int visibleLineCount, RenderDiagnostics *diagnostics) {
    const QString text = lineText(line);
    if (text.isEmpty()) {
        return;
    }

    const ResolvedStyle &lineStyle = resolvedStyleForLine(cfg, line);

    const LineLayout layout = cachedLayoutLine(cfg, lineStyle, line, lane, visibleLineCount);

    const QRectF lineRect(layout.x, layout.baselineY - layout.ascent, layout.width, layout.height);
    const auto intervals = lineIntervals(line);
    const auto transition = lineCharTransitionContext(cfg, line, tMs, intervals);
    const auto rubyDiagnostics = rubyDiagnosticsForLine(cfg, lineStyle, line, layout, tMs);
    const bool useUtopiaMainText = transition.has_value()
        && transition->effect == QStringLiteral("utopia")
        && !layout.hasInlineStyles;

    if (useUtopiaMainText) {
        paintRubyUtopiaText(
            painter,
            cfg,
            lineStyle,
            line,
            layout,
            intervals,
            transition.value(),
            tMs
        );
    } else {
        paintRubyDiagnostics(
            painter,
            lineStyle,
            rubyDiagnostics,
            lineStyle.rubyBaseFill,
            lineStyle.rubyAfterFill,
            lineStyle.rubyBeforeStrokeFill,
            lineStyle.rubyAfterStrokeFill,
            lineStyle.rubyBeforeStroke2Fill,
            lineStyle.rubyAfterStroke2Fill,
            lineStyle.rubyBeforeShadowFill,
            lineStyle.rubyAfterShadowFill
        );
    }

    if (useUtopiaMainText) {
        paintUtopiaMainText(
            painter,
            cfg,
            line,
            lineStyle,
            layout,
            intervals,
            transition.value(),
            tMs
        );
    } else {
        paintGlyphRunTextLayers(painter, line, layout, lineStyle, false);
    }

    const auto clip = afterClipRect(cfg, lineStyle, line, layout, tMs);
    if (!useUtopiaMainText && clip.has_value() && clip->width() > 0.0) {
        // One band -> the original rectangle, byte for byte.  Two bands only
        // happen when the source really has two wipes running at once.
        const QRegion region = afterClipRegion(cfg, lineStyle, line, layout, tMs);
        painter.save();
        if (region.rectCount() > 1) {
            painter.setClipRegion(region, Qt::IntersectClip);
        } else {
            painter.setClipRect(*clip, Qt::IntersectClip);
        }
        paintGlyphRunTextLayers(painter, line, layout, lineStyle, true);
        painter.restore();
    }

    if (diagnostics != nullptr) {
        LineDiagnostics lineDiagnostics;
        lineDiagnostics.lane = lane;
        lineDiagnostics.lineX = layout.x;
        lineDiagnostics.lineWidth = layout.width;
        lineDiagnostics.baselineY = layout.baselineY;
        if (clip.has_value()) {
            lineDiagnostics.afterClipLeft = clip->left();
            lineDiagnostics.afterClipRight = clip->right();
            lineDiagnostics.afterClipTop = clip->top();
            lineDiagnostics.afterClipHeight = clip->height();
        } else {
            lineDiagnostics.afterClipLeft = layout.x;
            lineDiagnostics.afterClipRight = layout.x;
            const double verticalExtent = layout.afterClipExtent > 0.0 ? layout.afterClipExtent : afterClipVerticalExtent(lineStyle);
            lineDiagnostics.afterClipTop = layout.baselineY - layout.ascent - verticalExtent;
            lineDiagnostics.afterClipHeight = layout.height + verticalExtent * 2.0;
        }
        diagnostics->lines.push_back(lineDiagnostics);
        if (!diagnostics->hasFirstLine) {
            diagnostics->hasFirstLine = true;
            diagnostics->lineX = lineDiagnostics.lineX;
            diagnostics->lineWidth = lineDiagnostics.lineWidth;
            diagnostics->baselineY = lineDiagnostics.baselineY;
            diagnostics->afterClipLeft = lineDiagnostics.afterClipLeft;
            diagnostics->afterClipRight = lineDiagnostics.afterClipRight;
            diagnostics->afterClipTop = lineDiagnostics.afterClipTop;
            diagnostics->afterClipHeight = lineDiagnostics.afterClipHeight;
        }
        diagnostics->rubies.insert(
            diagnostics->rubies.end(),
            rubyDiagnostics.begin(),
            rubyDiagnostics.end()
        );
    }
}

}  // namespace krok::subtitle::native::legacy_qt
