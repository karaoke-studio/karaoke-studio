#pragma once

#include "include/core/SkImage.h"
#include "include/core/SkRect.h"
#include <optional>

class SkCanvas;

namespace subtitle::skia {
struct LineBandTexture {
    enum class Fit { Stretch, Cover, Repeat };
    struct Slices {
        float left = 0, right = 0;
        int sourceLeft = 0, sourceRight = 0;
        Fit middle = Fit::Repeat;
    };

    sk_sp<SkImage> image;
    float opacity = 0;
    Fit fit = Fit::Stretch;
    std::optional<Slices> slices;
    sk_sp<SkImage> repeatedMiddle;

    void prepareMiddle();
    void draw(SkCanvas&, const SkRect& destination, float alpha) const;
};
}
