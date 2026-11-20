#pragma once

#include "../../model/line_band.h"
#include "../../skia/core/line_band.h"

namespace krok::subtitle::native {
class SkiaImageFillCache;

::subtitle::skia::LineBandTexture resolveLineBandTexture(const LineBandStyle&, SkiaImageFillCache&, bool animated);
}
