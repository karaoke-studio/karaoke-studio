#pragma once

#include "../../effects/signal_state.h"
#include "image_fill.h"
#include "signal_paths.h"
#include "static_glow.h"
#include "include/core/SkColor.h"
#include "include/core/SkImage.h"
#include "include/core/SkPoint.h"
#include "include/core/SkShader.h"
#include <optional>

class SkCanvas;

namespace subtitle::skia {
struct SignalTransition;
struct ShapeSignal {
    enum class Shape { Circle, Square, Rounded, Image };
    struct GradientBrushes {
        sk_sp<SkShader> fill, stroke, outerStroke, shadow, glow;
        bool empty() const { return !fill && !stroke && !outerStroke && !shadow && !glow; }
    };
    struct Decoration {
        SkColor outerStroke = 0, shadow = 0;
        float outerWidth = 0;
        float shadowX = 0, shadowY = 0;
        std::vector<PreparedGlow> glow;
        GradientBrushes unitGradients;
        ImageFill fillImage;
        SkPoint glyphPaintOrigin = SkPoint::Make(0, 0);
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
    void draw(SkCanvas& canvas, int time, const SignalTransition&, SkPoint paintShift, float entryOpacity = 1.f) const;
};
}
