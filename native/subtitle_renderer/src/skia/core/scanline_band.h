#pragma once

#include "../../effects/wipe_clock.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkRect.h"
#include <algorithm>
#include <cstdint>
#include <optional>
#include <vector>

namespace subtitle::skia {
// scanline_band_schema=1: the existing GPU's forward, horizontal, solid-color
// band without glow. The band follows the GPU's single line-level wipe front;
// only cells whose closed wipe window contains the sample repaint inside it,
// so timing gaps and pauses carry no band. Clocks are the cells' own resolved
// clocks; nothing is retimed here.
struct ScanlineBand {
    bool enabled = false;
    float halfWidth = .5f;          // max(width, 1) / 2 in physical pixels
    std::uint32_t argb = 0xffffffff;
    float solidPad = 4;             // primary + secondary outline width + 4
    float restingPad = .5f;         // secondary outline width / 2 + 0.5

    template <class Cell> static int wipeStart(const Cell& c) {
        return c.wipePoints.empty() ? c.startMs : c.wipePoints.front().timeMs;
    }
    template <class Cell> static int wipeEnd(const Cell& c) {
        return c.wipePoints.empty() ? c.endMs : c.wipePoints.back().timeMs;
    }
    // The GPU front coordinate of one cell; a zero-length cell divides by one.
    template <class Cell> static float coordinate(const Cell& c, int t) {
        const float position = c.wipePoints.empty()
            ? std::clamp(float(t - c.startMs) / float(std::max(c.endMs - c.startMs, 1)), 0.f, 1.f)
            : krok::subtitle::native::effects::wipePositionAt(c.wipePoints, t);
        return c.left + (c.right - c.left) * position;
    }
    template <class Cell> static bool sweeping(const Cell& c, int t) {
        const int start = wipeStart(c), end = wipeEnd(c);
        return start != end && start <= t && t <= end;
    }
    // The GPU's wipeEdge, or nothing once the line is complete or nothing is sung.
    template <class Cell>
    std::optional<float> frontAt(const std::vector<Cell>& cells, int t, float inkLeft) const {
        if (!enabled || cells.empty()
            || std::all_of(cells.begin(), cells.end(), [t](const Cell& c) { return t >= wipeEnd(c); }))
            return std::nullopt;
        float edge = inkLeft;
        for (std::size_t i = 0; i < cells.size(); ++i) {
            if (t < wipeStart(cells[i])) {
                if (i > 0) edge = std::max(edge, coordinate(cells[i], t) - restingPad);
                break;
            }
            edge = coordinate(cells[i], t);
            if (t <= wipeEnd(cells[i])) break;
        }
        if (!(edge > inkLeft)) return std::nullopt;
        return edge;
    }
    // paintCell(index, argb) repaints one cell's outline stack in a uniform color.
    template <class Cell, class PaintCell>
    void draw(SkCanvas& canvas, const std::vector<Cell>& cells, int t, float inkLeft, PaintCell&& paintCell) const {
        const auto front = frontAt(cells, t, inkLeft);
        if (!front) return;
        const SkRect band = SkRect::MakeLTRB(*front - halfWidth, -1e6f, *front + halfWidth, 1e6f);
        for (std::size_t i = 0; i < cells.size(); ++i) {
            const auto& c = cells[i];
            if (!sweeping(c, t) || !(band.fLeft < c.right + solidPad && c.left - solidPad < band.fRight)) continue;
            canvas.save();
            canvas.clipRect(band, SkClipOp::kIntersect, true);
            paintCell(i, argb);
            canvas.restore();
        }
    }
};
}
