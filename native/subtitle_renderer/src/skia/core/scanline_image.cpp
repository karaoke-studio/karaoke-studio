#include "scanline_image.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include <algorithm>

namespace subtitle::skia {
void ScanlineImage::draw(SkCanvas& canvas, int timeMs, float front, float centerY, float opacity) const {
    if (frames.empty() || opacity <= 0) return;
    const auto duration = frames.back().endMs;
    if (duration <= 0) return;
    auto time = timeMs % duration;
    if (time < 0) time += duration;
    const auto frame = std::upper_bound(frames.begin(), frames.end(), time,
        [](auto position, const ScanlineImageFrame& item) { return position < item.endMs; });
    SkPaint paint;
    paint.setAntiAlias(true);
    paint.setAlphaf(opacity);
    const auto destination = frame->destination.makeOffset(front + offsetX, centerY + offsetY);
    canvas.drawImageRect(frame->image, destination, SkSamplingOptions(SkFilterMode::kLinear), &paint);
}
}
