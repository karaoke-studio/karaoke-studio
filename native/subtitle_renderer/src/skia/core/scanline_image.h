#pragma once

#include "include/core/SkImage.h"
#include "include/core/SkRect.h"
#include <cstdint>
#include <vector>

class SkCanvas;
namespace subtitle::skia {
struct ScanlineImageFrame {
    sk_sp<SkImage> image;
    SkRect destination;
    std::int64_t endMs = 0;
};

struct ScanlineImage {
    std::vector<ScanlineImageFrame> frames;
    float offsetX = 0, offsetY = 0;
    bool showBand = true;

    void draw(SkCanvas&, int timeMs, float front, float centerY, float opacity) const;
};
}
