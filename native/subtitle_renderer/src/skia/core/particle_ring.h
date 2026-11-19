#pragma once

#include "include/core/SkColor.h"
#include "include/core/SkPoint.h"
#include <optional>

class SkCanvas;
class SkPath;

namespace subtitle::skia {
struct RingStroke {
    float radius, width;
};

std::optional<RingStroke> concentricRing(const SkPath& path);
void drawParticleRing(SkCanvas&, SkPoint center, RingStroke, float scale, SkColor4f color);
}
