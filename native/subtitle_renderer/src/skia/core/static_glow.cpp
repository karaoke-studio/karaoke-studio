#include "static_glow.h"
#include "solid_outline.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColorFilter.h"
#include "include/core/SkData.h"
#include "include/core/SkImage.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPathUtils.h"
#include "include/core/SkPixmap.h"
#include "include/core/SkRecorder.h"
#include "include/effects/SkImageFilters.h"
#include "include/effects/SkRuntimeEffect.h"
#include <algorithm>
#include <cmath>
#include <iterator>
#include <stdexcept>

namespace subtitle::skia {
namespace {
bool usesSoftwareCanvas(const SkCanvas& canvas) {
    const auto* recorder = canvas.baseRecorder();
    return !recorder || recorder->type() == SkRecorder::Type::kCPU;
}

SkIRect alphaBounds(const SkPixmap& mask) {
    auto ink = SkIRect::MakeEmpty();
    // Both N32 channel orders store alpha in the most significant byte.
    const auto hasAlpha = [](std::uint32_t pixel) { return SkColorGetA(pixel) != 0; };
    for (int row = 0; row < mask.height(); ++row) {
        const auto* begin = mask.addr32(0, row);
        const auto* end = begin + mask.width();
        const auto* first = std::find_if(begin, end, hasAlpha);
        if (first == end) continue;
        const auto last = std::find_if(std::make_reverse_iterator(end), std::make_reverse_iterator(first), hasAlpha);
        ink.join(SkIRect::MakeLTRB(int(first - begin), row, int(last.base() - begin), row + 1));
    }
    return ink;
}
}

sk_sp<SkColorFilter> glowStrengthFilter(float strength) {
    const float gain = std::clamp(std::isfinite(strength) ? strength : 1.f, 0.f, 10.f);
    if (gain == 1.f) return nullptr;
    // Applied to each blurred pass. Above x1 coverage scales by gain^alpha:
    // dense halo near the glyph follows the multiplier while the faint outer
    // tail (alpha near 0) barely moves, so the visible reach stays put. Mid
    // coverage keeps brightening all the way to x10 instead of clipping
    // early, and the dense core mixes toward white like an over-exposed
    // emissive surface. Below x1 the whole glow fades in proportion, so x0.5
    // reads as half the glow and x0 removes it.
    static const auto effect = SkRuntimeEffect::MakeForColorFilter(SkString(R"(
        uniform float gain;
        uniform float whitening;
        half4 main(half4 color) {
            float alpha = color.a;
            if (alpha <= 0.0) return half4(0);
            float3 rgb = color.rgb / alpha;
            float scaled = gain < 1.0 ? alpha * gain : min(1.0, alpha * pow(gain, alpha));
            rgb = mix(rgb, float3(1.0), whitening * scaled * scaled);
            return half4(rgb * scaled, scaled);
        }
    )"));
    if (!effect.effect) throw std::runtime_error("Skia 光晕强度滤镜无法编译");
    struct { float gain, whitening; } uniforms{gain, gain > 1.f ? std::min(.6f, .26f * std::log(gain)) : 0.f};
    auto filter = effect.effect->makeColorFilter(SkData::MakeWithCopy(&uniforms, sizeof(uniforms)));
    if (!filter) throw std::runtime_error("Skia 光晕强度滤镜初始化失败");
    return filter;
}

PreparedGlow::PreparedGlow(const StaticGlow& spec, const GlowState& state, std::optional<bool> phase)
    : before_(spec.before.color), after_(spec.after.color), radius_(state.radius),
      strokeWidth_(spec.outlineWidth + state.radius), join_(spec.join), phase_(phase) {
    const int passes = std::clamp(state.concentration, 0, 2) + 1;
    const auto strength = glowStrengthFilter(state.strength);
    for (int index = 0; index < passes; ++index) {
        const float sigma = float(radius_ - index * radius_ / passes);
        auto blur = SkImageFilters::Blur(sigma, sigma, SkTileMode::kDecal, nullptr);
        filters_.push_back(strength ? SkImageFilters::ColorFilter(strength, std::move(blur)) : std::move(blur));
    }
}

std::vector<PreparedGlow> PreparedGlow::prepare(const StaticGlow& spec, bool mergeMatchingPhases) {
    std::vector<PreparedGlow> layers;
    if (mergeMatchingPhases && spec.before.radius == spec.after.radius
        && spec.before.concentration == spec.after.concentration
        && spec.before.strength == spec.after.strength) {
        if (spec.before.radius > 0) layers.push_back(PreparedGlow(spec, spec.before, std::nullopt));
        return layers;
    }
    if (spec.before.radius > 0) layers.push_back(PreparedGlow(spec, spec.before, false));
    if (spec.after.radius > 0) layers.push_back(PreparedGlow(spec, spec.after, true));
    return layers;
}

SkPaint PreparedGlow::strokePaint() const {
    SkPaint stroke;
    stroke.setStyle(SkPaint::kStroke_Style);
    // Glyphs use round joins, but geometric lamps retain their pointed tips.
    stroke.setStrokeJoin(join_);
    if (join_ == SkPaint::kMiter_Join) stroke.setStrokeMiter(10.f);
    stroke.setStrokeWidth(strokeWidth_);
    return stroke;
}

void PreparedGlow::appendCell(const SkPath& path, const SkMatrix& peak, const GlowGradients& gradients,
                              const GlowImages& images) {
    if (path.isEmpty()) {
        cells_.emplace_back();
        return;
    }
    bool fill = false;
    auto outline = skpathutils::FillPathWithPaint(path, strokePaint(), &fill);
    if (!fill || !outline.isFinite()) throw std::runtime_error("Skia 字幕光晕轮廓无效");
    const auto bounds = outline.computeTightBounds();
    ink_.join(bounds);
    // Singing scales monotonically between identity and its peak. A scaled
    // outline bounds the fixed-width stroke of the scaled glyph from outside.
    if (!peak.isIdentity()) ink_.join(peak.mapRect(bounds));
    for (std::size_t phase = 0; phase < gradients.size(); ++phase)
        hasGradient_[phase] |= bool(gradients[phase]) || bool(images[phase]);
    cells_.push_back({std::move(outline), gradients, images});
}

SkRect PreparedGlow::sourceBounds() const {
    // Include a transparent filter margin in the source, even when downsampling.
    const float margin = 3.f * radius_ + 2.f;
    return ink_.isEmpty() ? ink_ : ink_.makeOutset(margin, margin);
}

sk_sp<SkShader> PreparedGlow::cellShader(const Cell& cell, bool after) const {
    if (cell.gradients[after]) return cell.gradients[after];
    return cell.images[after] ? cell.images[after].shaderAt(imageDevice_) : nullptr;
}

PreparedGlow::Source PreparedGlow::createSource(SkCanvas& canvas, const SkRect& transitionBounds,
                                               bool canonical) const {
    imageDevice_ = canvas.getTotalMatrix();
    auto source = sourceBounds();
    const float margin = 3.f * radius_ + 2.f;
    auto sampled = canvas.getLocalClipBounds().makeOutset(margin, margin);
    std::optional<SkRect> outputClip;
    if (!transitionBounds.isEmpty()) {
        const float expansion = 3.f * radius_ + 16.f;
        const float padding = expansion + strokeWidth_;
        // Preserve the halo samples in pixels intersected by the crop edge.
        outputClip = SkRect::Make(transitionBounds.makeOutset(padding, padding).roundOut());
        source = outputClip->makeOutset(expansion, expansion);
    }
    if (source.isEmpty() || !source.intersect(sampled)) return {};
    auto layer = makeSource(canvas, source, kN32_SkColorType, canonical);
    layer.outputClip = outputClip;
    return layer;
}

PreparedGlow::Source PreparedGlow::makeSource(SkCanvas& canvas, const SkRect& rectangle,
                                             SkColorType format, bool raster) const {
    const auto bounds = rectangle.roundOut();
    const auto colorSpace = canvas.imageInfo().refColorSpace();
    const auto info = SkImageInfo::Make(bounds.width(), bounds.height(), format, kPremul_SkAlphaType, colorSpace);
    auto target = raster ? SkSurfaces::Raster(info) : canvas.makeSurface(info);
    if (!target) throw std::runtime_error("Skia 字幕光晕临时表面分配失败");
    auto& layer = *target->getCanvas();
    layer.clear(SK_ColorTRANSPARENT);
    layer.translate(float(-bounds.left()), float(-bounds.top()));
    return {std::move(target), SkIPoint::Make(bounds.left(), bounds.top())};
}

bool PreparedGlow::visible(bool after) const {
    return (!phase_ || *phase_ == after) && (hasGradient_[after] || (after ? after_ : before_) >> 24);
}

void PreparedGlow::drawCell(SkCanvas& canvas, std::size_t index, bool after, std::optional<float> edge,
                            const SkPath* posed, float opacity, bool vertical, bool reverse, bool sampleCoverage) const {
    const auto& cell = cells_[index];
    const auto& path = posed ? *posed : cell.path;
    if (!visible(after) || path.isEmpty() || opacity <= 0) return;
    SkAutoCanvasRestore restore(&canvas, edge.has_value());
    if (edge) {
        const bool clipLow = after != reverse;
        const float low = clipLow ? -1e6f : *edge, high = clipLow ? *edge : 1e6f;
        const auto clip = vertical ? SkRect::MakeLTRB(-1e6f, low, 1e6f, high)
                                   : SkRect::MakeLTRB(low, -1e6f, high, 1e6f);
        // Split the blur source at pixel boundaries to keep the shared edge covered.
        canvas.clipRect(clip);
    }
    auto paint = posed ? strokePaint() : SkPaint{};
    paint.setAntiAlias(true);
    auto shader = cellShader(cell, after);
    paint.setColor(shader ? SK_ColorWHITE : after ? after_ : before_);
    if (opacity < 1.f) paint.setAlphaf(paint.getAlphaf() * opacity);
    if (sampleCoverage && posed && !shader) {
        paint.setStyle(SkPaint::kFill_Style);
        drawSamplePatternPath(canvas, prepareOutlinePath(*posed, strokeWidth_), paint, true);
    } else {
        paint.setShader(std::move(shader));
        canvas.drawPath(path, paint);
    }
}

void PreparedGlow::drawCellWithShader(SkCanvas& canvas, std::size_t index, bool after,
                                     const sk_sp<SkShader>& shader, float opacity) const {
    const auto& path = cells_[index].path;
    if (!shader || (phase_ && *phase_ != after) || path.isEmpty()) return;
    SkPaint paint;
    paint.setAntiAlias(true);
    paint.setShader(shader);
    paint.setAlphaf(opacity);
    canvas.drawPath(path, paint);
}

void PreparedGlow::prepareBytePasses() {
    // Subtle coverage can cross an 8-bit step between the stacked blur passes.
    // Apply the same quantization in Ganesh and Raster before each blend.
    static const auto effect = SkRuntimeEffect::MakeForColorFilter(SkString(R"(
        float4 main(float4 color) {
            return floor(color * 255.0 + 0.5) / 255.0;
        }
    )"));
    if (!effect.effect) throw std::runtime_error("Skia 光晕透明度滤镜无法编译");
    static const auto rounded = effect.effect->makeColorFilter(nullptr);
    byteFilters_.reserve(filters_.size());
    for (const auto& filter : filters_) byteFilters_.push_back(SkImageFilters::ColorFilter(rounded, filter));
}

void PreparedGlow::composite(SkCanvas& canvas, const Source& source, bool bytePasses, bool cropRaster) const {
    const bool crop = cropRaster && usesSoftwareCanvas(canvas) && canvas.getTotalMatrix().rectStaysRect();
    SkAutoCanvasRestore restore(&canvas, crop || source.outputClip.has_value());
    if (source.outputClip) canvas.clipRect(*source.outputClip);
    if (crop) {
        // Only part of the line has settled during a spin. Keep the original
        // source grid and restrict the software filter's output to its support.
        SkPixmap mask;
        if (!source.surface->peekPixels(&mask)) throw std::runtime_error("Skia 光晕表面无法取样");
        auto ink = alphaBounds(mask);
        if (ink.isEmpty()) return;
        ink.offset(source.origin.x(), source.origin.y());
        const auto transform = canvas.getTotalMatrix();
        auto deviceInk = transform.mapRect(SkRect::Make(ink)).roundOut();
        deviceInk.outset(1, 1); // Include the image's sampling margin in device pixels.
        const auto output = filters_.front()->filterBounds(deviceInk, transform, SkImageFilter::kForward_MapDirection);
        canvas.resetMatrix();
        canvas.clipRect(SkRect::Make(output));
        canvas.setMatrix(transform);
    }
    auto image = source.surface->makeImageSnapshot();
    if (!image) throw std::runtime_error("Skia 字幕光晕临时表面无法取样");
    SkPaint paint;
    for (const auto& filter : bytePasses ? byteFilters_ : filters_) {
        paint.setImageFilter(filter);
        canvas.drawImage(image, float(source.origin.x()), float(source.origin.y()), SkSamplingOptions(), &paint);
    }
}

bool PreparedGlow::cacheable(const SkCanvas& canvas) {
    // Only an integer translation keeps the blurred passes on the device
    // pixel grid, so blitting them repeats the direct composite exactly.
    const auto& matrix = canvas.getTotalMatrix();
    return matrix.isTranslate() && matrix.getTranslateX() == std::floor(matrix.getTranslateX())
        && matrix.getTranslateY() == std::floor(matrix.getTranslateY());
}

void PreparedGlow::blit(SkCanvas& canvas, const std::vector<CachedPass>& passes) {
    SkAutoCanvasRestore restore(&canvas, true);
    canvas.resetMatrix();
    // Source-over each pass in order, as composite() blends each filter.
    for (const auto& pass : passes)
        if (pass.image) canvas.drawImage(pass.image, float(pass.origin.x()), float(pass.origin.y()));
}

bool PreparedGlow::compositeCached(SkCanvas& canvas, const GlowCompositeKey& key) const {
    if (!cache_ || !(cache_->key == key)) return false;
    blit(canvas, cache_->passes);
    return true;
}

void PreparedGlow::compositeCapturing(SkCanvas& canvas, const Source& source, GlowCompositeKey key) const {
    cache_.reset();
    SkAutoCanvasRestore restore(&canvas, source.outputClip.has_value());
    if (source.outputClip) canvas.clipRect(*source.outputClip);
    auto image = source.surface->makeImageSnapshot();
    if (!image) throw std::runtime_error("Skia 字幕光晕临时表面无法取样");
    const auto matrix = canvas.getTotalMatrix();
    const auto deviceImage = matrix.mapRect(SkRect::MakeXYWH(float(source.origin.x()), float(source.origin.y()),
                                                              float(image->width()), float(image->height()))).roundOut();
    const auto clip = canvas.getDeviceClipBounds();
    // Evaluate each filter into a transparent device-aligned target with the
    // canvas transform, so the pass equals the filter output of a direct draw.
    const auto info = canvas.imageInfo();
    std::vector<CachedPass> passes;
    passes.reserve(filters_.size());
    for (const auto& filter : filters_) {
        auto output = filter->filterBounds(deviceImage, matrix, SkImageFilter::kForward_MapDirection);
        if (!output.intersect(clip)) {
            passes.push_back({});
            continue;
        }
        auto target = canvas.makeSurface(info.makeWH(output.width(), output.height()));
        if (!target) throw std::runtime_error("Skia 字幕光晕缓存表面分配失败");
        auto& layer = *target->getCanvas();
        layer.clear(SK_ColorTRANSPARENT);
        layer.setMatrix(SkMatrix::Translate(float(-output.left()), float(-output.top())) * matrix);
        SkPaint paint;
        paint.setImageFilter(filter);
        layer.drawImage(image, float(source.origin.x()), float(source.origin.y()), SkSamplingOptions(), &paint);
        auto pixels = target->makeImageSnapshot();
        if (!pixels) throw std::runtime_error("Skia 字幕光晕缓存表面无法取样");
        passes.push_back({std::move(pixels), output.topLeft()});
    }
    blit(canvas, passes);
    cache_ = CachedComposite{std::move(key), std::move(passes)};
}

void PreparedGlow::releaseCache() const { cache_.reset(); }

void PreparedGlow::compositeCanonical(SkCanvas& canvas, const Source& mask) const {
    const float x = float(mask.origin.x()), y = float(mask.origin.y());
    auto local = SkIRect::MakeWH(mask.surface->width(), mask.surface->height());
    if (usesSoftwareCanvas(canvas)) {
        // Keep software blur confined to the few remaining visible glyphs.
        // The input image retains the original origin and blur samples.
        SkPixmap maskPixels;
        if (!mask.surface->peekPixels(&maskPixels)) throw std::runtime_error("Skia 字幕光晕临时表面无法取样");
        auto ink = alphaBounds(maskPixels);
        if (ink.isEmpty()) return;
        const int support = 3 * radius_ + 2;
        ink.outset(support, support);
        if (!local.intersect(ink)) return;
    }
    local.offset(mask.origin.x(), mask.origin.y());
    const auto bounds = SkRect::Make(local);
    auto blurred = makeSource(canvas, bounds, kRGBA_F16_SkColorType);
    SkAutoCanvasRestore clip(&canvas, mask.outputClip.has_value());
    if (mask.outputClip) canvas.clipRect(*mask.outputClip);
    const auto input = mask.surface->makeImageSnapshot();
    if (!input) throw std::runtime_error("Skia 字幕光晕临时表面无法取样");
    SkPaint paint;
    auto* target = blurred.surface->getCanvas();
    // Filter one pass at a time and blend each into the destination.
    // Combining the passes here would defer the destination's rounding.
    for (const auto& filter : filters_) {
        target->clear(SK_ColorTRANSPARENT);
        paint.setImageFilter(filter);
        target->drawImage(input, x, y, SkSamplingOptions(), &paint);
        auto pixels = blurred.surface->makeImageSnapshot();
        if (!pixels) throw std::runtime_error("Skia 字幕光晕临时表面无法取样");
        canvas.drawImage(pixels, bounds.left(), bounds.top(), SkSamplingOptions());
    }
}

void PreparedGlow::compositeTransformedCell(SkCanvas& canvas, std::size_t index, bool after,
                                            std::optional<float> edge, const SkMatrix& transform, float opacity) const {
    const auto& path = cells_[index].path;
    if (!visible(after) || opacity <= 0 || path.isEmpty()) return;
    const float margin = 3.f * radius_ + 2.f;
    const auto bounds = path.getBounds().makeOutset(margin, margin);
    // A drip shears an opaque, unscaled glyph. Use the same floating-point
    // blur before the shear so dark glow edges agree on both devices.
    const bool opaqueShear = opacity == 1.f && transform.getScaleX() == 1.f && transform.getSkewY() != 0.f;
    const bool byteSource = usesSoftwareCanvas(canvas) && !opaqueShear;
    // Blur in glyph space before applying the animation transform.
    imageDevice_ = canvas.getTotalMatrix();
    auto mask = makeSource(canvas, bounds, byteSource ? kN32_SkColorType : kRGBA_F16_SkColorType, true);
    drawCell(*mask.surface->getCanvas(), index, after, edge, nullptr, byteSource ? 1.f : opacity);
    auto blurred = makeSource(canvas, bounds, kRGBA_F16_SkColorType);
    if (byteSource) {
        // Blur is linear in alpha. Filter at full brush opacity in N32,
        // then apply the animation alpha to each pass before F16 blending.
        auto pass = makeSource(canvas, bounds, kN32_SkColorType);
        const auto source = mask.surface->makeImageSnapshot();
        if (!source) throw std::runtime_error("Skia 逐字光晕临时表面无法取样");
        const float x = float(mask.origin.x()), y = float(mask.origin.y());
        SkPaint filterPaint, fade;
        fade.setAlphaf(opacity);
        for (const auto& filter : filters_) {
            pass.surface->getCanvas()->clear(SK_ColorTRANSPARENT);
            filterPaint.setImageFilter(filter);
            pass.surface->getCanvas()->drawImage(source, x, y, SkSamplingOptions{}, &filterPaint);
            auto filtered = pass.surface->makeImageSnapshot();
            if (!filtered) throw std::runtime_error("Skia 逐字光晕临时表面无法取样");
            blurred.surface->getCanvas()->drawImage(filtered, x, y, SkSamplingOptions{}, &fade);
        }
    } else {
        composite(*blurred.surface->getCanvas(), mask);
    }
    auto pixels = blurred.surface->makeImageSnapshot();
    if (!pixels) throw std::runtime_error("Skia 逐字光晕临时表面无法取样");
    const SkSamplingOptions linear(SkFilterMode::kLinear);
    // At steep spin angles a destination pixel spans many columns of the
    // upright image. Integrate the blurred light across the physical pixel.
    if (std::abs(transform.getScaleX()) < .125f) {
        const auto origin = SkMatrix::Translate(float(blurred.origin.x()), float(blurred.origin.y()));
        const auto imageToDevice = canvas.getTotalMatrix() * transform * origin;
        auto bounds = imageToDevice.mapRect(SkRect::MakeWH(float(pixels->width()), float(pixels->height()))).roundOut();
        if (!bounds.intersect(canvas.getDeviceClipBounds())) return;
        static const auto effect = SkRuntimeEffect::MakeForShader(SkString(R"(
            uniform shader glow;
            half4 main(float2 point) {
                float4 sum = float4(0);
                for (int y = 0; y < 4; ++y)
                    for (int x = 0; x < 4; ++x)
                        sum += glow.eval(point + (float2(x, y) - 1.5) * .25);
                return half4(sum * (1.0 / 16.0));
            }
        )"));
        if (!effect.effect) throw std::runtime_error(effect.errorText.c_str());
        SkRuntimeShaderBuilder builder(effect.effect);
        builder.child("glow") = pixels->makeShader(SkTileMode::kDecal, SkTileMode::kDecal, linear, &imageToDevice);
        auto coverage = builder.makeShader();
        if (!coverage) throw std::runtime_error("Skia 逐字光晕覆盖着色器初始化失败");
        SkPaint paint;
        paint.setShader(std::move(coverage));
        SkAutoCanvasRestore restore(&canvas, true);
        canvas.resetMatrix();
        canvas.drawIRect(bounds, paint);
        return;
    }
    SkAutoCanvasRestore restore(&canvas, true);
    canvas.concat(transform);
    canvas.drawImage(pixels, float(blurred.origin.x()), float(blurred.origin.y()), linear);
}
}
