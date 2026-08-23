#pragma once

#include "qt_render_types.h"

#include <QtCore/QRectF>
#include <QtGui/QRegion>

#include <optional>
#include <utility>
#include <vector>

namespace krok::subtitle::native::legacy_qt {

std::vector<std::pair<double, double>> afterClipBandsFromCharacterTiming(
    const protocol::RenderConfig &cfg,
    const protocol::TimingLine &line,
    const LineLayout &layout,
    int tMs
);
std::vector<std::pair<double, double>> mergeBands(
    std::vector<std::pair<double, double>> bands
);
QRegion bandsToRegion(
    const std::vector<std::pair<double, double>> &bands,
    double top,
    double height
);
std::optional<QRectF> afterClipRectFromCharacterTiming(
    const protocol::RenderConfig &cfg,
    const protocol::ResolvedStyle &style,
    const protocol::TimingLine &line,
    const LineLayout &layout,
    int tMs
);

}  // namespace krok::subtitle::native::legacy_qt
