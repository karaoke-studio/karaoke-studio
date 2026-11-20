#include "wipe_group.h"
#include "character_wipe.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace subtitle::skia {
namespace {
template <class Color>
void paintRubyLayer(SkCanvas& canvas, SkPaint& brush, const ResolvedCell& cell, int layer, Color&& color,
                    float opacity = 1.f) {
    const auto paint = [&](const SkPath& path, std::uint32_t before, std::uint32_t after) {
        brush.setColor(color(before, after));
        if (opacity < 1.f) brush.setAlphaf(brush.getAlphaf() * opacity);
        canvas.drawPath(path, brush);
    };
    if (layer == 2) {
        paint(cell.path, cell.before, cell.after);
        return;
    }
    const auto& stroke = layer == 0 ? cell.outerOutline : cell.outline;
    if (!stroke) return;
    SkAutoCanvasRestore restore(&canvas, stroke->protectBody);
    if (stroke->protectBody) {
        canvas.clipPath(cell.path, SkClipOp::kDifference, true);
        if (layer == 0 && cell.outline) canvas.clipPath(cell.outline->path, SkClipOp::kDifference, true);
    }
    paint(stroke->path, stroke->before, stroke->after);
}

void paintRubyCell(SkCanvas& canvas, SkPaint& brush, const ResolvedCell& cell, bool after, float opacity) {
    for (int layer = 0; layer < 3; ++layer)
        paintRubyLayer(canvas, brush, cell, layer, [=](auto before, auto sung) { return after ? sung : before; }, opacity);
}

// Protected ruby rings scale with the glyph. Opaque outlines keep their width.
std::optional<ResolvedCell> posedUnit(const ResolvedCell& cell, const CharacterTransformState& transform) {
    if (!transform.transformed) return std::nullopt;
    ResolvedCell visual = cell;
    visual.path = cell.path.makeTransform(transform.matrix);
    for (auto* layer : {&visual.outline, &visual.outerOutline}) {
        if (*layer) {
            auto& outline = **layer;
            outline.path = outline.protectBody ? outline.path.makeTransform(transform.matrix)
                                               : prepareOutlinePath(visual.path, outline.width);
        }
    }
    return visual;
}

float singingFront(const ResolvedCell& cell, int timeMs, float originX, const SkRect& ink) {
    const float progress = cell.wipePoints.empty()
        ? float(timeMs - cell.startMs) / (cell.endMs - cell.startMs)
        : krok::subtitle::native::effects::wipePositionAt(cell.wipePoints, timeMs);
    return transformedWipeEdge(originX, ink.left(), ink.right(), progress, cell.pivot.wipePad);
}

}

PreparedWipeGroup::PreparedWipeGroup(const ResolvedRubyGroup& group, bool noWipe, bool vertical,
                                     std::optional<std::size_t> glowOffset)
    : cells_(group.cells), shadow_(group.shadow), glowOffset_(glowOffset), fadeOwner_(group.fadeOwner),
      noWipe_(noWipe), vertical_(vertical), spinPivot_(group.spinPivot), affine_(group.affine) {
    if (cells_.empty() || cells_.size() > 4096) throw std::runtime_error("Skia 注音组单元数量无效");
    int previousEnd = -172800000;
    bounds_ = SkRect::MakeEmpty();
    for (const auto& cell : cells_) {
        if (!cell.path.isFinite() || !std::isfinite(cell.left) || !std::isfinite(cell.right)
            || cell.right < cell.left || cell.startMs < previousEnd || cell.endMs < cell.startMs
            || cell.endMs > 172800000
            || cell.before != cells_.front().before || cell.after != cells_.front().after)
            throw std::runtime_error("Skia 注音组几何、时钟或配色无效");
        if (vertical_ && (!std::isfinite(cell.top) || !std::isfinite(cell.bottom) || cell.bottom <= cell.top))
            throw std::runtime_error("Skia 竖排注音走字范围无效");
        if (!cell.wipePoints.empty() && (cell.wipePoints.size() != 2
            || cell.wipePoints.front().timeMs != cell.startMs || cell.wipePoints.front().position != 0
            || cell.wipePoints.back().timeMs != cell.endMs || !std::isfinite(cell.wipePoints.back().position)
            || cell.wipePoints.back().position < 0 || cell.wipePoints.back().position > 1))
            throw std::runtime_error("Skia 注音组重叠终点无效");
        previousEnd = cell.endMs;
        antialiasClip_ = antialiasClip_ || cell.adjustedWipeEnd;
        bounds_.join(cell.path.getBounds());
    }
}

SkMatrix PreparedWipeGroup::spinMatrix(const CharacterTransformState& owner) const {
    if (!spinPivot_ || !owner.transformed || owner.opacity <= 0.f) return SkMatrix::I();
    const float scale = owner.matrix.getScaleX(), shear = owner.matrix.getSkewY();
    const auto [x, y] = *spinPivot_;
    return SkMatrix::MakeAll(scale, 0, x * (1.f - scale),
                             shear, scale, y * (1.f - scale) - shear * x, 0, 0, 1);
}

void PreparedWipeGroup::drawSinging(SkCanvas& canvas, int timeMs, const SingingScale& scale,
                                   float originX, bool afterVisible, float opacity, float viewportHeight) const {
    SkPaint paint;
    paint.setAntiAlias(true);
    std::vector<std::optional<ResolvedCell>> animated(cells_.size());
    for (std::size_t index = 0; index < cells_.size(); ++index) {
        const auto& cell = cells_[index];
        if (cell.path.isEmpty()) continue;
        animated[index] = posedUnit(cell, scale.at(timeMs, cell.startMs, cell.endMs, cell.pivot));
    }
    for (bool after : {false, true}) {
        if (after && !afterVisible) break;
        for (std::size_t index = 0; index < cells_.size(); ++index) {
            const auto& cell = animated[index] ? *animated[index] : cells_[index];
            if (cell.path.isEmpty()) continue;
            const bool before = timeMs <= cell.startMs;
            const bool complete = !before && timeMs >= cell.endMs;
            if (after ? before : complete) continue;
            SkAutoCanvasRestore restore(&canvas, !before && !complete);
            if (!before && !complete) {
                const float edge = singingFront(cell, timeMs, originX, cell.path.computeTightBounds());
                const auto clip = after ? SkRect::MakeLTRB(-1e6f, -1e6f, edge, 1e6f)
                                        : SkRect::MakeLTRB(edge, -1e6f, 1e6f, 1e6f);
                clipViewportWipe(canvas, clip, viewportHeight, true);
            }
            paintRubyCell(canvas, paint, cell, after, opacity);
        }
    }
}

void PreparedWipeGroup::drawGlowSource(SkCanvas& canvas, const PreparedGlow& glow, int timeMs, bool after,
                                      const SingingScale& scale, float originX, float opacity) const {
    if (!glowOffset_ || bounds_.isEmpty() || opacity <= 0) return;
    if (scale.active) {
        for (std::size_t index = 0; index < cells_.size(); ++index) {
            const auto& cell = cells_[index];
            const bool before = timeMs <= cell.startMs;
            const bool complete = !before && timeMs >= cell.endMs;
            if (cell.path.isEmpty() || (after ? before : complete)) continue;
            const auto transform = scale.at(timeMs, cell.startMs, cell.endMs, cell.pivot);
            std::optional<SkPath> posed;
            if (transform.transformed) posed = cell.path.makeTransform(transform.matrix);
            const auto edge = before || complete ? std::nullopt : std::optional(singingFront(
                cell, timeMs, originX, (posed ? *posed : cell.path).computeTightBounds()));
            glow.drawCell(canvas, *glowOffset_ + index, after, edge, posed ? &*posed : nullptr, opacity);
        }
        return;
    }
    const bool before = std::ranges::all_of(cells_, [=](const auto& cell) { return timeMs <= cell.startMs; });
    const bool complete = !before && lineWipeComplete(cells_, timeMs);
    if (after ? before : complete) return;
    const auto edge = before || complete ? std::nullopt : std::optional(frontAt(timeMs, bounds_.left()));
    for (std::size_t i = 0; i < cells_.size(); ++i)
        glow.drawCell(canvas, *glowOffset_ + i, after, edge, nullptr, opacity);
}

void PreparedWipeGroup::compositeSpinGlow(SkCanvas& canvas, const PreparedGlow& glow, int timeMs, bool after,
                                         float opacity, const SkMatrix& pose) const {
    if (!glowOffset_ || bounds_.isEmpty() || opacity <= 0) return;
    const bool before = timeMs <= cells_.front().startMs;
    const bool complete = !before && lineWipeComplete(cells_, timeMs);
    if (after ? before : complete) return;
    const auto edge = before || complete ? std::nullopt : std::optional(frontAt(timeMs, bounds_.left()));
    for (std::size_t index = 0; index < cells_.size(); ++index)
        glow.compositeTransformedCell(canvas, *glowOffset_ + index, after, edge, pose, opacity);
}

void PreparedWipeGroup::drawShadow(SkCanvas& canvas, int timeMs, const SingingScale& scale, float originX) const {
    if (!shadow_.visible() || bounds_.isEmpty()) return;
    const float edge = frontAt(timeMs, bounds_.left());
    const bool complete = lineWipeComplete(cells_, timeMs);
    const float pad = std::max(shadow_.outlineWidth + shadow_.secondaryWidth, 2.f) + 4.f;
    if (!scale.active) {
        shadow_.drawAt(canvas, cells_, bounds_, edge, complete, pad);
        return;
    }
    shadow_.drawAnimated(canvas, cells_, bounds_, edge, complete, pad, [&](std::size_t index) {
        const auto& cell = cells_[index];
        const auto transform = scale.at(timeMs, cell.startMs, cell.endMs, cell.pivot);
        const bool before = timeMs <= cell.startMs;
        std::optional<float> clip;
        if (!before && timeMs < cell.endMs)
            clip = singingFront(cell, timeMs, originX, transform.matrix.mapRect(cell.path.computeTightBounds()));
        return StaticShadow::WipePose{transform, before, clip};
    });
}

float PreparedWipeGroup::frontAt(int timeMs, float startEdge) const {
    float edge = startEdge;
    for (const auto& cell : cells_) {
        if (timeMs < cell.startMs) break;
        const float progress = noWipe_ ? (timeMs >= cell.endMs ? 1.f : 0.f)
            : timeMs <= cell.startMs ? 0.f : (timeMs >= cell.endMs ? 1.f
            : float(timeMs - cell.startMs) / float(cell.endMs - cell.startMs));
        const float endpoint = cell.wipePoints.empty() ? 1.f : cell.wipePoints.back().position;
        const float low = vertical_ ? cell.top : cell.left;
        const float high = vertical_ ? cell.bottom : cell.right;
        edge = low + (high - low) * (progress * endpoint);
        if (timeMs <= cell.endMs) break;
    }
    return edge;
}

void PreparedWipeGroup::drawScanline(SkCanvas& canvas, const ScanlineBand& band, int timeMs,
                                     const SingingScale& scale, float originX) const {
    if (bounds_.isEmpty()) return;
    const float bandAlpha = band.alphaAt(timeMs);
    if (bandAlpha <= 0.f) return;
    const float pad = std::max(shadow_.outlineWidth + shadow_.secondaryWidth, 2.f) + 4.f;
    SkPaint brush;
    brush.setAntiAlias(true);
    if (scale.active) {
        // Utopia singing: each unit inside its open wipe window carries its own
        // band at the posed unit's front, as the existing GPU's ruby pass does.
        for (const auto& source : cells_) {
            if (source.path.isEmpty() || !(source.startMs < timeMs && timeMs < source.endMs)) continue;
            const auto posed = posedUnit(source, scale.at(timeMs, source.startMs, source.endMs, source.pivot));
            const auto& cell = posed ? *posed : source;
            const float edge = singingFront(cell, timeMs, originX, cell.path.computeTightBounds());
            if (edge + band.halfWidth <= bounds_.left() - pad || edge - band.halfWidth >= bounds_.right() + pad)
                continue;
            band.drawSlices(canvas, edge, [&](int layer, bool after, float opacity) {
                paintRubyLayer(canvas, brush, cell, layer,
                    [&](auto before, auto sung) { return band.paint.shade(before, sung, after); },
                    opacity * bandAlpha);
            });
        }
        return;
    }
    const float front = frontAt(timeMs, bounds_.left());
    if (front + band.halfWidth <= bounds_.left() - pad || front - band.halfWidth >= bounds_.right() + pad) return;
    for (const auto& cell : cells_) {
        if (cell.path.isEmpty() || !ScanlineBand::sweeping(cell, timeMs)) continue;
        band.drawSlices(canvas, front, [&](int layer, bool after, float opacity) {
            paintRubyLayer(canvas, brush, cell, layer,
                [&](auto before, auto sung) { return band.paint.shade(before, sung, after); },
                opacity * bandAlpha);
        });
    }
}

void PreparedWipeGroup::draw(SkCanvas& canvas, int timeMs, const SingingScale& scale, float originX,
                             float opacity, float viewportHeight, const SkMatrix& pose, SkPoint ownerPivot) const {
    if (bounds_.isEmpty() || opacity <= 0) return;
    const float startEdge = vertical_ ? bounds_.top() : bounds_.left();
    const float edge = frontAt(timeMs, startEdge);
    if (scale.active) {
        drawSinging(canvas, timeMs, scale, originX, edge > startEdge, opacity, viewportHeight);
        return;
    }
    SkPaint paint;
    paint.setAntiAlias(true);
    const auto draw = [&](bool after) {
        for (const auto& cell : cells_) {
            if (pose.isIdentity()) {
                paintRubyCell(canvas, paint, cell, after, opacity);
            } else {
                auto unitPose = pose;
                if (affine_) {
                    // The same affine motion acts around each reading's own
                    // pivot, shifted by the host's translation.
                    const SkVector delta{cell.pivot.layoutLeft - ownerPivot.x(), cell.pivot.centerY - ownerPivot.y()};
                    const auto mapped = pose.mapVector(delta.x(), delta.y());
                    unitPose.postTranslate(delta.x() - mapped.x(), delta.y() - mapped.y());
                }
                const auto posed = posedUnit(cell, {opacity, unitPose, true});
                paintRubyCell(canvas, paint, *posed, after, opacity);
            }
        }
    };
    const bool before = std::all_of(cells_.begin(), cells_.end(), [&](const auto& c) { return timeMs <= c.startMs; });
    const bool complete = lineWipeComplete(cells_, timeMs);
    const auto clip = [&](bool after) {
        const float low = after ? -1e6f : edge;
        const float high = after ? edge : 1e6f;
        clipViewportWipe(canvas, vertical_ ? SkRect::MakeLTRB(-1e6f, low, 1e6f, high)
                                           : SkRect::MakeLTRB(low, -1e6f, high, 1e6f),
                         viewportHeight, antialiasClip_);
    };
    if (before) {
        draw(false);
        // D2D's no-wipe endpoint is already 1 at an initial zero-duration
        // unit. Its Before stack is uncut, and the After stack is also drawn
        // through that endpoint (or uncut if the whole group is complete).
        // This singular boundary intentionally preserves that alpha overlap.
        if (noWipe_ && cells_.front().startMs == cells_.front().endMs
            && timeMs == cells_.front().startMs && edge > startEdge) {
            canvas.save();
            if (!complete)
                clip(true);
            draw(true);
            canvas.restore();
        }
        return;
    }
    if (complete) { draw(true); return; }
    // Completed groups release the wipe clip so overhangs stay fully sung.
    canvas.save();
    clip(false);
    draw(false);
    canvas.restore();
    if (edge > startEdge) {
        canvas.save();
        clip(true);
        draw(true);
        canvas.restore();
    }
}
}
