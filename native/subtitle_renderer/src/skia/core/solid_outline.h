#pragma once

#include "include/core/SkPath.h"
#include <cstdint>

namespace subtitle::skia {
struct SolidOutline {
    SkPath path;
    std::uint32_t before, after;
    bool protectBody;
};

SkPath prepareOutlinePath(const SkPath& body, float width);
}
