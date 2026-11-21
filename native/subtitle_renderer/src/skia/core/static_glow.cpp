#include "static_glow.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkImage.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPathUtils.h"
#include "include/effects/SkImageFilters.h"
#include <algorithm>
#include <stdexcept>

namespace subtitle::skia {
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

PreparedGlow::Source PreparedGlow::createSource(SkCanvas& canvas, const SkRect& transitionBounds) const {
    auto source = sourceBounds();
    const float margin = 3.f * radius_ + 2.f;
    auto sampled = canvas.getLocalClipBounds().makeOutset(margin, margin);
    std::optional<SkRect> outputClip;
    if (!transitionBounds.isEmpty()) {
        const float expansion = 3.f * radius_ + 16.f;
        const float padding = expansion + strokeWidth_;
        outputClip = transitionBounds.makeOutset(padding, padding);
        source = outputClip->makeOutset(expansion, expansion);
    }
    if (source.isEmpty() || !source.intersect(sampled)) return {};
    auto layer = makeSource(canvas, source, false);
    layer.outputClip = outputClip;
    return layer;
}

PreparedGlow::Source PreparedGlow::makeSource(SkCanvas& canvas, const SkRect& rectangle, bool canonical) const {
    const auto bounds = rectangle.roundOut();
    const auto colorSpace = canvas.imageInfo().refColorSpace();
    const auto type = canonical ? kRGBA_F16_SkColorType : kN32_SkColorType;
    const auto info = SkImageInfo::Make(bounds.width(), bounds.height(), type, kPremul_SkAlphaType, colorSpace);
    auto target = canonical ? SkSurfaces::Raster(info) : canvas.makeSurface(info);
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
                            const SkPath* posed, float opacity, bool vertical, bool reverse) const {
    const auto& cell = cells_[index];
    const auto& path = posed ? *posed : cell.path;
    if (!visible(after) || path.isEmpty() || opacity <= 0) return;
    SkAutoCanvasRestore restore(&canvas, edge.has_value());
    if (edge) {
        const bool clipLow = after != reverse;
        const float low = clipLow ? -1e6f : *edge, high = clipLow ? *edge : 1e6f;
        const auto clip = vertical ? SkRect::MakeLTRB(-1e6f, low, 1e6f, high)
                                   : SkRect::MakeLTRB(low, -1e6f, high, 1e6f);
        canvas.clipRect(clip, SkClipOp::kIntersect, true);
    }
    auto paint = posed ? strokePaint() : SkPaint{};
    paint.setAntiAlias(true);
    paint.setColor(cell.gradients[after] ? SK_ColorWHITE : after ? after_ : before_);
    paint.setShader(cell.gradients[after]);
    if (opacity < 1.f) paint.setAlphaf(paint.getAlphaf() * opacity);
    canvas.drawPath(path, paint);
}

void PreparedGlow::composite(SkCanvas& canvas, const Source& source) const {
    auto image = source.surface->makeImageSnapshot();
    if (!image) throw std::runtime_error("Skia 字幕光晕临时表面无法取样");
    SkAutoCanvasRestore restore(&canvas, source.outputClip.has_value());
    if (source.outputClip) canvas.clipRect(*source.outputClip);
    SkPaint paint;
    for (const auto& filter : filters_) {
        paint.setImageFilter(filter);
        canvas.drawImage(image, float(source.origin.x()), float(source.origin.y()), SkSamplingOptions(), &paint);
    }
}

void PreparedGlow::compositeTransformedCell(SkCanvas& canvas, std::size_t index, bool after,
                                            std::optional<float> edge, const SkMatrix& transform, float opacity) const {
    const auto& path = cells_[index].path;
    if (!visible(after) || opacity <= 0 || path.isEmpty()) return;
    const float margin = 3.f * radius_ + 2.f;
    const auto bounds = path.getBounds().makeOutset(margin, margin);
    // Blur in glyph space first. A floating-point source keeps the
    // early and late alpha ramps consistent across Skia devices.
    auto mask = makeSource(canvas, bounds, true);
    drawCell(*mask.surface->getCanvas(), index, after, edge, nullptr, opacity);
    auto blurred = makeSource(canvas, bounds, true);
    composite(*blurred.surface->getCanvas(), mask);
    auto pixels = blurred.surface->makeImageSnapshot();
    if (!pixels) throw std::runtime_error("Skia 逐字光晕临时表面无法取样");
    SkAutoCanvasRestore restore(&canvas, true);
    canvas.concat(transform);
    canvas.drawImage(pixels, float(blurred.origin.x()), float(blurred.origin.y()),
                     SkSamplingOptions(SkFilterMode::kLinear));
}
}
