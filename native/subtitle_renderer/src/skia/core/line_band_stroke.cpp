#include "line_band.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColorFilter.h"
#include "include/core/SkPaint.h"
#include "include/core/SkSurface.h"
#include "include/effects/SkImageFilters.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace subtitle::skia {
void LineBandTexture::validateStrokeBounds(const SkRect& bounds) const {
    if (!stroke || !image || bounds.isEmpty()) return;
    const float effectSize = stroke->size * bounds.height() / image->height();
    const float width = std::ceil(bounds.width()), height = std::ceil(bounds.height());
    if (width > 8192 || height > 8192 || width * height > 16 * 1024 * 1024 || effectSize > 512)
        throw std::runtime_error("Skia 行背景带描边尺寸超限");
}

void LineBandTexture::drawStroke(SkCanvas& canvas, const SkRect& bounds, float alpha, int timeMs) const {
    if (!SkRect::Intersects(bounds, canvas.getLocalClipBounds().makeOutset(512, 512))) return;
    validateStrokeBounds(bounds);
    const float effectSize = stroke->size * bounds.height() / image->height();
    const int width = int(std::ceil(bounds.width())), height = int(std::ceil(bounds.height()));
    const int size = std::max(1, int(std::lround(effectSize)));
    const bool inside = stroke->position == Stroke::Position::Inside;
    const bool center = stroke->position == Stroke::Position::Center;
    const int radius = center ? std::max(1, size / 2) : size;
    auto surface = canvas.makeSurface(SkImageInfo::MakeN32Premul(width, height));
    if (!surface) throw std::runtime_error("Skia 行背景带描边临时表面分配失败");
    auto& source = *surface->getCanvas();
    source.clear(SK_ColorTRANSPARENT);
    drawContent(source, SkRect::MakeWH(bounds.width(), bounds.height()), 1.f, timeMs);
    auto image = surface->makeImageSnapshot();
    if (!image) throw std::runtime_error("Skia 行背景带描边临时图像分配失败");
    const SkSamplingOptions linear(SkFilterMode::kLinear);
    SkPaint paint;
    paint.setAlphaf(alpha);
    auto expanded = inside ? nullptr : SkImageFilters::Dilate(radius, radius, nullptr);
    if (inside || center) {
        auto eroded = SkImageFilters::Erode(radius, radius, nullptr);
        expanded = SkImageFilters::Arithmetic(0, 1, -1, 0, true, std::move(eroded), std::move(expanded));
    }
    const auto outline = SkImageFilters::ColorFilter(SkColorFilters::Blend(stroke->color, SkBlendMode::kSrcIn),
                                                    std::move(expanded));
    const auto draw = [&](const sk_sp<SkImageFilter>& effect) {
        paint.setImageFilter(effect);
        canvas.drawImage(image, bounds.left(), bounds.top(), linear, &paint);
    };
    if (!inside && !center) draw(outline);
    draw(nullptr);
    if (inside || center) draw(outline);
}
}
