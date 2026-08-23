#include "qt_ruby_layout.h"

#include "qt_ruby_timing.h"

#include <algorithm>

namespace krok::subtitle::native::legacy_qt {

double rubyLayoutWidth(const QString &reading, const QFontMetricsF &metrics, double targetWidth) {
    const double natural = metrics.horizontalAdvance(reading);
    if (targetWidth <= natural) {
        return natural;
    }
    return targetWidth;
}

QPainterPath rubyTextPath(
    const QString &reading,
    const QFont &font,
    const QFontMetricsF &metrics,
    double x,
    double baselineY,
    double targetWidth
) {
    const auto units = rubyReadingUnits(reading);
    std::vector<double> widths;
    widths.reserve(units.size());
    double natural = 0.0;
    for (const QString &unit : units) {
        const double width = metrics.horizontalAdvance(unit);
        widths.push_back(width);
        natural += width;
    }

    QPainterPath path;
    if (units.empty()) {
        return path;
    }
    if (units.size() <= 1 || targetWidth <= natural * 1.15) {
        double cursor = x + std::max((targetWidth - natural) / 2.0, 0.0);
        for (std::size_t i = 0; i < units.size(); ++i) {
            path.addText(QPointF(cursor, baselineY), font, units[i]);
            cursor += widths[i];
        }
        return path;
    }

    const double slotWidth = targetWidth / static_cast<double>(units.size());
    for (std::size_t i = 0; i < units.size(); ++i) {
        const double unitX = x + slotWidth * static_cast<double>(i) + (slotWidth - widths[i]) / 2.0;
        path.addText(QPointF(unitX, baselineY), font, units[i]);
    }
    return path;
}

std::vector<RubyUnitLayout> rubyUnitLayouts(
    const std::vector<std::pair<QString, std::pair<int, int>>> &unitsAndIntervals,
    const QFontMetricsF &metrics,
    double x,
    double targetWidth
) {
    std::vector<RubyUnitLayout> out;
    if (unitsAndIntervals.empty()) {
        return out;
    }
    std::vector<double> widths;
    widths.reserve(unitsAndIntervals.size());
    double natural = 0.0;
    for (const auto &item : unitsAndIntervals) {
        const double width = metrics.horizontalAdvance(item.first);
        widths.push_back(width);
        natural += width;
    }
    if (unitsAndIntervals.size() <= 1 || targetWidth <= natural * 1.15) {
        double cursor = x + std::max((targetWidth - natural) / 2.0, 0.0);
        for (std::size_t i = 0; i < unitsAndIntervals.size(); ++i) {
            out.push_back(RubyUnitLayout{unitsAndIntervals[i].first, unitsAndIntervals[i].second, cursor, widths[i]});
            cursor += widths[i];
        }
        return out;
    }

    const double slotWidth = targetWidth / static_cast<double>(unitsAndIntervals.size());
    for (std::size_t i = 0; i < unitsAndIntervals.size(); ++i) {
        const double unitX = x + slotWidth * static_cast<double>(i) + (slotWidth - widths[i]) / 2.0;
        out.push_back(RubyUnitLayout{unitsAndIntervals[i].first, unitsAndIntervals[i].second, unitX, widths[i]});
    }
    return out;
}

}  // namespace krok::subtitle::native::legacy_qt
