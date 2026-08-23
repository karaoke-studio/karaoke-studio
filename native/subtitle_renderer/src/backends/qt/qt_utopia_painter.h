#pragma once

#include "qt_render_types.h"

#include <QtGui/QPainter>

#include <utility>
#include <vector>

namespace krok::subtitle::native::legacy_qt {

void paintRubyUtopiaText(
    QPainter &painter,
    const protocol::RenderConfig &cfg,
    const protocol::ResolvedStyle &style,
    const protocol::TimingLine &line,
    const LineLayout &layout,
    const std::vector<std::pair<int, int>> &intervals,
    const LineCharTransition &transition,
    int tMs
);
void paintUtopiaMainText(
    QPainter &painter,
    const protocol::RenderConfig &cfg,
    const protocol::TimingLine &line,
    const protocol::ResolvedStyle &style,
    const LineLayout &layout,
    const std::vector<std::pair<int, int>> &intervals,
    const LineCharTransition &transition,
    int tMs
);

}  // namespace krok::subtitle::native::legacy_qt
