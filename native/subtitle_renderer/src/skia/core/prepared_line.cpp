#include "core/prepared_scene.h"
#include "character_halo.h"
#include "character_wipe.h"
#include "stroke_flash.h"
#include "wipe_front.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include <algorithm>
#include <array>

namespace subtitle::skia {
void PreparedScene::drawLine(SkCanvas& canvas, const Line& line, int tMs, bool textVisible) const {
    SkPaint paint;
    paint.setAntiAlias(true);
    const auto animation = textVisible ? line.animation.at(tMs, line.startMs, line.endMs)
                                       : LineAnimation::State{1.f, 0.f, 0.f};
    if (animation.opacity <= 0.f) return;
    SkAutoCanvasRestore restore(&canvas, true);
    if (animation.dx != 0 || animation.dy != 0)
        canvas.translate(animation.dx, animation.dy);
    for (const auto& translation : line.translations) {
        if (translation.startMs <= tMs && tMs < translation.endMs) {
            canvas.translate(translation.x, translation.y);
            break;
        }
    }
    if (!textVisible) {
        drawSingParticles(canvas, line.particles, tMs, false);
        drawSingParticles(canvas, line.particles, tMs, true);
        return;
    }
    // Fade the composed line, preserving overlaps and translucent strokes.
    // Sheared characters leave the static paint bounds, so they fade in an unbounded layer.
    const bool geometry = line.characterGeometry.active();
    const bool movingCells = line.characterFade.transforms() || geometry;
    if (animation.opacity < 1.f && (movingCells || !line.paintBounds.isEmpty()))
        canvas.saveLayerAlphaf(movingCells ? nullptr : &line.paintBounds, animation.opacity);
    const auto rawProgress = [&](const Cell& cell) {
        if (line.wipeRightToLeft && tMs <= (cell.wipePoints.empty() ? cell.startMs : cell.wipePoints.front().timeMs))
            return 0.f;
        if (line.noWipe) {
            // The existing GPU's Before phase wins at the initial sample,
            // including a zero-duration cell. Switch on the next sample.
            const int start = cell.wipePoints.empty() ? cell.startMs : cell.wipePoints.front().timeMs;
            if (tMs <= start) return 0.f;
            if (!cell.wipePoints.empty())
                return tMs >= cell.wipePoints.back().timeMs
                    ? cell.wipePoints.back().position : cell.wipePoints.front().position;
            return tMs >= cell.endMs ? 1.f : 0.f;
        }
        return !cell.wipePoints.empty()
            ? krok::subtitle::native::effects::wipePositionAt(cell.wipePoints, tMs)
            : (cell.endMs == cell.startMs ? (tMs >= cell.startMs ? 1.f : 0.f)
               : std::clamp(float(double(tMs) - cell.startMs) / float(double(cell.endMs) - cell.startMs), 0.f, 1.f));
    };
    // Cells are already in singing order; this flag only chooses the physical front.
    // Vertical lines wipe along y over the fixed cell box (reverse: bottom to top).
    const auto low = [&](const Cell& cell) { return line.vertical ? cell.top : cell.left; };
    const auto high = [&](const Cell& cell) { return line.vertical ? cell.bottom : cell.right; };
    const auto front = [&](const Cell& cell, float progress) {
        return line.wipeRightToLeft ? high(cell)-(high(cell)-low(cell))*progress
                                : low(cell)+(high(cell)-low(cell))*progress;
    };
    const auto band = [&](float from, float to) {
        return line.vertical ? SkRect::MakeLTRB(-1e6f, from, 1e6f, to) : SkRect::MakeLTRB(from, -1e6f, to, 1e6f);
    };
    enum class Phase { Before, After, Wiping };
    struct State {
        float progress, edge;
        Phase phase;
        CharacterTransformState transform;
        std::optional<SkPath> animatedInk;
    };
    std::vector<State> states;
    states.reserve(line.cells.size());
    for (std::size_t index = 0; index < line.cells.size(); ++index) {
        const auto& cell = line.cells[index];
        float progress = rawProgress(cell);
        float edge = front(cell, progress);
        if (cell.adjustedWipeEnd) {
            const int start = cell.wipePoints.front().timeMs, end = cell.wipePoints.back().timeMs;
            const Cell* next = index+1 < line.cells.size() ? &line.cells[index+1] : nullptr;
            const int followingEnd = next ? (next->wipePoints.empty() ? next->endMs : next->wipePoints.back().timeMs) : end;
            const bool wiping = (start < tMs && tMs < end && start != end)
                || (next && start < tMs && tMs < followingEnd && start != followingEnd);
            if (!wiping) {
                progress = tMs <= start ? 0.f : 1.f;
                edge = front(cell, progress);
            } else if (tMs > end && next) {
                // N3 delegates the finishing cell's clip to its following
                // cell (next in singing order) until that cell completes.
                edge = front(*next, rawProgress(*next));
                const float covered = line.wipeRightToLeft ? high(cell)-edge : edge-low(cell);
                progress = std::clamp(covered/std::max(high(cell)-low(cell), 1.f), 0.f, 1.f);
            }
        }
        // The existing GPU's N3 phase classification, used only for paint order.
        const int start = wipeStart(cell), end = wipeEnd(cell);
        bool wiping = start < tMs && tMs < end && start != end;
        if (!wiping && index+1 < line.cells.size()) {
            const int followingEnd = wipeEnd(line.cells[index+1]);
            wiping = start < tMs && tMs < followingEnd && start != followingEnd;
        }
        const auto transform = !geometry || line.characterFade.side(tMs, line.startMs, line.endMs) != 0
            ? characterTransformAt(line.characterFade, tMs, line.startMs, line.endMs,
                                   int(index), int(line.cells.size()), cell.pivot)
            : line.characterGeometry.at(tMs, line.startMs, line.endMs,
                                        int(index), int(line.cells.size()), cell.pivot, line.inkCenter);
        std::optional<SkPath> animatedInk;
        if (transform.transformed && transform.opacity > 0
            && (cell.outline || cell.outerOutline || transform.glowAlpha > 0))
            animatedInk = cell.path.makeTransform(transform.matrix);
        states.push_back({progress, edge, wiping ? Phase::Wiping : (tMs <= start ? Phase::Before : Phase::After),
                          transform, std::move(animatedInk)});
    }
    enum class Layer { OuterOutline, Outline, Body };
    using PhaseShaders = std::array<sk_sp<SkShader>, 2>;
    const auto drawCell = [&](std::size_t index, Layer layer, const PhaseShaders& shaders) {
        const auto& cell = line.cells[index];
        const auto& stroke = layer == Layer::OuterOutline ? cell.outerOutline : cell.outline;
        const bool drawOutline = layer != Layer::Body;
        if (drawOutline && !stroke) return;
        const auto& path = drawOutline ? stroke->path : cell.path;
        const auto& [progress, edge, phase, transform, animatedInk] = states[index];
        const float opacity = transform.opacity;
        if (opacity <= 0) return;
        // The outline can reach beyond the integer wipe padding, just as
        // the vertical glyph can reach beyond its fixed cell.
        const bool animatedClip = line.transformedWipe && transform.transformed && phase == Phase::Wiping;
        float clipEdge = edge;
        if (animatedClip) {
            auto owner = index;
            if (tMs > wipeEnd(cell) && index + 1 < line.cells.size() && !line.cells[index + 1].path.isEmpty())
                owner = index + 1;
            const auto& clipCell = line.cells[owner];
            const auto& matrix = states[owner].transform.matrix;
            const float ratio = transformedWipeRatio(clipCell.wipePoints, clipCell.adjustedWipeEnd,
                                                     wipeStart(clipCell), wipeEnd(clipCell), tMs);
            clipEdge = transformedWipeEdge(line.cells.front().pivot.layoutLeft,
                matrix.mapPoint({clipCell.pivot.inkLeft, 0}).x(), matrix.mapPoint({clipCell.pivot.inkRight, 0}).x(),
                ratio, clipCell.pivot.wipePad);
        }
        const bool atFront = (line.vertical || drawOutline || animatedClip) && phase == Phase::Wiping;
        const bool before = !atFront && progress == 0;
        const bool after = !atFront && progress == 1;
        const auto draw = [&](bool sung) {
            if (drawOutline && stroke->protectBody) {
                canvas.clipPath(cell.path, SkClipOp::kDifference, true);
                if (layer == Layer::OuterOutline && cell.outline)
                    canvas.clipPath(cell.outline->path, SkClipOp::kDifference, true);
            }
            const auto& shader = shaders[sung];
            paint.setShader(shader);
            paint.setColor(shader ? SK_ColorWHITE : drawOutline ? (sung ? stroke->after : stroke->before)
                                                               : (sung ? cell.after : cell.before));
            if (opacity < 1.f) paint.setAlphaf(paint.getAlphaf() * opacity);
            if (animatedInk) {
                if (drawOutline) drawFixedOutline(canvas, *animatedInk, paint, stroke->width);
                else canvas.drawPath(*animatedInk, paint);
                return;
            }
            if (transform.transformed) canvas.concat(transform.matrix); // Clips stay in line space.
            canvas.drawPath(path, paint);
        };
        // Two disjoint clips avoid painting translucent after color over before color.
        if (!after) {
            canvas.save();
            canvas.clipRect(line.wipeRightToLeft
                ? band(-1e6f, before ? 1e6f : clipEdge)
                : band(before ? -1e6f : clipEdge, 1e6f),
                            SkClipOp::kIntersect, cell.adjustedWipeEnd || animatedClip);
            draw(false);
            canvas.restore();
        }
        if (!before) {
            canvas.save();
            canvas.clipRect(line.wipeRightToLeft
                ? band(after ? -1e6f : clipEdge, 1e6f)
                : band(-1e6f, after ? 1e6f : clipEdge),
                            SkClipOp::kIntersect, cell.adjustedWipeEnd || animatedClip);
            draw(true);
            canvas.restore();
        }
    };
    // Overlapping cells follow the existing GPU's per-character phase order:
    // unsung cells last-to-first, then sung cells, then wiping cells on top.
    // Each cell's own clip and colors are unchanged; only stacking differs.
    const auto drawLayer = [&](Layer layer, const ImageFill (&images)[2], PhaseShaders shaders) {
        for (std::size_t phase = 0; phase < shaders.size(); ++phase)
            if (images[phase]) shaders[phase] = images[phase].shaderAt(canvas.getTotalMatrix());
        for (std::size_t index = line.cells.size(); index > 0; --index)
            if (states[index-1].phase == Phase::Before) drawCell(index-1, layer, shaders);
        for (std::size_t index = 0; index < line.cells.size(); ++index)
            if (states[index].phase == Phase::After) drawCell(index, layer, shaders);
        for (std::size_t index = 0; index < line.cells.size(); ++index)
            if (states[index].phase == Phase::Wiping) drawCell(index, layer, shaders);
    };
    if (!line.particles.empty()) drawSingParticles(canvas, line.particles, tMs, false);
    if (geometry)
        for (std::size_t i = 0; i < line.cells.size(); ++i) {
            const auto& state = states[i];
            drawCharacterHalo(canvas, state.animatedInk ? *state.animatedInk : line.cells[i].path,
                              state.transform, line.characterGeometry.fontPx);
        }
    line.shadow.draw(canvas, line.cells, tMs, line.shadowInk);
    if (line.hasOuterOutline)
        drawLayer(Layer::OuterOutline, line.images.outer, {line.beforeOuterGradient, line.afterOuterGradient});
    if (line.hasOutline)
        drawLayer(Layer::Outline, line.images.outline, {line.beforeOutlineGradient, line.afterOutlineGradient});
    drawLayer(Layer::Body, line.images.body, {line.beforeGradient, line.afterGradient});
    // The GPU repaints the sweeping cells after the body.
    line.scanline.draw(canvas, line.cells, tMs, line.inkStart, line.inkTop, [&](std::size_t index, bool sung) {
        const auto& cell = line.cells[index];
        const float opacity = states[index].transform.opacity;
        if (opacity <= 0) return;
        const auto setColor = [&](std::uint32_t before, std::uint32_t after) {
            paint.setColor(line.scanline.paint.shade(before, after, sung));
            if (opacity < 1.f) paint.setAlphaf(paint.getAlphaf() * opacity);
        };
        for (const bool outer : {true, false}) {
            const auto& stroke = outer ? cell.outerOutline : cell.outline;
            if (!stroke) continue;
            SkAutoCanvasRestore keep(&canvas, true);
            if (stroke->protectBody) {
                canvas.clipPath(cell.path, SkClipOp::kDifference, true);
                if (outer && cell.outline) canvas.clipPath(cell.outline->path, SkClipOp::kDifference, true);
            }
            setColor(stroke->before, stroke->after);
            canvas.drawPath(stroke->path, paint);
        }
        setColor(cell.before, cell.after);
        canvas.drawPath(cell.path, paint);
    });
    if (line.hasStrokeFlash)
        for (const auto& cell : line.cells)
            drawStrokeFlash(canvas, cell.path, tMs - cell.startMs, cell.strokeFlashWidth);
    for (const auto& group : line.rubyGroups) group.draw(canvas, tMs);
    // The GPU's front particle layer covers the main text and readings.
    if (!line.particles.empty()) drawSingParticles(canvas, line.particles, tMs, true);
}
}
