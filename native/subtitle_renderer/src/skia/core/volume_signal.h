#pragma once

#include "../../effects/signal_state.h"
#include "static_glow.h"
#include "include/core/SkRRect.h"
#include <array>
#include <vector>

class SkCanvas;

namespace subtitle::skia {
struct VolumeSignal {
    struct Palette {
        SkColor fill = 0, stroke = 0, outerStroke = 0, shadow = 0;
        float shadowX = 0, shadowY = 0;
    };
    struct Bar {
        SkRRect rect;
        std::vector<PreparedGlow> glow;
    };
    krok::subtitle::native::VolumeSignalClock clock;
    float opacity = 1;
    float strokeWidth = 0, outerWidth = 0;
    bool decorated = false;
    std::array<Palette, 2> palette;
    std::vector<Bar> bars;

    void draw(SkCanvas&, int time) const;
};
}
