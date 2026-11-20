#include "line_band.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include "include/core/SkShader.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace subtitle::skia {
float LineBandTransition::opacityAt(int timeMs, int startMs, int endMs) const {
    const auto ease = [](int remaining, int duration) {
        if (duration <= 0) return 1.f;
        const float p = std::clamp(float(remaining) / duration, 0.f, 1.f);
        return p * p * (3.f - 2.f * p);
    };
    return ease(timeMs - startMs, inMs) * ease(endMs - timeMs, outMs);
}

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

void LineBandTexture::draw(SkCanvas& canvas, const SkRect& destination, float alpha, int timeMs) const {
    if (animation.empty()) {
        drawLayer(canvas, image, repeatedMiddle, destination, alpha);
        return;
    }
    const auto duration = animation.back().endMs;
    const auto phase = ((timeMs % duration) + duration) % duration;
    const auto frame = std::upper_bound(animation.begin(), animation.end(), phase,
        [](auto time, const Frame& item) { return time < item.endMs; });
    drawLayer(canvas, staffLines, {}, destination, alpha);
    drawLayer(canvas, staffClef, {}, destination, alpha);
    drawLayer(canvas, frame->image, {}, destination, alpha);
}

void LineBandTexture::drawLayer(SkCanvas& canvas, const sk_sp<SkImage>& layer, const sk_sp<SkImage>& repeatImage,
                                const SkRect& destination, float alpha) const {
    if (!layer) return;
    const SkSamplingOptions linear(SkFilterMode::kLinear);
    SkPaint paint;
    paint.setAlphaf(alpha);
    if (!slices && fit == Fit::Stretch) {
        canvas.drawImageRect(layer, destination, linear, &paint);
        return;
    }
    const float sourceWidth = float(layer->width()), sourceHeight = float(layer->height());
    const float heightScale = destination.height() / sourceHeight;
    const auto drawPiece = [&](const SkRect& source, const SkRect& area) {
        if (!source.isEmpty() && !area.isEmpty())
            canvas.drawImageRect(layer, source, area, linear, &paint, SkCanvas::kFast_SrcRectConstraint);
    };
    const auto drawTiled = [&](const SkRect& source, const SkRect& area, float scale) {
        const float tileWidth = source.width() * scale;
        if (tileWidth <= 0.5f) {
            drawPiece(source, area);
            return;
        }
        // Preserve tile origins and the last partial tile when scissoring an offscreen band.
        SkRect visible;
        if (!visible.intersect(area, canvas.getLocalClipBounds())) return;
        const int first = int(std::floor((visible.left() - area.left()) / tileWidth));
        for (int index = first; ; ++index) {
            const float left = area.left() + index * tileWidth;
            if (left >= area.right() - 0.5f || left >= visible.right()) break;
            const float right = std::min(left + tileWidth, area.right());
            const auto tile = SkRect::MakeLTRB(left, area.top(), right, area.bottom());
            const auto crop = SkRect::MakeXYWH(source.left(), source.top(),
                std::min((right - left) / scale, source.width()), source.height());
            drawPiece(crop, tile);
        }
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
        if (caps.middle == Fit::Repeat && repeatImage) {
            // Repeat the isolated middle pixels seamlessly. Endpoint pixels
            // never enter its bilinear sample, even at fractional scaling.
            const float sourceSpan = std::min(middle.width() / scale, 8191.f);
            SkMatrix matrix;
            matrix.setScaleTranslate(middle.width() / sourceSpan, heightScale, middle.left(), middle.top());
            paint.setShader(repeatImage->makeShader(SkTileMode::kRepeat, SkTileMode::kClamp, linear, matrix));
            canvas.drawRect(middle, paint);
        } else if (caps.middle == Fit::Repeat) {
            drawTiled(source, middle, scale);
        } else {
            drawPiece(caps.middle == Fit::Cover ? centeredCrop(source, middle) : source, middle);
        }
        return;
    }
    if (fit == Fit::Cover) {
        drawPiece(centeredCrop(SkRect::MakeWH(sourceWidth, sourceHeight), destination), destination);
        return;
    }
    drawTiled(SkRect::MakeWH(sourceWidth, sourceHeight), destination, heightScale);
}
}
