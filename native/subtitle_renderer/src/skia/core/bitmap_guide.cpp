#include "bitmap_guide.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColorFilter.h"
#include "include/core/SkPaint.h"
#include "include/core/SkShader.h"
#include "include/core/SkSurface.h"
#include "include/effects/SkImageFilters.h"
#include <algorithm>
#include <cmath>
#include <span>
#include <stdexcept>

namespace subtitle::skia {
PreparedBitmapGuide::PreparedBitmapGuide(const BitmapGuide& guide, std::size_t& remainingHaloBytes)
    : before_(guide.before), after_(guide.after), box_(guide.box), clipPad_(guide.clipPad),
      useLineFront_(guide.useLineFront), animations_(guide.animations), animationAnchorMs_(guide.animationAnchorMs),
      shadow_(guide.shadow) {
    if (!box_.isFinite() || box_.isEmpty() || !std::isfinite(clipPad_) || clipPad_ < 0
        || (!before_ && !after_))
        throw std::runtime_error("Skia 图片导唱符的图片或绘制框无效");
    if (!guide.decorate) return;
    for (const bool after : {false, true}) {
        const auto& state = after ? guide.glow.after : guide.glow.before;
        if (state.radius <= 0) continue;
        auto& halos = halos_[after];
        if (const auto& animation = animations_[after]) {
            halos.reserve(animation->size());
            for (const auto& frame : *animation)
                halos.push_back(prepareHalo(*frame.image, state, remainingHaloBytes));
        } else if (const auto& image = after ? after_ : before_) {
            halos.push_back(prepareHalo(*image, state, remainingHaloBytes));
        }
    }
}

std::vector<PreparedBitmapGuide::Halo> PreparedBitmapGuide::prepareHalo(const SkImage& image,
        const GlowState& state, std::size_t& remainingBytes) const {
    std::vector<Halo> passes;
    if (state.radius <= 0) return passes;
    const float pixelW = std::max(float(image.width()), 1.f), pixelH = std::max(float(image.height()), 1.f);
    const float scaleX = std::max(box_.width() / pixelW, .0001f);
    const float padDevice = std::ceil(state.radius * 3.5f) + 2.f;
    const float pad = padDevice / scaleX;
    // Integer native padding keeps the image on its own pixel grid; the
    // drawn source rectangle below crops to the GPU's fractional pad.
    constexpr int maxSide = 8192;
    const double padded = std::ceil(double(pad)) + 1;
    if (!std::isfinite(padded) || 2 * padded + std::max(image.width(), image.height()) > maxSide)
        throw std::runtime_error("Skia 图片导唱符光晕尺寸超限");
    const int margin = int(padded);
    const int count = std::clamp(state.concentration, 0, 2) + 1;
    const int width = image.width() + 2 * margin, height = image.height() + 2 * margin;
    const std::size_t bytes = std::size_t(width) * height * 4 * count;
    if (bytes > remainingBytes) throw std::runtime_error("Skia 图片导唱符光晕超过场景内存预算");
    remainingBytes -= bytes;
    const SkColor tint = SkColorSetA(state.color, 0xff); // The GPU keeps only the RGB of the decoration.
    for (int index = 0; index < count; ++index) {
        const float sigma = std::max(0.f, float(state.radius - index * state.radius / count) / scaleX);
        auto surface = SkSurfaces::Raster(SkImageInfo::MakeN32Premul(width, height));
        if (!surface) throw std::runtime_error("Skia 图片导唱符光晕临时表面分配失败");
        auto& canvas = *surface->getCanvas();
        canvas.clear(SK_ColorTRANSPARENT);
        SkPaint paint;
        paint.setColorFilter(SkColorFilters::Blend(tint, SkBlendMode::kSrcIn));
        if (sigma > 0) paint.setImageFilter(SkImageFilters::Blur(sigma, sigma, SkTileMode::kDecal, nullptr));
        canvas.drawImage(&image, float(margin), float(margin), SkSamplingOptions(), &paint);
        passes.push_back({surface->makeImageSnapshot(), pad});
        if (!passes.back().image) throw std::runtime_error("Skia 图片导唱符光晕无法取样");
    }
    return passes;
}

void PreparedBitmapGuide::draw(SkCanvas& canvas, bool after, float opacity, int mediaTimeMs) const {
    const SkImage* image = (after ? after_ : before_).get();
    std::size_t frameIndex = 0;
    if (const auto& animation = animations_[after]; animation && !animation->empty()) {
        const auto elapsed = std::max(std::int64_t(mediaTimeMs) - animationAnchorMs_, std::int64_t(0));
        const auto phase = elapsed % animation->back().endMs;
        const auto frame = std::upper_bound(animation->begin(), animation->end(), phase,
            [](auto t, const BitmapGuideFrame& item) { return t < item.endMs; });
        image = frame->image.get();
        frameIndex = std::size_t(frame - animation->begin());
    }
    if (!image || opacity <= 0) return;
    const SkSamplingOptions linear(SkFilterMode::kLinear, SkMipmapMode::kNone);
    const auto imageRect = SkRect::MakeIWH(image->width(), image->height());
    if (const auto& shadow = shadow_[after]; shadow.visible()) {
        SkPaint paint;
        paint.setAlphaf(opacity);
        const auto destination = box_.makeOffset(shadow.x, shadow.y);
        if (shadow.shader) {
            const auto mapping = SkMatrix::RectToRect(imageRect, destination);
            auto mask = image->makeShader(SkTileMode::kClamp, SkTileMode::kClamp, linear, mapping);
            paint.setShader(SkShaders::Blend(SkBlendMode::kDstIn, shadow.shader, std::move(mask)));
            canvas.drawRect(destination, paint);
        } else {
            paint.setColorFilter(SkColorFilters::Blend(shadow.color, SkBlendMode::kSrcIn));
            canvas.drawImageRect(image, imageRect, destination, linear, &paint, SkCanvas::kStrict_SrcRectConstraint);
        }
    }
    const float scaleX = box_.width() / std::max(float(image->width()), 1.f);
    const float scaleY = box_.height() / std::max(float(image->height()), 1.f);
    const auto& halos = halos_[after];
    const auto passes = halos.empty() ? std::span<const Halo>{} : std::span<const Halo>(halos[frameIndex]);
    for (const auto& halo : passes) {
        const float margin = float((halo.image->width() - image->width()) / 2);
        const auto source = SkRect::MakeLTRB(margin - halo.pad, margin - halo.pad,
                                             margin + float(image->width()) + halo.pad,
                                             margin + float(image->height()) + halo.pad);
        // The GPU places the padded source at the box origin minus the device pad on both axes.
        const float padDevice = halo.pad * scaleX;
        const auto destination = SkRect::MakeXYWH(box_.left() - padDevice, box_.top() - padDevice,
                                                  source.width() * scaleX, source.height() * scaleY);
        SkPaint paint;
        paint.setAlphaf(opacity);
        canvas.drawImageRect(halo.image, source, destination, linear, &paint, SkCanvas::kFast_SrcRectConstraint);
    }
    SkPaint paint;
    paint.setAntiAlias(true);
    paint.setAlphaf(opacity);
    canvas.drawImageRect(image, imageRect, box_, linear, &paint,
                         SkCanvas::kStrict_SrcRectConstraint);
}
}
