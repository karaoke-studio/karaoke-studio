#include "static_glow.h"
#include "solid_outline.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColorFilter.h"
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

PreparedGlow::PreparedGlow(const StaticGlow& spec, const GlowState& state, std::optional<bool> phase)
    : before_(spec.before.color), after_(spec.after.color), radius_(state.radius),
      strokeWidth_(spec.outlineWidth + state.radius), join_(spec.join), phase_(phase) {
    const int passes = std::clamp(state.concentration, 0, 2) + 1;
    for (int index = 0; index < passes; ++index) {
        const float sigma = float(radius_ - index * radius_ / passes);
        filters_.push_back(SkImageFilters::Blur(sigma, sigma, SkTileMode::kDecal, nullptr));
    }
}

std::vector<PreparedGlow> PreparedGlow::prepare(const StaticGlow& spec, bool mergeMatchingPhases) {
    std::vector<PreparedGlow> layers;
    if (mergeMatchingPhases && spec.before.radius == spec.after.radius
        && spec.before.concentration == spec.after.concentration) {
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

void PreparedGlow::appendCell(const SkPath& path, const SkMatrix& peak, const GlowGradients& gradients) {
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
        hasGradient_[phase] |= bool(gradients[phase]);
    cells_.push_back({std::move(outline), gradients});
}

SkRect PreparedGlow::sourceBounds() const {
    // Include a transparent filter margin in the source, even when downsampling.
    const float margin = 3.f * radius_ + 2.f;
    return ink_.isEmpty() ? ink_ : ink_.makeOutset(margin, margin);
}

PreparedGlow::Source PreparedGlow::createSource(SkCanvas& canvas, const SkRect& transitionBounds,
                                               bool canonical) const {
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
    paint.setColor(cell.gradients[after] ? SK_ColorWHITE : after ? after_ : before_);
    paint.setShader(cell.gradients[after]);
    if (opacity < 1.f) paint.setAlphaf(paint.getAlphaf() * opacity);
    if (sampleCoverage && posed && !cell.gradients[after]) {
        paint.setStyle(SkPaint::kFill_Style);
        drawSamplePatternPath(canvas, prepareOutlinePath(*posed, strokeWidth_), paint, true);
    } else {
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
