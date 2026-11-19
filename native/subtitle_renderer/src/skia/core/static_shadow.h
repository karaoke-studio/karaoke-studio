#pragma once

#include "wipe_front.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include <cstdint>

namespace subtitle::skia {
struct ShadowState {
    bool enabled = false;
    float x = 0, y = 0;
    std::uint32_t color = 0;

    bool visible() const { return enabled && (x != 0 || y != 0) && (color >> 24); }
};

struct StaticShadow {
    ShadowState before, after;
    float outlineWidth = 0, secondaryWidth = 0;

    bool visible() const { return before.visible() || after.visible(); }

    template <class Cell> void draw(SkCanvas& canvas, const std::vector<Cell>& cells, int time, const SkRect& ink) const {
        if (!visible() || ink.isEmpty()) return;
        const bool complete = lineWipeComplete(cells, time);
        const float edge = lineWipeFront(cells, time, ink.left(), secondaryWidth / 2 + .5f, false);
        const auto paint = [&](const ShadowState& state, const SkRect* clip) {
            if (!state.visible()) return;
            SkAutoCanvasRestore restore(&canvas, true);
            // The front clips the silhouette in the line's coordinates
            // before the shadow's own translation is applied.
            if (clip) canvas.clipRect(*clip, SkClipOp::kIntersect, true);
            canvas.translate(state.x, state.y);
            SkPaint brush;
            brush.setAntiAlias(true);
            brush.setColor(state.color);
            for (const auto& cell : cells) {
                if (cell.outerOutline) canvas.drawPath(cell.outerOutline->path, brush);
                else if (cell.outline) canvas.drawPath(cell.outline->path, brush);
                canvas.drawPath(cell.path, brush);
            }
        };
        const float stroke = outlineWidth + secondaryWidth;
        if (before.enabled && !after.enabled) {
            if (!complete) {
                const auto clip = SkRect::MakeLTRB(edge, -1e6f, ink.right() + stroke + 4.f, 1e6f);
                paint(before, &clip);
            }
        } else {
            paint(before, nullptr);
        }
        if (edge > ink.left()) {
            const auto clip = SkRect::MakeLTRB(ink.left() - std::max(stroke, 2.f) - 4.f, -1e6f, edge, 1e6f);
            paint(after, complete ? nullptr : &clip);
        }
    }
};
}
