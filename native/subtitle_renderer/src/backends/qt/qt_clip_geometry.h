#pragma once

#include "qt_render_types.h"

#include <QtCore/QRectF>
#include <QtGui/QRegion>

#include <optional>

namespace krok::subtitle::native::legacy_qt {

QRegion afterClipRegion(
    const protocol::RenderConfig &cfg,
    const protocol::ResolvedStyle &style,
    const protocol::TimingLine &line,
    const LineLayout &layout,
    int tMs
);
std::optional<QRectF> afterClipRect(
    const protocol::RenderConfig &cfg,
    const protocol::ResolvedStyle &style,
    const protocol::TimingLine &line,
    const LineLayout &layout,
    int tMs
);

}  // namespace krok::subtitle::native::legacy_qt
