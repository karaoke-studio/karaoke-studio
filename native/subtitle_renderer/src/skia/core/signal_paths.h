#pragma once

#include "include/core/SkPath.h"
#include <optional>
#include <string_view>

namespace subtitle::skia {
struct SignalPath {
    SkPath body;
    SkPoint highlightCenter;
    float highlightRadius;
};

std::optional<SignalPath> signalPath(std::string_view shape, float size);
}
