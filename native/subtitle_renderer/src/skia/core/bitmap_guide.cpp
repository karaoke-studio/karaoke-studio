#include "bitmap_guide.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColorFilter.h"
#include "include/core/SkPaint.h"
#include "include/core/SkShader.h"
#include "include/core/SkSurface.h"
#include "include/effects/SkImageFilters.h"
#include <algorithm>
#include <cmath>
#include <functional>
#include <stdexcept>

namespace subtitle::skia {
namespace {
void hashCombine(std::size_t& seed, std::size_t value) noexcept {
    seed ^= value + 0x9e3779b97f4a7c15ULL + (seed << 6) + (seed >> 2);
}
}

std::size_t BitmapHaloCache::KeyHash::operator()(const Key& key) const noexcept {
    std::size_t seed = 0;
    hashCombine(seed, std::hash<std::uint32_t>{}(key.imageId));
    hashCombine(seed, std::hash<std::uintptr_t>{}(key.gradientOwner));
    hashCombine(seed, std::hash<bool>{}(key.afterPhase));
    hashCombine(seed, std::hash<int>{}(key.workW));
    hashCombine(seed, std::hash<int>{}(key.workH));
    hashCombine(seed, std::hash<std::int64_t>{}(key.scaleXQ64));
    hashCombine(seed, std::hash<std::int64_t>{}(key.scaleYQ64));
    hashCombine(seed, std::hash<int>{}(key.radius));
    hashCombine(seed, std::hash<int>{}(key.concentration));
    hashCombine(seed, std::hash<std::uint32_t>{}(key.tint));
    hashCombine(seed, std::hash<std::int64_t>{}(key.strengthQ1000));
    return seed;
}

BitmapHaloCache::BitmapHaloCache(std::size_t maxBytes) : maxBytes_(maxBytes) {}

std::size_t BitmapHaloCache::usedBytes() const {
    std::lock_guard lock(mutex_);
    return usedBytes_;
}

std::size_t BitmapHaloCache::entryCount() const {
    std::lock_guard lock(mutex_);
    return index_.size();
}

std::shared_ptr<const BitmapGuideHaloPasses> BitmapHaloCache::find(const Key& key) {
    std::lock_guard lock(mutex_);
    const auto found = index_.find(key);
    if (found == index_.end()) return nullptr;
    lru_.splice(lru_.begin(), lru_, found->second);
    return found->second->passes;
}

std::shared_ptr<const BitmapGuideHaloPasses> BitmapHaloCache::insert(
        const Key& key, std::size_t bytes, std::shared_ptr<const BitmapGuideHaloPasses> passes) {
    std::lock_guard lock(mutex_);
    if (const auto found = index_.find(key); found != index_.end()) {
        lru_.splice(lru_.begin(), lru_, found->second);
        return found->second->passes;
    }
    if (bytes > maxBytes_) return passes;
    while (!lru_.empty() && usedBytes_ + bytes > maxBytes_) {
        const auto& victim = lru_.back();
        usedBytes_ = victim.bytes <= usedBytes_ ? usedBytes_ - victim.bytes : 0;
        index_.erase(victim.key);
        lru_.pop_back();
    }
    lru_.push_front(Entry{key, bytes, passes});
    index_.emplace(key, lru_.begin());
    usedBytes_ += bytes;
    return passes;
}

PreparedBitmapGuide::PreparedBitmapGuide(const BitmapGuide& guide, std::shared_ptr<BitmapHaloCache> haloCache)
    : before_(guide.before), after_(guide.after), wipes_(guide.wipes || guide.after), decorate_(guide.decorate),
      box_(guide.box), clipPad_(guide.clipPad), useLineFront_(guide.useLineFront), glow_(guide.glow),
      glowGradients_(guide.glowGradients),
      haloCache_(haloCache ? std::move(haloCache) : std::make_shared<BitmapHaloCache>()),
      animations_(guide.animations), animationAnchorMs_(guide.animationAnchorMs), shadow_(guide.shadow) {
    if (!box_.isFinite() || box_.isEmpty() || !std::isfinite(clipPad_) || clipPad_ < 0)
        throw std::runtime_error("Skia 图片导唱符的图片或绘制框无效");
    if (!decorate_) return;
    // Validate single-frame halo dimensions/budget eagerly at configure time,
    // while deferring actual raster blurs to draw() through the shared LRU cache.
    for (const bool after : {false, true}) {
        const auto& state = after ? glow_.after : glow_.before;
        if (state.radius <= 0) continue;
        const SkImage* sample = nullptr;
        if (const auto& animation = animations_[after]; animation && !animation->empty())
            sample = animation->front().image.get();
        else if (const auto& image = after ? after_ : before_)
            sample = image.get();
        if (sample) (void)haloGeometry(*sample, state);
    }
}

PreparedBitmapGuide::HaloGeometry PreparedBitmapGuide::haloGeometry(
        const SkImage& image, const GlowState& state) const {
    HaloGeometry geom;
    const float pixelW = std::max(float(image.width()), 1.f);
    const float pixelH = std::max(float(image.height()), 1.f);
    const float rawScaleX = std::max(box_.width() / pixelW, .0001f);
    const float rawScaleY = std::max(box_.height() / pixelH, .0001f);
    // When the source image is larger than its drawn box, blur at device box
    // resolution instead of inflating padding by 1 / scaleX on the native grid.
    geom.downscale = rawScaleX < 1.f || rawScaleY < 1.f;
    geom.workW = geom.downscale
        ? std::clamp(int(std::ceil(box_.width() - 1e-4f)), 1, image.width())
        : image.width();
    geom.workH = geom.downscale
        ? std::clamp(int(std::ceil(box_.height() - 1e-4f)), 1, image.height())
        : image.height();
    geom.workScaleX = std::max(box_.width() / float(geom.workW), .0001f);
    geom.workScaleY = std::max(box_.height() / float(geom.workH), .0001f);
    const float padDevice = std::ceil(state.radius * 3.5f) + 2.f;
    geom.pad = padDevice / geom.workScaleX;
    // Integer working-grid padding keeps the image on its pixel grid; the
    // drawn source rectangle crops to the GPU's fractional pad.
    constexpr int maxSide = 8192;
    const double padded = std::ceil(double(geom.pad)) + 1;
    if (!std::isfinite(padded) || 2 * padded + std::max(geom.workW, geom.workH) > maxSide)
        throw std::runtime_error("Skia 图片导唱符光晕尺寸超限");
    geom.margin = int(padded);
    geom.count = std::clamp(state.concentration, 0, 2) + 1;
    geom.width = geom.workW + 2 * geom.margin;
    geom.height = geom.workH + 2 * geom.margin;
    geom.bytes = std::size_t(geom.width) * std::size_t(geom.height) * 4 * std::size_t(geom.count);
    if (geom.bytes > haloCache_->maxBytes())
        throw std::runtime_error("Skia 图片导唱符光晕超过场景内存预算");
    return geom;
}

std::shared_ptr<const BitmapGuideHaloPasses> PreparedBitmapGuide::resolveHalo(
        const SkImage& image, bool after) const {
    const auto& state = after ? glow_.after : glow_.before;
    if (!decorate_ || state.radius <= 0) return nullptr;
    const auto& gradient = glowGradients_[after];
    const auto geom = haloGeometry(image, state);
    const SkColor tint = gradient ? SK_ColorTRANSPARENT : SkColorSetA(state.color, 0xff);
    const BitmapHaloCache::Key key{
        image.uniqueID(),
        gradient ? reinterpret_cast<std::uintptr_t>(this) : 0,
        gradient ? after : false,
        geom.workW,
        geom.workH,
        std::llround(double(geom.workScaleX) * 64.0),
        std::llround(double(geom.workScaleY) * 64.0),
        state.radius,
        std::clamp(state.concentration, 0, 2),
        tint,
        std::llround(double(state.strength) * 1000.0),
    };
    if (auto cached = haloCache_->find(key)) return cached;
    auto built = std::make_shared<BitmapGuideHaloPasses>(buildHalo(image, state, gradient, geom));
    return haloCache_->insert(key, geom.bytes, std::move(built));
}

BitmapGuideHaloPasses PreparedBitmapGuide::buildHalo(const SkImage& image,
        const GlowState& state, const sk_sp<SkShader>& gradient, const HaloGeometry& geom) const {
    BitmapGuideHaloPasses passes;
    if (state.radius <= 0) return passes;
    const SkColor tint = SkColorSetA(state.color, 0xff); // The GPU keeps only the RGB of the decoration.
    const auto sourceRect = SkRect::MakeIWH(image.width(), image.height());
    const auto imageAtMargin = SkRect::MakeXYWH(
        float(geom.margin), float(geom.margin), float(geom.workW), float(geom.workH));
    const SkSamplingOptions downSampling(SkFilterMode::kLinear, SkMipmapMode::kLinear);
    sk_sp<SkShader> tintedImage;
    if (gradient) {
        const auto nativeMapping = SkMatrix::RectToRect(box_, imageAtMargin);
        auto paint = gradient->makeWithLocalMatrix(nativeMapping);
        auto mask = geom.downscale
            ? image.makeShader(SkTileMode::kClamp, SkTileMode::kClamp, downSampling,
                               SkMatrix::RectToRect(sourceRect, imageAtMargin))
            : image.makeShader(SkTileMode::kClamp, SkTileMode::kClamp, SkSamplingOptions(),
                               SkMatrix::Translate(float(geom.margin), float(geom.margin)));
        tintedImage = SkShaders::Blend(SkBlendMode::kDstIn, std::move(paint), std::move(mask));
    }
    passes.reserve(geom.count);
    for (int index = 0; index < geom.count; ++index) {
        const float radiusPass = float(state.radius - index * state.radius / geom.count);
        const float sigmaX = std::max(0.f, radiusPass / geom.workScaleX);
        const float sigmaY = geom.downscale ? std::max(0.f, radiusPass / geom.workScaleY) : sigmaX;
        auto surface = SkSurfaces::Raster(SkImageInfo::MakeN32Premul(geom.width, geom.height));
        if (!surface) throw std::runtime_error("Skia 图片导唱符光晕临时表面分配失败");
        auto& canvas = *surface->getCanvas();
        canvas.clear(SK_ColorTRANSPARENT);
        SkPaint paint;
        if (sigmaX > 0 || sigmaY > 0) {
            auto blur = SkImageFilters::Blur(sigmaX, sigmaY, SkTileMode::kDecal, nullptr);
            // Same per-pass strength curve as the text glow.
            if (auto strength = glowStrengthFilter(state.strength))
                blur = SkImageFilters::ColorFilter(std::move(strength), std::move(blur));
            paint.setImageFilter(std::move(blur));
        }
        if (tintedImage) {
            paint.setShader(tintedImage);
            canvas.drawRect(imageAtMargin, paint);
        } else {
            paint.setColorFilter(SkColorFilters::Blend(tint, SkBlendMode::kSrcIn));
            if (geom.downscale) {
                canvas.drawImageRect(&image, sourceRect, imageAtMargin, downSampling, &paint,
                                     SkCanvas::kStrict_SrcRectConstraint);
            } else {
                canvas.drawImage(&image, float(geom.margin), float(geom.margin), SkSamplingOptions(), &paint);
            }
        }
        passes.push_back({surface->makeImageSnapshot(), geom.pad, geom.workW, geom.workH});
        if (!passes.back().image) throw std::runtime_error("Skia 图片导唱符光晕无法取样");
    }
    return passes;
}

void PreparedBitmapGuide::draw(SkCanvas& canvas, bool after, float opacity, int mediaTimeMs) const {
    const SkImage* image = (after ? after_ : before_).get();
    if (const auto& animation = animations_[after]; animation && !animation->empty()) {
        const auto elapsed = std::max(std::int64_t(mediaTimeMs) - animationAnchorMs_, std::int64_t(0));
        const auto phase = elapsed % animation->back().endMs;
        const auto frame = std::upper_bound(animation->begin(), animation->end(), phase,
            [](auto t, const BitmapGuideFrame& item) { return t < item.endMs; });
        image = frame->image.get();
    }
    if (!image || opacity <= 0) return;
    const SkSamplingOptions linear(SkFilterMode::kLinear, SkMipmapMode::kNone);
    const auto imageRect = SkRect::MakeIWH(image->width(), image->height());
    if (const auto& shadow = shadow_[after]; shadow.visible()) {
        SkPaint paint;
        paint.setAlphaf(opacity);
        const auto destination = box_.makeOffset(shadow.x, shadow.y);
        auto shader = shadow.image ? shadow.image.shaderAt(canvas.getTotalMatrix()) : shadow.shader;
        if (shader) {
            const auto mapping = SkMatrix::RectToRect(imageRect, destination);
            auto mask = image->makeShader(SkTileMode::kClamp, SkTileMode::kClamp, linear, mapping);
            paint.setShader(SkShaders::Blend(SkBlendMode::kDstIn, std::move(shader), std::move(mask)));
            canvas.drawRect(destination, paint);
        } else {
            paint.setColorFilter(SkColorFilters::Blend(shadow.color, SkBlendMode::kSrcIn));
            canvas.drawImageRect(image, imageRect, destination, linear, &paint, SkCanvas::kStrict_SrcRectConstraint);
        }
    }
    if (const auto passes = resolveHalo(*image, after)) {
        for (const auto& halo : *passes) {
            const float margin = float((halo.image->width() - halo.workW) / 2);
            const auto source = SkRect::MakeLTRB(margin - halo.pad, margin - halo.pad,
                                                 margin + float(halo.workW) + halo.pad,
                                                 margin + float(halo.workH) + halo.pad);
            const float scaleX = box_.width() / std::max(float(halo.workW), 1.f);
            const float scaleY = box_.height() / std::max(float(halo.workH), 1.f);
            // The GPU places the padded source at the box origin minus the device pad on both axes.
            const float padDevice = halo.pad * scaleX;
            const auto destination = SkRect::MakeXYWH(box_.left() - padDevice, box_.top() - padDevice,
                                                      source.width() * scaleX, source.height() * scaleY);
            SkPaint paint;
            paint.setAlphaf(opacity);
            canvas.drawImageRect(halo.image, source, destination, linear, &paint, SkCanvas::kFast_SrcRectConstraint);
        }
    }
    SkPaint paint;
    paint.setAntiAlias(true);
    paint.setAlphaf(opacity);
    canvas.drawImageRect(image, imageRect, box_, linear, &paint,
                         SkCanvas::kStrict_SrcRectConstraint);
}
}
