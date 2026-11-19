#pragma once

#include "scanline_feather.h"
#include "scanline_paint.h"
#include "../../effects/wipe_clock.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkRect.h"
#include <algorithm>
#include <cstdint>
#include <optional>
#include <vector>

namespace subtitle::skia {
// The horizontal solid-color band follows the existing GPU's
// single line-level wipe front;
// only cells whose closed wipe window contains the sample repaint inside it,
// so timing gaps and pauses carry no band. Clocks are the cells' own resolved
// clocks; nothing is retimed here.
struct ScanlineBand {
    bool enabled = false;
    float halfWidth = .5f;          // max(width, 1) / 2 in physical pixels
    ScanlinePaint paint;
    float solidPad = 4;             // primary + secondary outline width + 4
    float restingPad = .5f;         // secondary outline width / 2 + 0.5
    float softness = 0;            // Physical pixels, limited to the band half-width.
    bool rightToLeft = false;

    template <class Cell> static int wipeStart(const Cell& c) {
        return c.wipePoints.empty() ? c.startMs : c.wipePoints.front().timeMs;
    }
    template <class Cell> static int wipeEnd(const Cell& c) {
        return c.wipePoints.empty() ? c.endMs : c.wipePoints.back().timeMs;
    }
    // The GPU front coordinate of one cell; a zero-length cell divides by one.
    template <class Cell> float coordinate(const Cell& c, int t) const {
        const float position = c.wipePoints.empty()
            ? std::clamp(float(t - c.startMs) / float(std::max(c.endMs - c.startMs, 1)), 0.f, 1.f)
            : krok::subtitle::native::effects::wipePositionAt(c.wipePoints, t);
        return rightToLeft ? c.right - (c.right - c.left) * position
                           : c.left + (c.right - c.left) * position;
    }
    template <class Cell> static bool sweeping(const Cell& c, int t) {
        const int start = wipeStart(c), end = wipeEnd(c);
        return start != end && start <= t && t <= end;
    }
    // The GPU's wipeEdge, or nothing once the line is complete or nothing is sung.
    template <class Cell>
    std::optional<float> frontAt(const std::vector<Cell>& cells, int t, float inkStart) const {
        if (!enabled || !paint.visible() || cells.empty()
            || std::all_of(cells.begin(), cells.end(), [t](const Cell& c) { return t >= wipeEnd(c); }))
            return std::nullopt;
        float edge = inkStart;
        for (std::size_t i = 0; i < cells.size(); ++i) {
            if (t < wipeStart(cells[i])) {
                if (i > 0) {
                    const float start = coordinate(cells[i], t);
                    edge = rightToLeft ? std::min(edge, start + restingPad) : std::max(edge, start - restingPad);
                }
                break;
            }
            edge = coordinate(cells[i], t);
            if (t <= wipeEnd(cells[i])) break;
        }
        if (rightToLeft ? edge >= inkStart : edge <= inkStart) return std::nullopt;
        return edge;
    }
    // paintCell(index, sung) repaints one cell's stack with the palette side.
    template <class Cell, class PaintCell>
    void draw(SkCanvas& canvas, const std::vector<Cell>& cells, int t, float inkStart, float inkTop, PaintCell&& paintCell) const {
        const auto front = frontAt(cells, t, inkStart);
        if (!front) return;
        const SkRect band = SkRect::MakeLTRB(*front - halfWidth, -1e6f, *front + halfWidth, 1e6f);
        SkAutoCanvasRestore restore(&canvas, false);
        SkRect layer = band.makeOutset(1.f, 0.f);
        if (softness > 0) {
            if (!layer.intersect(canvas.getLocalClipBounds())) return;
            canvas.saveLayer(layer, nullptr);
        }
        for (std::size_t i = 0; i < cells.size(); ++i) {
            const auto& c = cells[i];
            if (!sweeping(c, t) || !(band.fLeft < c.right + solidPad && c.left - solidPad < band.fRight)) continue;
            canvas.save();
            canvas.clipRect(band, SkClipOp::kIntersect, true);
            if (paint.splitAtFront()) {
                // The GPU offsets before/after by +1/-1 in physical x.
                // The feather is applied to both passes at once.
                for (const bool sung : {false, true}) {
                    SkAutoCanvasRestore keep(&canvas, true);
                    const float edge = *front + (sung ? -1.f : 1.f);
                    const bool positiveSide = sung == rightToLeft;
                    const float lo = positiveSide ? edge : -1e6f;
                    const float hi = positiveSide ? 1e6f : edge;
                    canvas.clipRect(SkRect::MakeLTRB(lo, band.fTop, hi, band.fBottom), SkClipOp::kIntersect, true);
                    paintCell(i, sung);
                }
            } else {
                paintCell(i, false);
            }
            canvas.restore();
        }
        if (softness > 0)
            applyScanlineFeather(canvas, layer, *front, halfWidth, softness, band.fLeft - solidPad, inkTop - solidPad);
    }
};
}
