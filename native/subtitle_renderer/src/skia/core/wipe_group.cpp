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
                    float opacity = 1.f, sk_sp<SkShader> shader = {}) {
    const auto paint = [&](const SkPath& path, std::uint32_t before, std::uint32_t after) {
        brush.setShader(shader);
        brush.setColor(shader ? SK_ColorWHITE : color(before, after));
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

void paintRubyCell(SkCanvas& canvas, SkPaint& brush, const ResolvedCell& cell, bool after, float opacity,
                   const std::array<sk_sp<SkShader>, 3>& shaders) {
    for (int layer = 0; layer < 3; ++layer)
        paintRubyLayer(canvas, brush, cell, layer, [=](auto before, auto sung) { return after ? sung : before; },
                       opacity, shaders[layer]);
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

float singingFront(const ResolvedCell& cell, int timeMs, float originX, const SkRect& ink, bool rtl = false) {
    const float progress = cell.wipePoints.empty()
        ? float(timeMs - cell.startMs) / (cell.endMs - cell.startMs)
        : krok::subtitle::native::effects::wipePositionAt(cell.wipePoints, timeMs);
    return transformedWipeEdge(originX, ink.left(), ink.right(), progress, cell.pivot.wipePad, rtl);
}

CharacterTransformState readingExit(const UtopiaTransition& exit, const ResolvedCell& cell, int time, int done) {
    auto pivot = cell.pivot;
    // The reading layout retains its visual center separately from its left edge.
    pivot.layoutRight = 2 * pivot.layoutRight - pivot.layoutLeft;
    return exit.at(time, 0, 0, 0, done, pivot, {}, cell.startMs, cell.endMs);
}

CharacterTransformState readingMotion(const ResolvedCell& cell, int time, const SingingScale& scale,
                                      const UtopiaTransition* exit, int followingDone, std::optional<float> entryScale) {
    if (exit) return readingExit(*exit, cell, time, followingDone);
    if (!entryScale) return scale.at(time, cell.startMs, cell.endMs, cell.pivot);
    CharacterTransformState transform;
    transform.transformed = *entryScale != 1.f;
    transform.matrix = UtopiaTransition::entryScaleMatrix(cell.pivot, *entryScale);
    return transform;
}

SkMatrix readingDrip(const ResolvedCell& cell, const SkMatrix& pose) {
    const float shear = pose.getSkewY();
    return SkMatrix::MakeAll(1, 0, 0, shear, 1, -shear * cell.pivot.layoutRight, 0, 0, 1);
}

}

PreparedWipeGroup::PreparedWipeGroup(const ResolvedRubyGroup& group, bool noWipe, bool vertical,
                                     std::optional<std::size_t> glowOffset)
    : cells_(group.cells), shadow_(group.shadow), glowOffset_(glowOffset), fadeOwner_(group.fadeOwner),
      utopiaExitOwner_(group.utopiaExitOwner),
      noWipe_(noWipe), vertical_(vertical), rightToLeft_(group.rightToLeft),
      spinPivot_(group.spinPivot), affine_(group.affine), lower_(group.lower),
      drip_(group.drip),
      fillGradients_(group.fillGradients), fillImages_(group.fillImages),
      outlineGradients_(group.outlineGradients), outerOutlineGradients_(group.outerOutlineGradients),
      scanlineGradients_(group.scanlineGradients), shadowGradients_(group.shadowGradients) {
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

sk_sp<SkShader> PreparedWipeGroup::fillShader(const SkCanvas& canvas, bool after) const {
    return fillImages_[after] ? fillImages_[after].shaderAt(canvas.getTotalMatrix()) : fillGradients_[after];
}

void PreparedWipeGroup::drawSinging(SkCanvas& canvas, int timeMs, const SingingScale& scale,
                                   float originX, bool afterVisible, float opacity, float viewportHeight,
                                   const UtopiaTransition* exit, int followingDone, std::optional<float> entryScale) const {
    SkPaint paint;
    paint.setAntiAlias(true);
    std::vector<std::optional<ResolvedCell>> animated(cells_.size());
    float exitOpacity = 1.f;
    for (std::size_t index = 0; index < cells_.size(); ++index) {
        const auto& cell = cells_[index];
        if (cell.path.isEmpty()) continue;
        const auto transform = readingMotion(cell, timeMs, scale, exit, followingDone, entryScale);
        exitOpacity = transform.opacity;
        if (exitOpacity <= 0.f) return;
        animated[index] = posedUnit(cell, transform);
    }
    for (bool after : {false, true}) {
        if (after && !afterVisible) break;
        const std::array shaders{outerOutlineGradients_[after], outlineGradients_[after], fillShader(canvas, after)};
        for (std::size_t index = 0; index < cells_.size(); ++index) {
            const auto& cell = animated[index] ? *animated[index] : cells_[index];
            if (cell.path.isEmpty()) continue;
            const bool before = timeMs <= cell.startMs;
            const bool complete = !before && timeMs >= cell.endMs;
            if (after ? before : complete) continue;
            SkAutoCanvasRestore restore(&canvas, !before && !complete);
            if (!before && !complete) {
                const auto ink = cell.path.computeTightBounds();
                const float edge = exit ? transformedWipeEdge(originX, ink.left(), ink.right(), 1.f, cell.pivot.wipePad)
                                        : singingFront(cell, timeMs, originX, ink, rightToLeft_);
                const auto clip = after != rightToLeft_ ? SkRect::MakeLTRB(-1e6f, -1e6f, edge, 1e6f)
                                                       : SkRect::MakeLTRB(edge, -1e6f, 1e6f, 1e6f);
                clipViewportWipe(canvas, clip, viewportHeight);
            }
            paintRubyCell(canvas, paint, cell, after, opacity * exitOpacity, shaders);
        }
    }
}

void PreparedWipeGroup::drawGlowSource(SkCanvas& canvas, const PreparedGlow& glow, int timeMs, bool after,
                                      const SingingScale& scale, float originX, float opacity,
                                      const UtopiaTransition* exit, int followingDone,
                                      std::optional<float> entryScale) const {
    if (!glowOffset_ || bounds_.isEmpty() || opacity <= 0) return;
    if ((scale.active && !vertical_) || exit) {
        for (std::size_t index = 0; index < cells_.size(); ++index) {
            const auto& cell = cells_[index];
            const bool before = timeMs <= cell.startMs;
            const bool complete = !before && timeMs >= cell.endMs;
            if (cell.path.isEmpty() || (after ? before : complete)) continue;
            const auto transform = readingMotion(cell, timeMs, scale, exit, followingDone, entryScale);
            if (transform.opacity <= 0.f) continue;
            std::optional<SkPath> posed;
            if (transform.transformed) posed = cell.path.makeTransform(transform.matrix);
            std::optional<float> edge;
            if (!before && !complete) {
                const auto ink = (posed ? *posed : cell.path).computeTightBounds();
                edge = exit ? transformedWipeEdge(originX, ink.left(), ink.right(), 1.f, cell.pivot.wipePad)
                            : singingFront(cell, timeMs, originX, ink, rightToLeft_);
            }
            glow.drawCell(canvas, *glowOffset_ + index, after, edge, posed ? &*posed : nullptr,
                          opacity * transform.opacity, false, rightToLeft_, entryScale && opacity < .125f);
        }
        return;
    }
    const bool before = std::ranges::all_of(cells_, [=](const auto& cell) { return timeMs <= cell.startMs; });
    const bool complete = !before && lineWipeComplete(cells_, timeMs);
    if (after ? before : complete) return;
    const auto edge = before || complete ? std::nullopt : std::optional(frontAt(timeMs, wipeStart()));
    for (std::size_t i = 0; i < cells_.size(); ++i) {
        const auto& cell = cells_[i];
        const auto transform = scale.at(timeMs, cell.startMs, cell.endMs, cell.pivot);
        const auto posed = transform.transformed ? std::optional(cell.path.makeTransform(transform.matrix)) : std::nullopt;
        glow.drawCell(canvas, *glowOffset_ + i, after, edge, posed ? &*posed : nullptr, opacity, vertical_, rightToLeft_);
    }
}

void PreparedWipeGroup::compositeTransformedGlow(SkCanvas& canvas, const PreparedGlow& glow, int timeMs, bool after,
                                                float opacity, const SkMatrix& pose) const {
    if (!glowOffset_ || bounds_.isEmpty() || opacity <= 0) return;
    const bool before = timeMs <= cells_.front().startMs;
    const bool complete = !before && lineWipeComplete(cells_, timeMs);
    if (after ? before : complete) return;
    const auto edge = before || complete ? std::nullopt : std::optional(frontAt(timeMs, bounds_.left()));
    for (std::size_t index = 0; index < cells_.size(); ++index) {
        const auto matrix = drip_ ? readingDrip(cells_[index], pose) : pose;
        glow.compositeTransformedCell(canvas, *glowOffset_ + index, after, edge, matrix, opacity);
    }
}

void PreparedWipeGroup::drawShadow(SkCanvas& canvas, int timeMs, const SingingScale& scale, float originX) const {
    if (!shadow_.visible() || bounds_.isEmpty()) return;
    const float edge = frontAt(timeMs, wipeStart());
    const bool complete = lineWipeComplete(cells_, timeMs);
    const float pad = std::max(shadow_.outlineWidth + shadow_.secondaryWidth, 2.f) + 4.f;
    if (!scale.active) {
        shadow_.drawAt(canvas, cells_, bounds_, edge, complete, pad, vertical_, rightToLeft_, 1.f, shadowGradients_);
        return;
    }
    shadow_.drawAnimated(canvas, cells_, bounds_, edge, complete, pad, [&](std::size_t index) {
        const auto& cell = cells_[index];
        const auto transform = scale.at(timeMs, cell.startMs, cell.endMs, cell.pivot);
        const bool before = timeMs <= cell.startMs;
        std::optional<float> clip;
        if (!before && timeMs < cell.endMs)
            clip = vertical_ ? wipeCoordinate(cell, timeMs, rightToLeft_, true)
                             : singingFront(cell, timeMs, originX, transform.matrix.mapRect(cell.path.computeTightBounds()));
        return StaticShadow::WipePose{transform, before, clip};
    }, vertical_, vertical_ && rightToLeft_);
}

float PreparedWipeGroup::wipeStart() const {
    if (vertical_) return bounds_.top();
    return rightToLeft_ ? bounds_.right() : bounds_.left();
}

void PreparedWipeGroup::drawUtopiaExit(SkCanvas& canvas, int timeMs, int followingDone,
                                       const UtopiaTransition& transition, float originX, float viewportHeight) const {
    drawSinging(canvas, timeMs, {}, originX, true, 1.f, viewportHeight, &transition, followingDone);
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
        const float width = (high - low) * (progress * endpoint);
        edge = rightToLeft_ ? high - width : low + width;
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
    const float inkStart = vertical_ ? bounds_.top() : bounds_.left();
    const float inkEnd = vertical_ ? bounds_.bottom() : bounds_.right();
    SkPaint brush;
    brush.setAntiAlias(true);
    if (scale.active) {
        // Utopia singing: each unit inside its open wipe window carries its own
        // band at the posed unit's front, as the existing GPU's ruby pass does.
        for (const auto& source : cells_) {
            if (source.path.isEmpty() || !(source.startMs < timeMs && timeMs < source.endMs)) continue;
            const auto posed = posedUnit(source, scale.at(timeMs, source.startMs, source.endMs, source.pivot));
            const auto& cell = posed ? *posed : source;
            const float edge = vertical_ ? wipeCoordinate(source, timeMs, false, true)
                                        : singingFront(cell, timeMs, originX, cell.path.computeTightBounds());
            if (edge + band.halfWidth <= inkStart - pad || edge - band.halfWidth >= inkEnd + pad)
                continue;
            band.drawSlices(canvas, edge, [&](int layer, bool after, float opacity) {
                paintRubyLayer(canvas, brush, cell, layer,
                    [&](auto before, auto sung) { return band.paint.shade(before, sung, after); },
                    opacity * bandAlpha, scanlineGradients_[layer][after]);
            });
        }
        return;
    }
    const float front = frontAt(timeMs, inkStart);
    if (front + band.halfWidth <= inkStart - pad || front - band.halfWidth >= inkEnd + pad) return;
    for (const auto& cell : cells_) {
        if (cell.path.isEmpty() || !ScanlineBand::sweeping(cell, timeMs)) continue;
        band.drawSlices(canvas, front, [&](int layer, bool after, float opacity) {
            paintRubyLayer(canvas, brush, cell, layer,
                [&](auto before, auto sung) { return band.paint.shade(before, sung, after); },
                opacity * bandAlpha, scanlineGradients_[layer][after]);
        });
    }
}

void PreparedWipeGroup::draw(SkCanvas& canvas, int timeMs, const SingingScale& scale, float originX,
                             float opacity, float viewportHeight, const SkMatrix& pose, SkPoint ownerPivot,
                             std::optional<float> entryScale) const {
    if (bounds_.isEmpty() || opacity <= 0) return;
    const float startEdge = wipeStart();
    const float edge = frontAt(timeMs, startEdge);
    const bool revealed = rightToLeft_ ? edge < startEdge : edge > startEdge;
    if (scale.active && !vertical_) {
        drawSinging(canvas, timeMs, scale, originX, revealed, opacity, viewportHeight, nullptr, 0, entryScale);
        return;
    }
    SkPaint paint;
    paint.setAntiAlias(true);
    const auto draw = [&](bool after) {
        const std::array shaders{outerOutlineGradients_[after], outlineGradients_[after], fillShader(canvas, after)};
        for (const auto& cell : cells_) {
            const auto singing = scale.at(timeMs, cell.startMs, cell.endMs, cell.pivot);
            if (pose.isIdentity()) {
                const auto posed = posedUnit(cell, singing);
                paintRubyCell(canvas, paint, posed ? *posed : cell, after, opacity, shaders);
            } else {
                auto unitPose = pose;
                if (drip_) {
                    unitPose = readingDrip(cell, pose);
                } else if (affine_) {
                    // The same affine motion acts around each reading's own
                    // pivot, shifted by the host's translation. Utopia singing
                    // replaces layoutLeft with its scale origin; layoutRight
                    // keeps the unit's layout center (zoom lines never pose).
                    const SkVector delta{cell.pivot.layoutRight - ownerPivot.x(), cell.pivot.centerY - ownerPivot.y()};
                    const auto mapped = pose.mapVector(delta.x(), delta.y());
                    unitPose.postTranslate(delta.x() - mapped.x(), delta.y() - mapped.y());
                }
                const auto posed = posedUnit(cell, {opacity, unitPose, true});
                paintRubyCell(canvas, paint, *posed, after, opacity, shaders);
            }
        }
    };
    const bool before = std::all_of(cells_.begin(), cells_.end(), [&](const auto& c) { return timeMs <= c.startMs; });
    const bool complete = lineWipeComplete(cells_, timeMs);
    const auto clip = [&](bool after) {
        const bool clipLow = after != rightToLeft_;
        const float low = clipLow ? -1e6f : edge;
        const float high = clipLow ? edge : 1e6f;
        clipViewportWipe(canvas, vertical_ ? SkRect::MakeLTRB(-1e6f, low, 1e6f, high)
                                           : SkRect::MakeLTRB(low, -1e6f, high, 1e6f),
                         viewportHeight);
    };
    if (before) {
        draw(false);
        // D2D's no-wipe endpoint is already 1 at an initial zero-duration
        // unit. Its Before stack is uncut, and the After stack is also drawn
        // through that endpoint (or uncut if the whole group is complete).
        // This singular boundary intentionally preserves that alpha overlap.
        if (noWipe_ && cells_.front().startMs == cells_.front().endMs
            && timeMs == cells_.front().startMs && revealed) {
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
    if (revealed) {
        canvas.save();
        clip(true);
        draw(true);
        canvas.restore();
    }
}
}
