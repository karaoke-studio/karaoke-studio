#include "qt_utopia_painter.h"

#include "qt_character_animation.h"
#include "qt_font_factory.h"
#include "qt_ruby_layout.h"
#include "qt_ruby_target.h"
#include "qt_ruby_timing.h"
#include "qt_ruby_wipe.h"
#include "qt_transformed_text.h"

#include <QtCore/QPointF>
#include <QtCore/QRectF>
#include <QtGui/QFontMetricsF>
#include <QtGui/QPainterPath>
#include <QtGui/QTransform>

#include <algorithm>
#include <cmath>
#include <optional>

namespace krok::subtitle::native::legacy_qt {

using protocol::RenderConfig;
using protocol::ResolvedStyle;
using protocol::RubyAnnotation;
using protocol::TimingLine;

void paintRubyUtopiaText(
    QPainter &painter,
    const RenderConfig &cfg,
    const ResolvedStyle &style,
    const TimingLine &line,
    const LineLayout &layout,
    const std::vector<std::pair<int, int>> &intervals,
    const LineCharTransition &transition,
    int tMs
) {
    if (cfg.rubies.empty()) {
        return;
    }
    const QFont rubyFont = buildRubyFont(style);
    const QFontMetricsF rubyMetrics(rubyFont);
    const double rubyBaselineY = layout.baselineY - layout.ascent - style.rubyGapPx;
    const int count = std::max(static_cast<int>(line.chars.size()), 1);

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
        const int firstIndex = *std::min_element(indices.begin(), indices.end());
        const int lastIndex = *std::max_element(indices.begin(), indices.end());
        const int followingDoneMs = utopiaFollowingDoneTime(line, intervals, lastIndex, cfg);
        const AnimationState state = transitionCharState(
            cfg,
            transition,
            intervals,
            firstIndex,
            count,
            tMs,
            cfg.height,
            followingDoneMs
        );
        if (state.opacity <= 0.0) {
            continue;
        }

        const double x = targetRange->first;
        const double targetWidth = std::max(targetRange->second - targetRange->first, 1.0);
        const double readingWidth = rubyLayoutWidth(paintRuby.reading, rubyMetrics, targetWidth);
        const bool groupExiting = indices.size() > 1 && tMs > followingDoneMs;
        painter.save();
        painter.setOpacity(painter.opacity() * state.opacity);

        if (groupExiting) {
            QString reading = paintRuby.reading;
            if (cfg.rightToLeft) {
                const auto visual = rubyUtopiaVisualUnits(reading);
                reading.clear();
                for (auto it = visual.rbegin(); it != visual.rend(); ++it) {
                    reading += *it;
                }
            }
            QPainterPath uprightPath = rubyTextPath(reading, rubyFont, rubyMetrics, x, rubyBaselineY, targetWidth);
            const QRectF sourceRect(
                x,
                rubyBaselineY - rubyMetrics.ascent(),
                readingWidth,
                rubyMetrics.height()
            );
            const double centerX = x + readingWidth / 2.0;
            const double centerY = rubyBaselineY - rubyMetrics.ascent() + rubyMetrics.height() / 2.0;
            const QTransform transform = characterTransform(
                centerX,
                centerY,
                state,
                QPointF(x, rubyBaselineY)
            );
            QPainterPath path = transform.map(uprightPath);
            const QRectF rect = path.boundingRect();
            if (!rect.isEmpty()) {
                paintRubyTransformedStack(
                    painter,
                    path,
                    rect,
                    style,
                    rubyProgressRatio(paintRuby, tMs),
                    cfg.rightToLeft,
                    true,
                    &uprightPath,
                    &sourceRect,
                    &transform,
                    QStringLiteral("ruby_utopia_group")
                );
            }
            painter.restore();
            continue;
        }

        auto unitsAndIntervals = rubyUtopiaReadingUnitsAndIntervals(paintRuby);
        if (cfg.rightToLeft) {
            std::reverse(unitsAndIntervals.begin(), unitsAndIntervals.end());
        }
        const auto layouts = rubyUnitLayouts(unitsAndIntervals, rubyMetrics, x, targetWidth);
        for (const RubyUnitLayout &unit : layouts) {
            const AnimationState unitState = transitionCharState(
                cfg,
                transition,
                intervals,
                firstIndex,
                count,
                tMs,
                cfg.height,
                followingDoneMs,
                unit.interval
            );
            if (unitState.opacity <= 0.0) {
                continue;
            }
            QPainterPath uprightPath;
            uprightPath.addText(QPointF(unit.x, rubyBaselineY), rubyFont, unit.text);
            const QRectF sourceRect(
                unit.x,
                rubyBaselineY - rubyMetrics.ascent(),
                unit.width,
                rubyMetrics.height()
            );
            const double centerX = unit.x + unit.width / 2.0;
            const double centerY = rubyBaselineY - rubyMetrics.ascent() + rubyMetrics.height() / 2.0;
            const QTransform transform = characterTransform(
                centerX,
                centerY,
                unitState,
                QPointF(unit.x, rubyBaselineY)
            );
            QPainterPath path = transform.map(uprightPath);
            const QRectF rect = path.boundingRect();
            if (rect.isEmpty()) {
                continue;
            }
            painter.save();
            painter.setOpacity(painter.opacity() * unitState.opacity);
            paintRubyTransformedStack(
                painter,
                path,
                rect,
                style,
                progressRatio(unit.interval.first, unit.interval.second, tMs),
                cfg.rightToLeft,
                false,
                &uprightPath,
                &sourceRect,
                &transform,
                QStringLiteral("ruby_utopia_reading")
            );
            painter.restore();
        }
        painter.restore();
    }
}

void paintUtopiaMainText(
    QPainter &painter,
    const RenderConfig &cfg,
    const TimingLine &line,
    const ResolvedStyle &style,
    const LineLayout &layout,
    const std::vector<std::pair<int, int>> &intervals,
    const LineCharTransition &transition,
    int tMs
) {
    const QFontMetricsF metrics(layout.font);
    const int count = std::max(static_cast<int>(line.chars.size()), 1);
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        if (i >= layout.charLefts.size() || i >= layout.charWidths.size()) {
            continue;
        }

        std::vector<int> indices{static_cast<int>(i)};
        std::optional<RubyAnnotation> groupRuby;
        const auto group = rubyGroupForCharIndex(cfg, line, intervals, static_cast<int>(i));
        bool groupExiting = false;
        if (group.has_value()) {
            const int groupDoneMs = utopiaFollowingDoneTime(line, intervals, group->indices.back(), cfg);
            groupExiting = tMs > groupDoneMs;
            if (groupExiting && static_cast<int>(i) != group->indices.front()) {
                continue;
            }
            if (groupExiting) {
                indices = group->indices;
                groupRuby = group->ruby;
            }
        }

        const int firstIndex = indices.front();
        const int lastIndex = indices.back();
        const int followingDoneMs = utopiaFollowingDoneTime(line, intervals, lastIndex, cfg);
        const auto wipeWindow = utopiaWipeWindowForIndex(
            line,
            layout,
            firstIndex,
            style,
            group,
            intervals[static_cast<std::size_t>(firstIndex)]
        );
        const AnimationState state = transitionCharState(
            cfg,
            transition,
            intervals,
            firstIndex,
            count,
            tMs,
            cfg.height,
            followingDoneMs,
            wipeWindow
        );
        if (state.opacity <= 0.0) {
            continue;
        }

        QPainterPath path;
        double left = layout.charLefts[static_cast<std::size_t>(firstIndex)];
        double right = left + layout.charWidths[static_cast<std::size_t>(firstIndex)];
        for (int index : indices) {
            if (index < 0 || static_cast<std::size_t>(index) >= line.chars.size()) {
                continue;
            }
            const std::size_t pos = static_cast<std::size_t>(index);
            if (pos >= layout.charLefts.size() || pos >= layout.charWidths.size()) {
                continue;
            }
            path.addText(QPointF(layout.charLefts[pos], layout.baselineY), layout.font, line.chars[pos].text);
            left = std::min(left, layout.charLefts[pos]);
            right = std::max(right, layout.charLefts[pos] + layout.charWidths[pos]);
        }
        const double width = std::max(right - left, 1.0);
        const QRectF sourceRect(left, layout.baselineY - metrics.ascent(), width, metrics.height());
        const double centerX = left + width / 2.0;
        const double centerY = layout.baselineY - metrics.ascent() + metrics.height() / 2.0;
        const QTransform transform = characterTransform(
            centerX,
            centerY,
            state,
            QPointF(left, layout.baselineY)
        );
        const QPainterPath paintPath = transform.map(path);
        const QRectF paintRect = paintPath.boundingRect();
        if (paintRect.isEmpty()) {
            continue;
        }
        const int paintLeft = static_cast<int>(std::round(paintRect.left()));
        const int paintWidth = std::max(1, static_cast<int>(std::round(paintRect.width())));
        const bool inUtopiaExit = cfg.exitAnim == QStringLiteral("utopia") && tMs > followingDoneMs;
        const double ratio = groupRuby.has_value()
            ? rubyProgressRatio(groupRuby.value(), tMs)
            : characterFillRatio(intervals, i, tMs);

        painter.save();
        painter.setOpacity(painter.opacity() * state.opacity);
        paintTransformedTextStack(
            painter,
            paintPath,
            paintRect,
            style,
            ratio,
            cfg.rightToLeft,
            paintLeft,
            paintWidth,
            inUtopiaExit,
            &path,
            &sourceRect,
            &transform,
            groupRuby.has_value() ? QStringLiteral("main_utopia_ruby_group") : QStringLiteral("main_utopia_char")
        );
        painter.restore();
    }
}

}  // namespace krok::subtitle::native::legacy_qt
