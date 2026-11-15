#include "wipe_group.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace subtitle::skia {
PreparedWipeGroup::PreparedWipeGroup(const std::vector<ResolvedCell>& cells) : cells_(cells) {
    if (cells.empty() || cells.size() > 4096) throw std::runtime_error("Skia 注音组单元数量无效");
    int previousEnd = -172800000;
    bounds_ = SkRect::MakeEmpty();
    for (const auto& cell : cells_) {
        if (!cell.path.isFinite() || !std::isfinite(cell.left) || !std::isfinite(cell.right)
            || cell.right < cell.left || cell.startMs < previousEnd || cell.endMs < cell.startMs
            || cell.endMs > 172800000 || !cell.wipePoints.empty()
            || cell.before != cells.front().before || cell.after != cells.front().after)
            throw std::runtime_error("Skia 注音组几何、时钟或配色无效");
        previousEnd = cell.endMs;
        bounds_.join(cell.path.getBounds());
    }
}
void PreparedWipeGroup::draw(SkCanvas& canvas, int timeMs) const {
    if (bounds_.isEmpty()) return;
    SkPaint paint;
    paint.setAntiAlias(true);
    const auto draw = [&](bool after) {
        paint.setColor(after ? cells_.front().after : cells_.front().before);
        for (const auto& cell : cells_) canvas.drawPath(cell.path, paint);
    };
    const bool before = std::all_of(cells_.begin(), cells_.end(), [&](const auto& c) { return timeMs <= c.startMs; });
    const bool complete = std::all_of(cells_.begin(), cells_.end(), [&](const auto& c) { return timeMs >= c.endMs; });
    // Before wins at a wholly zero-duration group's initial timestamp.
    if (before) { draw(false); return; }
    if (complete) { draw(true); return; }
    float edge = bounds_.left();
    for (const auto& cell : cells_) {
        if (timeMs < cell.startMs) break;
        const float progress = timeMs <= cell.startMs ? 0.f : (timeMs >= cell.endMs ? 1.f
            : float(timeMs - cell.startMs) / float(cell.endMs - cell.startMs));
        edge = cell.left + (cell.right - cell.left) * progress;
        if (timeMs <= cell.endMs) break;
    }
    // Disjoint clips preserve translucent alpha; completed groups release
    // their clip so overhangs keep the established full-after appearance.
    canvas.save();
    canvas.clipRect(SkRect::MakeLTRB(edge, -1e6f, 1e6f, 1e6f));
    draw(false);
    canvas.restore();
    if (edge > bounds_.left()) {
        canvas.save();
        canvas.clipRect(SkRect::MakeLTRB(-1e6f, -1e6f, edge, 1e6f));
        draw(true);
        canvas.restore();
    }
}
}
