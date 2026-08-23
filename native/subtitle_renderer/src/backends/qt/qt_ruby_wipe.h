#pragma once

#include "../render_backend.h"
#include "qt_render_types.h"

#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace krok::subtitle::native::legacy_qt {

std::pair<int, int> utopiaWipeWindowForIndex(
    const protocol::TimingLine &line,
    const LineLayout &layout,
    int index,
    const protocol::ResolvedStyle &style,
    const std::optional<RubyGroupInfo> &group,
    std::pair<int, int> fallback
);
bool applyRubyMainWipeProjection(
    krok::subtitle::native::TextLine &line,
    const protocol::TimingLine &sourceLine,
    const protocol::RubyAnnotation &ruby,
    const std::vector<int> &targetIndices,
    const std::string &progressMode,
    int timingOffsetMs
);

}  // namespace krok::subtitle::native::legacy_qt
