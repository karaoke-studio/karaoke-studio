#include "core/motion_probe.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include "include/core/SkRRect.h"
#include <cmath>

namespace subtitle::skia {
void drawMotionProbe(SkCanvas& canvas, int width, int height, double seconds) {
    canvas.clear(SK_ColorTRANSPARENT);
    const float w = static_cast<float>(width), h = static_cast<float>(height);
    // Position comes from the requested time only: a 2 s ping-pong sweep across the width.
    const double phase = std::fmod(seconds, 2.0);
    const float sweep = static_cast<float>(phase < 1.0 ? phase : 2.0 - phase);
    const float bar = w * 0.25f;
    SkPaint paint;
    paint.setAntiAlias(true);
    paint.setColor(SkColorSetARGB(160, 40, 200, 255));
    canvas.drawRRect(SkRRect::MakeRectXY(SkRect::MakeXYWH(sweep * (w - bar), h * 0.2f, bar, h * 0.6f),
                                         h * 0.1f, h * 0.1f), paint);
    paint.setColor(SkColorSetARGB(200, 255, 170, 30));
    canvas.drawCircle((1.0f - sweep) * w, h * 0.5f, h * 0.2f, paint);
}
}
