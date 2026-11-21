#pragma once
#include "include/core/SkRect.h"

class SkCanvas;

namespace subtitle::skia {
void applyScanlineFeather(SkCanvas&, const SkRect& layer, float front, float halfWidth,
                         float softness, float maskLeft, float maskTop, bool vertical = false);
void clipScanlineSlice(SkCanvas&, float low, float high, bool vertical = false);
}
