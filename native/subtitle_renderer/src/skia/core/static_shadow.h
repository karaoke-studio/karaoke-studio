#pragma once

#include "wipe_front.h"
#include "character_transform.h"
#include "image_fill.h"
#include "solid_outline.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include "include/core/SkShader.h"
#include "include/effects/SkImageFilters.h"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <span>
#include <utility>

namespace subtitle::skia {
struct ShadowState {
    bool enabled = false;
    float x = 0, y = 0;
    std::uint32_t color = 0;
    float blur = 0, spread = 0;
    sk_sp<SkShader> shader;
    ImageFill image;

    bool visible() const { return enabled && (x != 0 || y != 0 || blur > 0 || spread > 0)
        && (shader || image || (color >> 24)); }
    bool soft() const { return blur > 0.f || spread > 0.f; }
    float extent() const { return visible() ? blur + spread : 0.f; }
};

inline void configureShadowBrush(SkPaint& brush, const ShadowState& state) {
    brush.setAntiAlias(true);
    if (state.spread > 0.f) {
        brush.setStyle(SkPaint::kStrokeAndFill_Style);
        brush.setStrokeWidth(state.spread * 2.f);
        brush.setStrokeJoin(SkPaint::kRound_Join);
        brush.setStrokeCap(SkPaint::kRound_Cap);
    }
}

template <class DrawSilhouette>
inline void drawShadowState(SkCanvas& canvas, const ShadowState& state, float opacity,
                            DrawSilhouette&& drawSilhouette) {
    if (!state.soft()) {
        SkPaint brush;
        brush.setAntiAlias(true);
        auto shader = state.image ? state.image.shaderAt(canvas.getTotalMatrix()) : state.shader;
        brush.setColor(shader ? SK_ColorWHITE : state.color);
        brush.setShader(std::move(shader));
        if (opacity < 1.f) brush.setAlphaf(brush.getAlphaf() * opacity);
        drawSilhouette(brush);
        return;
    }
    auto shader = state.image ? state.image.shaderAt(canvas.getTotalMatrix()) : state.shader;
    SkPaint layerPaint;
    if (state.blur > 0.f)
        layerPaint.setImageFilter(SkImageFilters::Blur(state.blur / 3.f, state.blur / 3.f,
                                                       SkTileMode::kDecal, nullptr));
    if (shader) {
        if (opacity < 1.f) layerPaint.setAlphaf(opacity);
        canvas.saveLayer(nullptr, &layerPaint);
        SkPaint maskBrush;
        configureShadowBrush(maskBrush, state);
        maskBrush.setColor(SK_ColorWHITE);
        drawSilhouette(maskBrush);
        SkPaint fillPaint;
        fillPaint.setAntiAlias(true);
        fillPaint.setBlendMode(SkBlendMode::kSrcIn);
        fillPaint.setShader(std::move(shader));
        canvas.drawPaint(fillPaint);
        canvas.restore();
        return;
    }
    const float alpha = (SkColorGetA(state.color) / 255.f) * opacity;
    if (alpha <= 0.f) return;
    layerPaint.setAlphaf(alpha);
    canvas.saveLayer(nullptr, &layerPaint);
    SkPaint solidBrush;
    configureShadowBrush(solidBrush, state);
    solidBrush.setColor(SkColorSetA(state.color, 255));
    drawSilhouette(solidBrush);
    canvas.restore();
}

struct StaticShadow {
    ShadowState before, after;
    float outlineWidth = 0, secondaryWidth = 0;

    bool visible() const { return before.visible() || after.visible(); }

    float maxExtent(bool sung, std::span<const StaticShadow> cellShadows = {}) const {
        float extent = (sung ? after : before).extent();
        for (const auto& cell : cellShadows)
            extent = std::max(extent, (sung ? cell.after : cell.before).extent());
        return extent;
    }

    template <class Cell> void draw(SkCanvas& canvas, const std::vector<Cell>& cells, int time, const SkRect& ink,
                                   bool vertical = false, bool reverse = false, float opacity = 1.f,
                                   bool noWipe = false, std::span<const StaticShadow> cellShadows = {}) const {
        if ((!visible() && cellShadows.empty()) || ink.isEmpty()) return;
        const bool complete = lineWipeComplete(cells, time);
        const float low = vertical ? ink.top() : ink.left(), high = vertical ? ink.bottom() : ink.right();
        // A reverse vertical before-only shadow keeps its first cell's
        // overhang before the whole-unit clock starts, just as it does during that unit.
        const bool initialReverseShadow = noWipe && vertical && reverse && before.enabled && !after.enabled
            && !cells.empty() && time < wipeStart(cells.front());
        const float edge = initialReverseShadow ? wipeCoordinate(cells.front(), time, reverse, vertical, noWipe)
            : lineWipeFront(cells, time, reverse ? high : low, secondaryWidth / 2 + .5f, reverse, vertical, noWipe);
        drawAt(canvas, cells, ink, edge, complete,
               outlineWidth + secondaryWidth + 4.f + maxExtent(false, cellShadows),
               vertical, reverse, opacity, nullptr, cellShadows);
    }

    template <class Cell>
    void drawAt(SkCanvas& canvas, const std::vector<Cell>& cells, const SkRect& ink,
                float edge, bool complete, float beforePad, bool vertical = false, bool reverse = false,
                float opacity = 1.f, float* previousReadingOffset = nullptr,
                std::span<const StaticShadow> cellShadows = {}) const {
        const auto silhouette = [&](const Cell& cell, const SkPaint& brush) {
            if (cell.outerOutline) canvas.drawPath(cell.outerOutline->path, brush);
            else if (cell.outline) canvas.drawPath(cell.outline->path, brush);
            canvas.drawPath(cell.path, brush);
        };
        const auto paint = [&](bool sung, const SkRect* clip) {
            if (!cellShadows.empty()) {
                SkAutoCanvasRestore keep(&canvas, clip != nullptr);
                if (clip) canvas.clipRect(*clip, SkClipOp::kIntersect, true);
                for (std::size_t index = 0; index < cells.size(); ++index) {
                    const auto& shadow = cellShadows[index];
                    const auto& state = sung ? shadow.after : shadow.before;
                    if (!state.visible() || cells[index].path.isEmpty()) continue;
                    SkAutoCanvasRestore positioned(&canvas, true);
                    canvas.translate(state.x, state.y);
                    drawShadowState(canvas, state, opacity, [&](const SkPaint& brush) {
                        silhouette(cells[index], brush);
                    });
                }
                return;
            }
            const auto& state = sung ? after : before;
            float clipOffset = 0;
            if (previousReadingOffset && state.enabled && (state.x != 0 || state.y != 0)) {
                // Horizontal reading clips are pushed under the preceding
                // shadow transform before applying this phase's offset.
                clipOffset = *previousReadingOffset - (sung ? state.x : 0.f);
                *previousReadingOffset = state.x;
            }
            if (!state.visible()) return;
            SkAutoCanvasRestore restore(&canvas, true);
            // The front clips the silhouette in the line's coordinates
            // before the shadow's own translation is applied.
            if (clip) canvas.clipRect(clip->makeOffset(clipOffset, 0.f), SkClipOp::kIntersect, true);
            canvas.translate(state.x, state.y);
            drawShadowState(canvas, state, opacity, [&](const SkPaint& brush) {
                for (const auto& cell : cells) silhouette(cell, brush);
            });
        };
        const auto band = [vertical](float low, float high) {
            return vertical ? SkRect::MakeLTRB(-1e6f, low, 1e6f, high)
                            : SkRect::MakeLTRB(low, -1e6f, high, 1e6f);
        };
        const float stroke = outlineWidth + secondaryWidth;
        const float start = vertical ? ink.top() : ink.left(), end = vertical ? ink.bottom() : ink.right();
        const float effectiveBeforePad = beforePad + (cellShadows.empty() ? before.extent() : maxExtent(false, cellShadows));
        if (before.enabled && !after.enabled) {
            if (!complete) {
                const auto clip = reverse ? band(start - effectiveBeforePad, edge) : band(edge, end + effectiveBeforePad);
                paint(false, &clip);
            }
        } else {
            paint(false, nullptr);
        }
        if (reverse ? edge < end : edge > start) {
            const float afterPad = std::max(stroke, 2.f) + 4.f + maxExtent(true, cellShadows);
            const auto clip = reverse ? band(edge, end + afterPad) : band(start - afterPad, edge);
            paint(true, complete ? nullptr : &clip);
        }
    }

    struct WipePose {
        CharacterTransformState transform;
        bool before = false;
        std::optional<float> edge;
    };

    template <class Cell, class PoseAt>
    void drawAnimated(SkCanvas& canvas, const std::vector<Cell>& cells, const SkRect& ink,
                      float edge, bool complete, float beforePad, PoseAt&& poseAt,
                      bool vertical = false, bool reverse = false,
                      std::span<const StaticShadow> cellShadows = {}) const {
        if ((!visible() && cellShadows.empty()) || ink.isEmpty()) return;
        const float low = vertical ? ink.top() : ink.left(), high = vertical ? ink.bottom() : ink.right();
        const auto band = [vertical](float start, float end) {
            return vertical ? SkRect::MakeLTRB(-1e6f, start, 1e6f, end)
                            : SkRect::MakeLTRB(start, -1e6f, end, 1e6f);
        };
        const float effectiveBeforePad = beforePad + maxExtent(false, cellShadows);
        for (bool sung : {false, true}) {
            if (sung && (reverse ? edge >= high : edge <= low)) continue;
            const bool beforeOnly = !sung && before.enabled && !after.enabled;
            if (beforeOnly && complete) continue;
            SkAutoCanvasRestore side(&canvas, beforeOnly);
            if (beforeOnly) {
                // A vertical pose can grow beyond the unposed ink box.
                const float lower = vertical ? -1e6f : low - effectiveBeforePad;
                const float upper = vertical ? 1e6f : high + effectiveBeforePad;
                canvas.clipRect(reverse ? band(lower, edge) : band(edge, upper), SkClipOp::kIntersect, true);
            }
            for (std::size_t index = 0; index < cells.size(); ++index) {
                const auto& cell = cells[index];
                if (cell.path.isEmpty()) continue;
                const auto& shadow = cellShadows.empty() ? *this : cellShadows[index];
                const auto& state = sung ? shadow.after : shadow.before;
                if (!state.visible()) continue;
                const float width = shadow.outlineWidth + shadow.secondaryWidth;
                const float pad = std::max(width, 2.f) + 4.f + state.extent();
                const auto pose = poseAt(index);
                if (pose.transform.opacity <= 0.f || (sung && pose.before)) continue;
                const auto path = pose.transform.transformed ? cell.path.makeTransform(pose.transform.matrix) : cell.path;
                const auto offset = pose.transform.matrix.mapVector({state.x, state.y});
                SkAutoCanvasRestore restore(&canvas, true);
                if (sung && pose.edge) {
                    const auto box = path.computeTightBounds();
                    const auto clip = reverse ? band(*pose.edge, (vertical ? box.bottom() : box.right()) + pad)
                                              : band((vertical ? box.top() : box.left()) - pad, *pose.edge);
                    canvas.clipRect(clip, SkClipOp::kIntersect, true);
                }
                canvas.translate(offset.x(), offset.y());
                drawShadowState(canvas, state, pose.transform.opacity, [&](const SkPaint& paint) {
                    if (width > 0) {
                        if (pose.transform.transformed) drawFixedOutline(canvas, path, paint, width);
                        else if (cell.outerOutline) canvas.drawPath(cell.outerOutline->path, paint);
                        else if (cell.outline) canvas.drawPath(cell.outline->path, paint);
                    }
                    canvas.drawPath(path, paint);
                });
            }
        }
    }
};
}
