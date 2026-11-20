#pragma once

#include "include/core/SkImage.h"
#include "include/core/SkColor.h"
#include "include/core/SkRect.h"
#include <cstdint>
#include <optional>
#include <vector>

class SkCanvas;

namespace subtitle::skia {
struct LineBandTransition {
    int inMs = 0, outMs = 0;

    float opacityAt(int timeMs, int startMs, int endMs) const;
};

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
    struct Stroke {
        enum class Position { Outside, Inside, Center };

        float size = 0;
        SkColor color = SK_ColorWHITE;
        Position position = Position::Outside;
    };
    struct Blur {
        float size = 0, spread = 0, range = .5f;
        SkColor color = SK_ColorWHITE;
        float dx = 0, dy = 0;
    };

    sk_sp<SkImage> image;
    float opacity = 0;
    Fit fit = Fit::Stretch;
    std::optional<Slices> slices;
    sk_sp<SkImage> repeatedMiddle;
    sk_sp<SkImage> staffLines, staffClef;
    std::vector<Frame> animation;
    std::optional<Stroke> stroke;
    std::optional<Blur> shadow, glow;

    void prepareMiddle();
    void validateEffectBounds(const SkRect&) const;
    void draw(SkCanvas&, const SkRect& destination, float alpha, int timeMs) const;

private:
    void drawContent(SkCanvas&, const SkRect&, float alpha, int timeMs) const;
    void drawDecorated(SkCanvas&, const SkRect&, float alpha, int timeMs) const;
    void drawLayer(SkCanvas&, const sk_sp<SkImage>&, const sk_sp<SkImage>& middle,
                   const SkRect& destination, float alpha) const;
};
}
