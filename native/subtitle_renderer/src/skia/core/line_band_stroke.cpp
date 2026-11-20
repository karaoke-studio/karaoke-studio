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
    if (!outerStroke || !image || bounds.isEmpty()) return;
    const float effectSize = outerStroke->size * bounds.height() / image->height();
    const float width = std::ceil(bounds.width()), height = std::ceil(bounds.height());
    if (width > 8192 || height > 8192 || width * height > 16 * 1024 * 1024 || effectSize > 512)
        throw std::runtime_error("Skia 行背景带描边尺寸超限");
}

void LineBandTexture::drawOuterStroke(SkCanvas& canvas, const SkRect& bounds, float alpha, int timeMs) const {
    if (!SkRect::Intersects(bounds, canvas.getLocalClipBounds().makeOutset(512, 512))) return;
    validateStrokeBounds(bounds);
    const float effectSize = outerStroke->size * bounds.height() / image->height();
    const int width = int(std::ceil(bounds.width())), height = int(std::ceil(bounds.height()));
    const int radius = std::max(1, int(std::lround(effectSize)));
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
    paint.setImageFilter(SkImageFilters::ColorFilter(SkColorFilters::Blend(outerStroke->color, SkBlendMode::kSrcIn),
                                                   SkImageFilters::Dilate(radius, radius, nullptr)));
    canvas.drawImage(image, bounds.left(), bounds.top(), linear, &paint);
    paint.setImageFilter(nullptr);
    canvas.drawImage(image, bounds.left(), bounds.top(), linear, &paint);
}
}
