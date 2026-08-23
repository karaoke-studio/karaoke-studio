#pragma once

#include "qt_render_types.h"

#include <QtGui/QPainter>

#include <vector>

namespace krok::subtitle::native::legacy_qt {

void paintRubyDiagnostics(
    QPainter &painter,
    const protocol::ResolvedStyle &style,
    const std::vector<RubyDiagnostics> &rubies,
    const protocol::PaintFillSpec &base,
    const protocol::PaintFillSpec &fill,
    const protocol::PaintFillSpec &beforeStroke,
    const protocol::PaintFillSpec &afterStroke,
    const protocol::PaintFillSpec &beforeStroke2,
    const protocol::PaintFillSpec &afterStroke2,
    const protocol::PaintFillSpec &beforeShadow,
    const protocol::PaintFillSpec &afterShadow
);

}  // namespace krok::subtitle::native::legacy_qt
