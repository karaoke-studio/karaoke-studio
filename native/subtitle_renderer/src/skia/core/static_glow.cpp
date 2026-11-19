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

std::vector<PreparedGlow> PreparedGlow::prepare(const StaticGlow& spec) {
    std::vector<PreparedGlow> layers;
    if (spec.before.radius == spec.after.radius && spec.before.concentration == spec.after.concentration) {
        if (spec.before.radius > 0) layers.push_back(PreparedGlow(spec, spec.before, std::nullopt));
        return layers;
    }
    if (spec.before.radius > 0) layers.push_back(PreparedGlow(spec, spec.before, false));
    if (spec.after.radius > 0) layers.push_back(PreparedGlow(spec, spec.after, true));
    return layers;
}

void PreparedGlow::appendCell(const SkPath& path) {
    if (path.isEmpty()) {
        cells_.emplace_back();
        return;
    }
    SkPaint stroke;
    stroke.setStyle(SkPaint::kStroke_Style);
    stroke.setStrokeJoin(SkPaint::kMiter_Join);
    stroke.setStrokeMiter(10.f);
    stroke.setStrokeWidth(strokeWidth_);
    bool fill = false;
    auto outline = skpathutils::FillPathWithPaint(path, stroke, &fill);
    if (!fill || !outline.isFinite()) throw std::runtime_error("Skia 字幕光晕轮廓无效");
    ink_.join(outline.computeTightBounds());
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

void PreparedGlow::drawCell(SkCanvas& canvas, std::size_t index, bool after, std::optional<float> edge) const {
    const auto& path = cells_[index];
    if (!visible(after) || path.isEmpty()) return;
    SkAutoCanvasRestore restore(&canvas, edge.has_value());
    if (edge) {
        const auto clip = after ? SkRect::MakeLTRB(-1e6f, -1e6f, *edge, 1e6f)
                                : SkRect::MakeLTRB(*edge, -1e6f, 1e6f, 1e6f);
        canvas.clipRect(clip, SkClipOp::kIntersect, true);
    }
    SkPaint paint;
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
