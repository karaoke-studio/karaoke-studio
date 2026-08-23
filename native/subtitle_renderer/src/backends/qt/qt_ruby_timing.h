#pragma once

#include "../../protocol/render_config.h"

#include <QtCore/QString>

#include <utility>
#include <vector>

namespace krok::subtitle::native::legacy_qt {

using RubyTimedUnit = std::pair<QString, std::pair<int, int>>;

std::vector<QString> rubyReadingUnits(const QString &reading);
std::vector<QString> rubyUtopiaVisualUnits(const QString &text);
std::vector<RubyTimedUnit> rubyUtopiaReadingUnitsAndIntervals(
    const protocol::RubyAnnotation &ruby
);
double rubyProgressRatio(const protocol::RubyAnnotation &ruby, int tMs);

}  // namespace krok::subtitle::native::legacy_qt
