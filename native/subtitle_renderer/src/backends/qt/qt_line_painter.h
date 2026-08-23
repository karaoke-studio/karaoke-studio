#pragma once

#include "qt_render_types.h"

#include <QtGui/QPainter>

namespace krok::subtitle::native::legacy_qt {

void paintLine(
    QPainter &painter,
    const protocol::RenderConfig &cfg,
    const protocol::TimingLine &line,
    int tMs,
    int lane,
    int visibleLineCount,
    RenderDiagnostics *diagnostics
);

}  // namespace krok::subtitle::native::legacy_qt
