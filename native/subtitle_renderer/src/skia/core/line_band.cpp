#include "line_band.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include "include/core/SkShader.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace subtitle::skia {
namespace {
SkRect centeredCrop(const SkRect& source, const SkRect& destination) {
    const float scale = std::max(destination.width() / source.width(), destination.height() / source.height());
    const float width = std::min(destination.width() / scale, source.width());
    const float height = std::min(destination.height() / scale, source.height());
    return SkRect::MakeXYWH(source.centerX() - width / 2, source.centerY() - height / 2, width, height);
}
}

void LineBandTexture::prepareMiddle() {
    if (!image || !slices || slices->middle != Fit::Repeat) return;
    const int left = slices->sourceLeft, right = image->width() - slices->sourceRight;
    if (right <= left) return;
    repeatedMiddle = image->makeSubset(nullptr, SkIRect::MakeLTRB(left, 0, right, image->height()), {});
    if (!repeatedMiddle) throw std::runtime_error("Skia 行背景带无法准备平铺中段");
}

void LineBandTexture::draw(SkCanvas& canvas, const SkRect& destination, float alpha) const {
    const SkSamplingOptions linear(SkFilterMode::kLinear);
    SkPaint paint;
    paint.setAlphaf(alpha);
    const auto drawStretch = [&] { canvas.drawImageRect(image, destination, linear, &paint); };
    if (!slices && fit == Fit::Stretch) {
        drawStretch();
        return;
    }
    const float sourceWidth = float(image->width()), sourceHeight = float(image->height());
    const float heightScale = destination.height() / sourceHeight;
    const auto drawPiece = [&](const SkRect& source, const SkRect& area) {
        if (!source.isEmpty() && !area.isEmpty())
            canvas.drawImageRect(image, source, area, linear, &paint, SkCanvas::kFast_SrcRectConstraint);
    };
    if (slices) {
        const auto& caps = *slices;
        const float total = caps.left + caps.right;
        const float scale = total * heightScale > destination.width() ? destination.width() / total : heightScale;
        const float left = destination.left() + caps.left * scale;
        const float right = destination.right() - caps.right * scale;
        drawPiece(SkRect::MakeWH(caps.left, sourceHeight),
                  SkRect::MakeLTRB(destination.left(), destination.top(), left, destination.bottom()));
        drawPiece(SkRect::MakeLTRB(sourceWidth - caps.right, 0, sourceWidth, sourceHeight),
                  SkRect::MakeLTRB(right, destination.top(), destination.right(), destination.bottom()));
        const auto middle = SkRect::MakeLTRB(left, destination.top(), right, destination.bottom());
        const auto source = SkRect::MakeLTRB(float(caps.sourceLeft), 0, sourceWidth - caps.sourceRight, sourceHeight);
        if (middle.isEmpty() || source.isEmpty()) return;
        if (caps.middle == Fit::Repeat && repeatedMiddle) {
            // Repeat the isolated middle pixels seamlessly. Endpoint pixels
            // never enter its bilinear sample, even at fractional scaling.
            const float sourceSpan = std::min(middle.width() / scale, 8191.f);
            SkMatrix matrix;
            matrix.setScaleTranslate(middle.width() / sourceSpan, heightScale, middle.left(), middle.top());
            paint.setShader(repeatedMiddle->makeShader(SkTileMode::kRepeat, SkTileMode::kClamp, linear, matrix));
            canvas.drawRect(middle, paint);
        } else {
            drawPiece(caps.middle == Fit::Cover ? centeredCrop(source, middle) : source, middle);
        }
        return;
    }
    if (fit == Fit::Cover) {
        drawPiece(centeredCrop(SkRect::MakeWH(sourceWidth, sourceHeight), destination), destination);
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
        drawPiece(crop, tile);
    }
}
}
