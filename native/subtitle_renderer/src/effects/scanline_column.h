#pragma once
#include "../model/paint_types.h"

namespace krok::subtitle::native::effects {
std::vector<std::uint8_t> scanlineColumnPixels(
    const std::string &shape,
    const RgbaColor &outer,
    const RgbaColor &inner,
    const RgbaColor &core,
    const PaintStyle *fill,
    int timeMs,
    unsigned width,
    unsigned height,
    float gradientLeft,
    float gradientWidth
);


}
