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
      strokeWidth_(spec.outlineWidth + state.radius), phase_(phase) {
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
    // The reference glyph paths request round joins. Miter spikes extend the
    // source beyond the intended radius before any blur is applied.
    stroke.setStrokeJoin(SkPaint::kRound_Join);
    stroke.setStrokeWidth(strokeWidth_);
    return stroke;
}

void PreparedGlow::appendCell(const SkPath& path, const SkMatrix& peak) {
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
    cells_.push_back(std::move(outline));
}

PreparedGlow::Source PreparedGlow::createSource(SkCanvas& canvas) const {
    // Include a transparent filter margin in the source, even when downsampling.
    const float margin = 3.f * radius_ + 2.f;
    auto source = ink_.makeOutset(margin, margin);
    auto sampled = canvas.getLocalClipBounds().makeOutset(margin, margin);
    if (ink_.isEmpty() || !source.intersect(sampled)) return {};
    const auto bounds = source.roundOut();
    auto target = canvas.makeSurface(SkImageInfo::MakeN32Premul(bounds.width(), bounds.height(),
                                                                canvas.imageInfo().refColorSpace()));
    if (!target) throw std::runtime_error("Skia 字幕光晕临时表面分配失败");
    auto& layer = *target->getCanvas();
    layer.clear(SK_ColorTRANSPARENT);
    layer.translate(float(-bounds.left()), float(-bounds.top()));
    return {std::move(target), SkIPoint::Make(bounds.left(), bounds.top())};
}

bool PreparedGlow::visible(bool after) const {
    return (!phase_ || *phase_ == after) && ((after ? after_ : before_) >> 24);
}

void PreparedGlow::drawCell(SkCanvas& canvas, std::size_t index, bool after, std::optional<float> edge,
                            const SkPath* posed) const {
    const auto& path = posed ? *posed : cells_[index];
    if (!visible(after) || path.isEmpty()) return;
    SkAutoCanvasRestore restore(&canvas, edge.has_value());
    if (edge) {
        const auto clip = after ? SkRect::MakeLTRB(-1e6f, -1e6f, *edge, 1e6f)
                                : SkRect::MakeLTRB(*edge, -1e6f, 1e6f, 1e6f);
        canvas.clipRect(clip, SkClipOp::kIntersect, true);
    }
    auto paint = posed ? strokePaint() : SkPaint{};
    paint.setAntiAlias(true);
    paint.setColor(after ? after_ : before_);
    canvas.drawPath(path, paint);
}

void PreparedGlow::composite(SkCanvas& canvas, const Source& source) const {
    auto image = source.surface->makeImageSnapshot();
    if (!image) throw std::runtime_error("Skia 字幕光晕临时表面无法取样");
    SkPaint paint;
    for (const auto& filter : filters_) {
        paint.setImageFilter(filter);
        canvas.drawImage(image, float(source.origin.x()), float(source.origin.y()), SkSamplingOptions(), &paint);
    }
}
}
