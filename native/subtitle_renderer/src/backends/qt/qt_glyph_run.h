#pragma once

#include "qt_render_types.h"

#include <QtGui/QPainter>

namespace krok::subtitle::native::legacy_qt {

void paintGlyphRunTextLayers(
    QPainter &painter,
    const protocol::TimingLine &line,
    const LineLayout &layout,
    const protocol::ResolvedStyle &lineStyle,
    bool after
);

}  // namespace krok::subtitle::native::legacy_qt
