#pragma once

#include "include/core/SkPath.h"
#include <cstdint>

class SkCanvas;
class SkPaint;

namespace subtitle::skia {
struct SolidOutline {
    SkPath path;
    std::uint32_t before, after;
    bool protectBody;
    float width = 0;
};

SkPath prepareOutlinePath(const SkPath& body, float width);
void drawFixedOutline(SkCanvas&, const SkPath&, const SkPaint&, float width);
void drawSupersampledSolidPath(SkCanvas&, const SkPath&, const SkPaint& fill, const SkPaint& composite);
}
