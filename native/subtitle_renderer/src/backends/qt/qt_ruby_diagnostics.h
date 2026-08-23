#pragma once

#include "qt_render_types.h"

#include <vector>

namespace krok::subtitle::native::legacy_qt {

std::vector<RubyDiagnostics> rubyDiagnosticsForLine(
    const protocol::RenderConfig &cfg,
    const protocol::ResolvedStyle &style,
    const protocol::TimingLine &line,
    const LineLayout &layout,
    int tMs
);

}  // namespace krok::subtitle::native::legacy_qt
