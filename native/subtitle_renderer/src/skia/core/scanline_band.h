#pragma once

#include "scanline_feather.h"
#include "scanline_image.h"
#include "scanline_paint.h"
#include "scanline_shape.h"
#include "wipe_front.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkRect.h"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <memory>
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
    bool rubyHighlight = false;
    bool vertical = false;
    ScanlineShape shape;
    std::shared_ptr<const ScanlineImage> image;

    bool repaintsGlyphs() const { return !image || image->showBand; }

    // A procedural shape fades the ordinary band with its display-window fade.
    float alphaAt(int t) const { return shape ? shape.fadeAt(t) : 1.f; }

    template <class Cell> static bool sweeping(const Cell& c, int t) {
        const int start = wipeStart(c), end = wipeEnd(c);
        return start != end && start <= t && t <= end;
    }
    // The GPU's wipeEdge, or nothing once the line is complete or nothing is sung.
    template <class Cell>
    std::optional<float> frontAt(const std::vector<Cell>& cells, int t, float inkStart) const {
        if (!enabled || !paint.visible() || cells.empty() || lineWipeComplete(cells, t))
            return std::nullopt;
        const float edge = lineWipeFront(cells, t, inkStart, restingPad, rightToLeft, vertical);
        if (rightToLeft ? edge >= inkStart : edge <= inkStart) return std::nullopt;
        return edge;
    }
    // The annotation pass repaints each phase with physical-width feather slices.
    template <class PaintSlice>
    void drawSlices(SkCanvas& canvas, float front, PaintSlice&& paintSlice) const {
        const int count = std::clamp(int(std::lround(halfWidth * 2.f)), 1, 64);
        const float step = halfWidth * 2.f / count;
        for (const bool after : {false, true}) {
            for (int layer = 0; layer < 3; ++layer) {
                for (int i = 0; i < count; ++i) {
                    const float left = -halfWidth + i * step;
                    const float distance = std::abs(left + step * .5f);
                    const float opacity = softness > 0 ? std::clamp((halfWidth - distance) / softness, 0.f, 1.f) : 1.f;
                    const float lo = front + (after ? left : std::max(left, 0.f));
                    const float hi = front + (after ? std::min(left + step, 0.f) : left + step);
                    if (lo >= hi || opacity <= 0) continue;
                    SkAutoCanvasRestore keep(&canvas, true);
                    clipScanlineSlice(canvas, lo, hi, vertical);
                    paintSlice(layer, after, opacity * opacity * (3.f - 2.f * opacity));
                }
            }
        }
    }
    // Utopia singing: the line front only gates the band. Each sweeping cell
    // repaints its posed layers around its own front. Horizontal glyphs
    // use their transformed edge, while vertical glyphs use a fixed cell.
    template <class Cell, class EdgeOf, class PaintLayer>
    void drawPosed(SkCanvas& canvas, const std::vector<Cell>& cells, int t, float inkStart,
                   EdgeOf&& edgeOf, PaintLayer&& paintLayer) const {
        if (!repaintsGlyphs() || !frontAt(cells, t, inkStart)) return;
        for (std::size_t i = 0; i < cells.size(); ++i) {
            const auto& c = cells[i];
            if (!sweeping(c, t)) continue;
            const float edge = edgeOf(i);
            const float start = vertical ? c.top : c.left;
            const float end = vertical ? c.bottom : c.right;
            if (!(edge - halfWidth < end + solidPad && start - solidPad < edge + halfWidth)) continue;
            drawSlices(canvas, edge, [&](int layer, bool after, float opacity) { paintLayer(i, layer, after, opacity); });
        }
    }
    // paintCell(index, sung) repaints one cell's stack with the palette side.
    template <class Cell, class PaintCell>
    void draw(SkCanvas& canvas, const std::vector<Cell>& cells, int t, float inkStart, float inkTop, PaintCell&& paintCell) const {
        const auto front = frontAt(cells, t, inkStart);
        if (!front || !repaintsGlyphs()) return;
        const float bandStart = *front - halfWidth, bandEnd = *front + halfWidth;
        const SkRect band = vertical ? SkRect::MakeLTRB(-1e6f, bandStart, 1e6f, bandEnd)
                                    : SkRect::MakeLTRB(bandStart, -1e6f, bandEnd, 1e6f);
        SkAutoCanvasRestore restore(&canvas, false);
        SkRect layer = vertical ? band.makeOutset(0.f, 1.f) : band.makeOutset(1.f, 0.f);
        if (softness > 0) {
            if (!layer.intersect(canvas.getLocalClipBounds())) return;
            canvas.saveLayer(layer, nullptr);
        }
        for (std::size_t i = 0; i < cells.size(); ++i) {
            const auto& c = cells[i];
            const float cellStart = vertical ? c.top : c.left;
            const float cellEnd = vertical ? c.bottom : c.right;
            if (!sweeping(c, t) || !(bandStart < cellEnd + solidPad && cellStart - solidPad < bandEnd)) continue;
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
                    const auto phase = vertical ? SkRect::MakeLTRB(band.fLeft, lo, band.fRight, hi)
                                                : SkRect::MakeLTRB(lo, band.fTop, hi, band.fBottom);
                    canvas.clipRect(phase, SkClipOp::kIntersect, true);
                    paintCell(i, sung);
                }
            } else {
                paintCell(i, false);
            }
            canvas.restore();
        }
        if (softness > 0) {
            const float maskLeft = (vertical ? layer.fLeft : band.fLeft) - solidPad;
            const float maskTop = (vertical ? band.fTop : inkTop) - solidPad;
            applyScanlineFeather(canvas, layer, *front, halfWidth, softness, maskLeft, maskTop, vertical);
        }
    }
};
}
