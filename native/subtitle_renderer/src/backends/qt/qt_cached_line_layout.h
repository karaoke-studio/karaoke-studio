#pragma once

#include "qt_render_types.h"

namespace krok::subtitle::native::legacy_qt {

LineLayout cachedLayoutLine(
    const protocol::RenderConfig &cfg,
    const protocol::ResolvedStyle &lineStyle,
    const protocol::TimingLine &line,
    int lane,
    int visibleLineCount
);

}  // namespace krok::subtitle::native::legacy_qt
