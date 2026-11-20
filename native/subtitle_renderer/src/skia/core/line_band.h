#pragma once

#include "include/core/SkImage.h"
#include "include/core/SkRect.h"
#include <cstdint>
#include <optional>
#include <vector>

class SkCanvas;

namespace subtitle::skia {
struct LineBandTexture {
    enum class Fit { Stretch, Cover, Repeat };
    struct Slices {
        float left = 0, right = 0;
        int sourceLeft = 0, sourceRight = 0;
        Fit middle = Fit::Repeat;
    };
    struct Frame {
        sk_sp<SkImage> image;
        std::int64_t endMs = 0;
    };

    sk_sp<SkImage> image;
    float opacity = 0;
    Fit fit = Fit::Stretch;
    std::optional<Slices> slices;
    sk_sp<SkImage> repeatedMiddle;
    sk_sp<SkImage> staffLines, staffClef;
    std::vector<Frame> animation;

    void prepareMiddle();
    void draw(SkCanvas&, const SkRect& destination, float alpha, int timeMs) const;

private:
    void drawLayer(SkCanvas&, const sk_sp<SkImage>&, const sk_sp<SkImage>& middle,
                   const SkRect& destination, float alpha) const;
};
}
