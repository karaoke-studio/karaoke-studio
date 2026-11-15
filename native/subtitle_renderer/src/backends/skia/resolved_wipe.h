#pragma once
#include <optional>
#include "../../model/wipe_point.h"
#include <QtCore/QJsonObject>
#include <cstddef>
#include <vector>

namespace krok::subtitle::native {
std::optional<float> resolvedSkiaWipeEnd(const QJsonObject& cell, const QJsonObject& ir);
std::vector<WipePoint> resolvedSkiaWipePoints(const QJsonObject& cell, const QJsonObject& ir,
                                            int timingOffset, std::size_t& totalPoints);
}
