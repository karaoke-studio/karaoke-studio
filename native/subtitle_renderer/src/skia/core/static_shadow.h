#pragma once

#include "wipe_front.h"
#include "character_transform.h"
#include "solid_outline.h"
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
        drawAt(canvas, cells, ink, edge, complete, outlineWidth + secondaryWidth + 4.f);
    }

    template <class Cell>
    void drawAt(SkCanvas& canvas, const std::vector<Cell>& cells, const SkRect& ink,
                float edge, bool complete, float beforePad) const {
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
                const auto clip = SkRect::MakeLTRB(edge, -1e6f, ink.right() + beforePad, 1e6f);
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

    struct WipePose {
        CharacterTransformState transform;
        bool before = false;
        std::optional<float> edge;
    };

    template <class Cell, class PoseAt>
    void drawAnimated(SkCanvas& canvas, const std::vector<Cell>& cells, const SkRect& ink,
                      float edge, bool complete, float beforePad, PoseAt&& poseAt) const {
        if (!visible() || ink.isEmpty()) return;
        const float width = outlineWidth + secondaryWidth;
        const float pad = std::max(width, 2.f) + 4.f;
        for (bool sung : {false, true}) {
            const auto& state = sung ? after : before;
            if (!state.visible() || (sung && edge <= ink.left())) continue;
            const bool beforeOnly = !sung && !after.enabled;
            if (beforeOnly && complete) continue;
            SkAutoCanvasRestore side(&canvas, beforeOnly);
            if (beforeOnly)
                canvas.clipRect(SkRect::MakeLTRB(edge, -1e6f, ink.right() + beforePad, 1e6f), SkClipOp::kIntersect, true);
            for (std::size_t index = 0; index < cells.size(); ++index) {
                const auto& cell = cells[index];
                if (cell.path.isEmpty()) continue;
                const auto pose = poseAt(index);
                if (pose.transform.opacity <= 0.f || (sung && pose.before)) continue;
                const auto path = pose.transform.transformed ? cell.path.makeTransform(pose.transform.matrix) : cell.path;
                const auto offset = pose.transform.matrix.mapVector({state.x, state.y});
                SkAutoCanvasRestore restore(&canvas, true);
                if (sung && pose.edge) {
                    const auto box = path.computeTightBounds();
                    canvas.clipRect(SkRect::MakeLTRB(box.left() - pad, -1e6f, *pose.edge, 1e6f),
                                    SkClipOp::kIntersect, true);
                }
                canvas.translate(offset.x(), offset.y());
                SkPaint paint;
                paint.setAntiAlias(true);
                paint.setColor(state.color);
                paint.setAlphaf(paint.getAlphaf() * pose.transform.opacity);
                if (width > 0) {
                    if (pose.transform.transformed) drawFixedOutline(canvas, path, paint, width);
                    else if (cell.outerOutline) canvas.drawPath(cell.outerOutline->path, paint);
                    else if (cell.outline) canvas.drawPath(cell.outline->path, paint);
                }
                canvas.drawPath(path, paint);
            }
        }
    }
};
}
