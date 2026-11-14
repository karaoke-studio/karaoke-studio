#pragma once
class SkCanvas;

namespace subtitle::skia {
// Deterministic, time-driven translucent pattern used to prove a GPU hand-off path end to end
// (P03). It depends only on the requested time, never on frame history, so any target that
// draws it at the same time produces the same picture.
void drawMotionProbe(SkCanvas& canvas, int width, int height, double seconds);
}
