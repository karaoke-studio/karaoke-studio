#include "wipe_group.h"
#include "character_wipe.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace subtitle::skia {
namespace {
void paintRubyCell(SkCanvas& canvas, SkPaint& brush, const ResolvedCell& cell, bool after) {
    const auto outline = [&](const SolidOutline& stroke, bool outer) {
        SkAutoCanvasRestore restore(&canvas, stroke.protectBody);
        if (stroke.protectBody) {
            canvas.clipPath(cell.path, SkClipOp::kDifference, true);
            if (outer && cell.outline) canvas.clipPath(cell.outline->path, SkClipOp::kDifference, true);
        }
        brush.setColor(after ? stroke.after : stroke.before);
        canvas.drawPath(stroke.path, brush);
    };
    if (cell.outerOutline) outline(*cell.outerOutline, true);
    if (cell.outline) outline(*cell.outline, false);
    brush.setColor(after ? cell.after : cell.before);
    canvas.drawPath(cell.path, brush);
}
}

PreparedWipeGroup::PreparedWipeGroup(const std::vector<ResolvedCell>& cells, bool noWipe, bool vertical)
    : cells_(cells), noWipe_(noWipe), vertical_(vertical) {
    if (cells.empty() || cells.size() > 4096) throw std::runtime_error("Skia 注音组单元数量无效");
    int previousEnd = -172800000;
    bounds_ = SkRect::MakeEmpty();
    for (const auto& cell : cells_) {
        if (!cell.path.isFinite() || !std::isfinite(cell.left) || !std::isfinite(cell.right)
            || cell.right < cell.left || cell.startMs < previousEnd || cell.endMs < cell.startMs
            || cell.endMs > 172800000
            || cell.before != cells.front().before || cell.after != cells.front().after)
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
void PreparedWipeGroup::drawSinging(SkCanvas& canvas, int timeMs, const SingingScale& scale,
                                   float originX, bool afterVisible) const {
    SkPaint paint;
    paint.setAntiAlias(true);
    std::vector<std::optional<ResolvedCell>> animated(cells_.size());
    for (std::size_t index = 0; index < cells_.size(); ++index) {
        const auto& cell = cells_[index];
        if (cell.path.isEmpty()) continue;
        const auto transform = scale.at(timeMs, cell.startMs, cell.endMs, cell.pivot);
        if (!transform.transformed) continue;
        auto& visual = animated[index].emplace(cell);
        visual.path = cell.path.makeTransform(transform.matrix);
        if (visual.outline) visual.outline->path = prepareOutlinePath(visual.path, visual.outline->width);
        if (visual.outerOutline)
            visual.outerOutline->path = prepareOutlinePath(visual.path, visual.outerOutline->width);
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
                const SkRect ink = cell.path.computeTightBounds();
                const float progress = cell.wipePoints.empty()
                    ? float(timeMs - cell.startMs) / (cell.endMs - cell.startMs)
                    : krok::subtitle::native::effects::wipePositionAt(cell.wipePoints, timeMs);
                const float edge = transformedWipeEdge(originX, ink.left(), ink.right(), progress, cell.pivot.wipePad);
                const auto clip = after ? SkRect::MakeLTRB(-1e6f, -1e6f, edge, 1e6f)
                                        : SkRect::MakeLTRB(edge, -1e6f, 1e6f, 1e6f);
                canvas.clipRect(clip, SkClipOp::kIntersect, true);
            }
            paintRubyCell(canvas, paint, cell, after);
        }
    }
}

void PreparedWipeGroup::draw(SkCanvas& canvas, int timeMs, const SingingScale& scale, float originX) const {
    if (bounds_.isEmpty()) return;
    SkPaint paint;
    paint.setAntiAlias(true);
    const auto draw = [&](bool after) {
        for (const auto& cell : cells_) paintRubyCell(canvas, paint, cell, after);
    };
    const bool before = std::all_of(cells_.begin(), cells_.end(), [&](const auto& c) { return timeMs <= c.startMs; });
    const bool complete = std::all_of(cells_.begin(), cells_.end(), [&](const auto& c) { return timeMs >= c.endMs; });
    const float startEdge = vertical_ ? bounds_.top() : bounds_.left();
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
    if (scale.active) {
        drawSinging(canvas, timeMs, scale, originX, edge > startEdge);
        return;
    }
    const auto clip = [&](bool after) {
        const float low = after ? -1e6f : edge;
        const float high = after ? edge : 1e6f;
        canvas.clipRect(vertical_ ? SkRect::MakeLTRB(-1e6f, low, 1e6f, high)
                                  : SkRect::MakeLTRB(low, -1e6f, high, 1e6f),
                        SkClipOp::kIntersect, antialiasClip_);
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
    // Disjoint clips preserve translucent alpha; completed groups release
    // their clip so overhangs keep the established full-after appearance.
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
