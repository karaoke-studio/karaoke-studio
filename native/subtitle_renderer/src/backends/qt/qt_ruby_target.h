#pragma once

#include "qt_render_types.h"

#include <optional>
#include <utility>
#include <vector>

namespace krok::subtitle::native::legacy_qt {

std::vector<int> rubyTargetIndices(
    const protocol::RubyAnnotation &ruby,
    const protocol::TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals
);
protocol::RubyAnnotation effectiveRubyForTarget(
    const protocol::RubyAnnotation &ruby,
    const std::vector<int> &indices,
    const std::vector<std::pair<int, int>> &intervals
);
std::optional<std::pair<double, double>> rubyTargetXRange(
    const protocol::RubyAnnotation &ruby,
    const protocol::TimingLine &line,
    const LineLayout &layout,
    const std::vector<std::pair<int, int>> &intervals
);
std::optional<RubyGroupInfo> rubyGroupForCharIndex(
    const protocol::RenderConfig &cfg,
    const protocol::TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals,
    int index
);

}  // namespace krok::subtitle::native::legacy_qt
