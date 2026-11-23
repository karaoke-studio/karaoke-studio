#pragma once

#include "../../effects/signal_state.h"
#include "image_fill.h"
#include "static_glow.h"
#include "include/core/SkRRect.h"
#include "include/core/SkShader.h"
#include <array>
#include <vector>

class SkCanvas;

namespace subtitle::skia {
struct SignalTransition;
struct VolumeSignal {
    struct Palette {
        SkColor fill = 0, stroke = 0, outerStroke = 0, shadow = 0;
        float shadowX = 0, shadowY = 0;
        sk_sp<SkShader> fillGradient, strokeGradient, outerGradient, shadowGradient;
        ImageFill fillImage, strokeImage, outerImage, shadowImage, glowImage;
    };
    struct Bar {
        SkRRect rect;
        std::vector<PreparedGlow> glow;
    };
    krok::subtitle::native::VolumeSignalClock clock;
    int singingStartMs = 0;
    float opacity = 1;
    float strokeWidth = 0, outerWidth = 0;
    float textOriginX = 0, groupLeft = 0, groupRight = 0;
    bool decorated = false;
    std::array<Palette, 2> palette;
    std::vector<Bar> bars;

    void drawTransitionHalo(SkCanvas&, int time, const SignalTransition&) const;
    void draw(SkCanvas&, int time, const SignalTransition&, SkPoint paintShift) const;
};
}
