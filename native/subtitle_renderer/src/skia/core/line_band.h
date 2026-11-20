#pragma once

#include "include/core/SkImage.h"
#include "include/core/SkRect.h"

class SkCanvas;

namespace subtitle::skia {
struct LineBandTexture {
    enum class Fit { Stretch, Cover, Repeat };

    sk_sp<SkImage> image;
    float opacity = 0;
    Fit fit = Fit::Stretch;

    void draw(SkCanvas&, const SkRect& destination, float alpha) const;
};
}
