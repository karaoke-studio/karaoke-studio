#include "line_band.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include <algorithm>
#include <cmath>

namespace subtitle::skia {
void LineBandTexture::draw(SkCanvas& canvas, const SkRect& destination, float alpha) const {
    const SkSamplingOptions linear(SkFilterMode::kLinear);
    SkPaint paint;
    paint.setAlphaf(alpha);
    const auto drawStretch = [&] { canvas.drawImageRect(image, destination, linear, &paint); };
    if (fit == Fit::Stretch) {
        drawStretch();
        return;
    }
    const float sourceWidth = float(image->width()), sourceHeight = float(image->height());
    const float heightScale = destination.height() / sourceHeight;
    if (fit == Fit::Cover) {
        const float scale = std::max(destination.width() / sourceWidth, heightScale);
        const float width = std::min(destination.width() / scale, sourceWidth);
        const float height = std::min(destination.height() / scale, sourceHeight);
        const auto crop = SkRect::MakeXYWH((sourceWidth - width) / 2, (sourceHeight - height) / 2, width, height);
        canvas.drawImageRect(image, crop, destination, linear, &paint, SkCanvas::kFast_SrcRectConstraint);
        return;
    }
    const float tileWidth = sourceWidth * heightScale;
    if (tileWidth <= 0.5f) {
        drawStretch();
        return;
    }
    // Preserve tile origins and the final partial tile when scissoring an offscreen band.
    SkRect visible;
    if (!visible.intersect(destination, canvas.getLocalClipBounds())) return;
    const int first = int(std::floor((visible.left() - destination.left()) / tileWidth));
    for (int index = first; ; ++index) {
        const float left = destination.left() + index * tileWidth;
        if (left >= destination.right() - 0.5f || left >= visible.right()) break;
        const float right = std::min(left + tileWidth, destination.right());
        const auto tile = SkRect::MakeLTRB(left, destination.top(), right, destination.bottom());
        const auto crop = SkRect::MakeWH(std::min((right - left) / heightScale, sourceWidth), sourceHeight);
        canvas.drawImageRect(image, crop, tile, linear, &paint, SkCanvas::kFast_SrcRectConstraint);
    }
}
}
