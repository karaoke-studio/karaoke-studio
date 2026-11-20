#include "line_band.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColorFilter.h"
#include "include/core/SkColorSpace.h"
#include "include/core/SkPaint.h"
#include "include/core/SkSurface.h"
#include "include/effects/SkImageFilters.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <stdexcept>
#include <utility>

namespace subtitle::skia {
namespace {
sk_sp<SkImageFilter> blurMask(const LineBandTexture::Blur& effect, float scale, float opacity) {
    const int radius = std::max(1, int(std::lround(effect.size * scale)));
    const int spread = std::max(0, int(std::lround(effect.spread * scale)));
    const int length = 2 * radius + 1;
    std::vector<float> weights(length, 1.f / length);
    auto layer = spread ? SkImageFilters::Dilate(spread, spread, nullptr) : nullptr;
    // The authored blur radius specifies three passes of a box kernel.
    for (int pass = 0; pass < 3; ++pass) {
        layer = SkImageFilters::MatrixConvolution({length, 1}, weights.data(), 1.f, 0, {radius, 0},
                                                   SkTileMode::kDecal, true, std::move(layer));
        layer = SkImageFilters::MatrixConvolution({1, length}, weights.data(), 1.f, 0, {0, radius},
                                                   SkTileMode::kDecal, true, std::move(layer));
    }
    auto tint = SkColor4f::FromColor(effect.color);
    tint.fA *= opacity;
    auto color = SkColorFilters::Blend(tint, nullptr, SkBlendMode::kSrcIn);
    if (effect.range != .5f) {
        const float gamma = .5f / std::clamp(effect.range, .01f, 1.f);
        std::array<std::uint8_t, 256> alpha{};
        for (std::size_t i = 0; i < alpha.size(); ++i)
            alpha[i] = std::uint8_t(std::lround(std::pow(float(i) / 255.f, gamma) * 255.f));
        color = color->makeComposed(SkColorFilters::TableARGB(alpha.data(), nullptr, nullptr, nullptr));
    }
    return SkImageFilters::ColorFilter(std::move(color), std::move(layer));
}

sk_sp<SkImageFilter> strokeMask(const LineBandTexture::Stroke& effect, float scale) {
    using Position = LineBandTexture::Stroke::Position;
    const int size = std::max(1, int(std::lround(effect.size * scale)));
    const bool inside = effect.position == Position::Inside;
    const bool center = effect.position == Position::Center;
    const int radius = center ? std::max(1, size / 2) : size;
    auto expanded = inside ? nullptr : SkImageFilters::Dilate(radius, radius, nullptr);
    if (inside || center) {
        auto eroded = SkImageFilters::Erode(radius, radius, nullptr);
        expanded = SkImageFilters::Arithmetic(0, 1, -1, 0, true, std::move(eroded), std::move(expanded));
    }
    return SkImageFilters::ColorFilter(SkColorFilters::Blend(effect.color, SkBlendMode::kSrcIn),
                                       std::move(expanded));
}
}

void LineBandTexture::validateEffectBounds(const SkRect& bounds) const {
    if ((!stroke && !shadow && !glow) || !image || bounds.isEmpty()) return;
    const float scale = bounds.height() / image->height();
    const float width = std::ceil(bounds.width()), height = std::ceil(bounds.height());
    if (width > 8192 || height > 8192 || width * height > 16 * 1024 * 1024
        || (stroke && stroke->size * scale > 512))
        throw std::runtime_error("Skia 行背景带描边尺寸超限");
    for (const auto* effect : {&shadow, &glow}) {
        // The pinned convolution backend supports at most 256 taps per kernel.
        if (*effect && ((*effect)->size * scale >= 127.5f || (*effect)->spread * scale > 512))
            throw std::runtime_error("Skia 行背景带阴影或发光尺寸超限");
    }
}

void LineBandTexture::drawDecorated(SkCanvas& canvas, const SkRect& bounds, float alpha, int timeMs) const {
    validateEffectBounds(bounds);
    const float scale = bounds.height() / image->height();
    auto visible = bounds;
    for (const auto* effect : {&shadow, &glow})
        if (*effect) {
            const float pad = (3 * (*effect)->size + (*effect)->spread) * scale + 2;
            visible.join(bounds.makeOffset((*effect)->dx, (*effect)->dy).makeOutset(pad, pad));
        }
    if (stroke) {
        const float pad = stroke->size * scale + 2;
        visible.join(bounds.makeOutset(pad, pad));
    }
    if (!SkRect::Intersects(visible, canvas.getLocalClipBounds())) return;
    const int width = int(std::ceil(bounds.width())), height = int(std::ceil(bounds.height()));
    auto surface = canvas.makeSurface(SkImageInfo::MakeN32Premul(width, height));
    if (!surface) throw std::runtime_error("Skia 行背景带描边临时表面分配失败");
    auto& source = *surface->getCanvas();
    source.clear(SK_ColorTRANSPARENT);
    drawContent(source, SkRect::MakeWH(bounds.width(), bounds.height()), 1.f, timeMs);
    auto image = surface->makeImageSnapshot();
    if (!image) throw std::runtime_error("Skia 行背景带描边临时图像分配失败");
    const SkSamplingOptions linear(SkFilterMode::kLinear);
    SkPaint paint;
    const auto draw = [&](const sk_sp<SkImageFilter>& effect, float opacity, float dx = 0.f, float dy = 0.f) {
        paint.setAlphaf(opacity);
        paint.setImageFilter(effect);
        canvas.drawImage(image, bounds.left() + dx, bounds.top() + dy, linear, &paint);
    };
    // Apply opacity after the mask transfer. Applying it to the input before
    // the glow range curve changes the shape and strength of the glow.
    if (shadow) draw(blurMask(*shadow, scale, alpha), 1.f, shadow->dx, shadow->dy);
    if (glow) draw(blurMask(*glow, scale, alpha), 1.f);
    if (stroke && stroke->position == Stroke::Position::Outside) draw(strokeMask(*stroke, scale), alpha);
    draw(nullptr, alpha);
    if (stroke && stroke->position != Stroke::Position::Outside) draw(strokeMask(*stroke, scale), alpha);
}
}
