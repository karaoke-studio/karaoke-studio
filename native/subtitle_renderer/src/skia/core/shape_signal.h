#pragma once

#include "../../backends/signal_state.h"
#include "include/core/SkColor.h"
#include "include/core/SkPoint.h"

class SkCanvas;

namespace subtitle::skia {
struct ShapeSignal {
    enum class Shape { Circle, Square, Rounded };
    krok::subtitle::native::ShapeSignalClock clock;
    SkPoint origin;
    Shape shape;
    float size, pitch;
    float strokeWidth, strokeSoften, opacity, highlight;
    SkColor fill, stroke;
    bool shadow;

    void draw(SkCanvas& canvas, int time) const;
};
}
