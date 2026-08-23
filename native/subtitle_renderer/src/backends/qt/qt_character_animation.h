#pragma once

#include "qt_render_types.h"

#include <QtCore/QPointF>
#include <QtGui/QTransform>

#include <cstddef>
#include <optional>
#include <utility>
#include <vector>

namespace krok::subtitle::native::legacy_qt {

int charEndMs(const protocol::TimingLine &line, std::size_t index);
double progressRatio(int startMs, int endMs, int tMs);
int utopiaFollowingDoneTime(
    const protocol::TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals,
    int index,
    const protocol::RenderConfig &cfg
);
std::optional<LineCharTransition> lineCharTransitionContext(
    const protocol::RenderConfig &cfg,
    const protocol::TimingLine &line,
    int tMs,
    const std::vector<std::pair<int, int>> &intervals
);
QTransform characterTransform(
    double centerX,
    double centerY,
    const AnimationState &state,
    std::optional<QPointF> scaleOrigin = std::nullopt
);
AnimationState transitionCharState(
    const protocol::RenderConfig &cfg,
    const LineCharTransition &transition,
    const std::vector<std::pair<int, int>> &intervals,
    int index,
    int count,
    int tMs,
    int frameHeight,
    int followingDoneMs,
    std::optional<std::pair<int, int>> overrideInterval = std::nullopt
);

}  // namespace krok::subtitle::native::legacy_qt
