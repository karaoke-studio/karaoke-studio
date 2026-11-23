#pragma once

#include "../../effects/signal_state.h"
#include "signal_paths.h"
#include "static_glow.h"
#include "include/core/SkColor.h"
#include "include/core/SkImage.h"
#include "include/core/SkPoint.h"
#include <optional>

class SkCanvas;

namespace subtitle::skia {
struct ShapeSignal {
    enum class Shape { Circle, Square, Rounded, Image };
    struct Decoration {
        SkColor outerStroke = 0, shadow = 0;
        float outerWidth = 0;
        float shadowX = 0, shadowY = 0;
        std::vector<PreparedGlow> glow;
    };
    krok::subtitle::native::ShapeSignalClock clock;
    SkPoint origin;
    Shape shape;
    float size, pitch;
    float strokeWidth, strokeSoften, opacity, highlight;
    SkColor fill, stroke;
    bool shadow;
    sk_sp<SkImage> image;
    std::optional<Decoration> decoration;
    std::optional<SignalPath> path;

    SkPath outline() const;
    void draw(SkCanvas& canvas, int time, float entryOpacity = 1.f) const;
};
}
