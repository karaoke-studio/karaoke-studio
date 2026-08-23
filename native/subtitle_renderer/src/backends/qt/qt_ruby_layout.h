#pragma once

#include "qt_render_types.h"
#include "qt_ruby_timing.h"

#include <QtCore/QString>
#include <QtGui/QFont>
#include <QtGui/QFontMetricsF>
#include <QtGui/QPainterPath>

#include <vector>

namespace krok::subtitle::native::legacy_qt {

double rubyLayoutWidth(
    const QString &reading,
    const QFontMetricsF &metrics,
    double targetWidth
);
QPainterPath rubyTextPath(
    const QString &reading,
    const QFont &font,
    const QFontMetricsF &metrics,
    double x,
    double baselineY,
    double targetWidth
);
std::vector<RubyUnitLayout> rubyUnitLayouts(
    const std::vector<RubyTimedUnit> &unitsAndIntervals,
    const QFontMetricsF &metrics,
    double x,
    double targetWidth
);

}  // namespace krok::subtitle::native::legacy_qt
