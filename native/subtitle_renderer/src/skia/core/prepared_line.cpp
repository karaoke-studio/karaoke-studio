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
    const bool utopia = line.singingScale.active || line.utopiaTransition.active();
    const bool movingCells = line.characterFade.transforms() || geometry || utopia;
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
        const auto transform = [&] {
            if (utopia) {
                const int following = line.utopiaTransition.leave ? line.followingDone[index] : 0;
                return line.utopiaTransition.at(tMs, line.startMs, int(index), int(line.cells.size()), following,
                                               cell.pivot, line.singingScale, start, end);
            }
            if (geometry && line.characterFade.side(tMs, line.startMs, line.endMs) == 0)
                return line.characterGeometry.at(tMs, line.startMs, line.endMs,
                                                 int(index), int(line.cells.size()), cell.pivot, line.inkCenter);
            return characterTransformAt(line.characterFade, tMs, line.startMs, line.endMs,
                                        int(index), int(line.cells.size()), cell.pivot);
        }();
        std::optional<SkPath> animatedInk;
        if (transform.transformed && transform.opacity > 0
            && (cell.outline || cell.outerOutline || transform.glowAlpha > 0 || !line.glow.empty()))
            animatedInk = cell.path.makeTransform(transform.matrix);
        states.push_back({progress, edge, wiping ? Phase::Wiping : (tMs <= start ? Phase::Before : Phase::After),
                          transform, std::move(animatedInk)});
    }
    enum class Layer { OuterOutline, Outline, Body };
    using PhaseShaders = std::array<sk_sp<SkShader>, 2>;
    const auto inPhaseOrder = [&](auto&& paintCell) {
        for (std::size_t index = line.cells.size(); index > 0; --index)
            if (states[index-1].phase == Phase::Before) paintCell(index-1, Phase::Before);
        for (std::size_t index = 0; index < line.cells.size(); ++index)
            if (states[index].phase == Phase::After) paintCell(index, Phase::After);
        for (std::size_t index = 0; index < line.cells.size(); ++index)
            if (states[index].phase == Phase::Wiping) paintCell(index, Phase::Wiping);
    };
    const auto transformedEdge = [&](std::size_t index) {
        auto owner = index;
        if (tMs > wipeEnd(line.cells[index]) && index + 1 < line.cells.size() && !line.cells[index + 1].path.isEmpty())
            owner = index + 1;
        const auto& cell = line.cells[owner];
        const auto& transform = states[owner].transform;
        const float ratio = transform.utopiaExit ? 1.f
            : utopia && tMs <= wipeEnd(cell) ? rawProgress(cell)
            : transformedWipeRatio(cell.wipePoints, cell.adjustedWipeEnd, wipeStart(cell), wipeEnd(cell), tMs);
        const float origin = utopia && !line.rubyGroups.empty()
            ? line.rubySingingOrigin : line.cells.front().pivot.layoutLeft;
        return transformedWipeEdge(origin, transform.matrix.mapPoint({cell.pivot.inkLeft, 0}).x(),
            transform.matrix.mapPoint({cell.pivot.inkRight, 0}).x(), ratio, cell.pivot.wipePad);
    };
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
        const bool animatedClip = (utopia || (line.transformedWipe && transform.transformed))
            && phase == Phase::Wiping;
        const float clipEdge = animatedClip ? transformedEdge(index) : edge;
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
        inPhaseOrder([&](std::size_t index, Phase) { drawCell(index, layer, shaders); });
    };
    if (!line.particles.empty()) drawSingParticles(canvas, line.particles, tMs, false);
    if (geometry)
        for (std::size_t i = 0; i < line.cells.size(); ++i) {
            const auto& state = states[i];
            drawCharacterHalo(canvas, state.animatedInk ? *state.animatedInk : line.cells[i].path,
                              state.transform, line.characterGeometry.fontPx);
        }
    for (const auto& layer : line.rubyGlow) {
        const bool after = layer.visible(true);
        if (!after && !layer.visible(false)) continue;
        const auto source = layer.createSource(canvas);
        if (!source.surface) continue;
        for (const auto& group : line.rubyGroups)
            group.drawGlowSource(*source.surface->getCanvas(), layer, tMs, after,
                                 line.singingScale, line.rubySingingOrigin);
        layer.composite(canvas, source);
    }
    for (const auto& layer : line.glow) {
        const bool before = layer.visible(false), after = layer.visible(true);
        if (std::ranges::none_of(states, [=](const State& state) {
                return (before && state.phase != Phase::After) || (after && state.phase != Phase::Before);
            })) continue;
        const auto source = layer.createSource(canvas);
        if (!source.surface) continue;
        auto& glowCanvas = *source.surface->getCanvas();
        inPhaseOrder([&](std::size_t index, Phase phase) {
            const auto& ink = states[index].animatedInk;
            const auto* posed = ink ? &*ink : nullptr;
            if (phase == Phase::Wiping) {
                const float edge = utopia ? transformedEdge(index) : states[index].edge;
                layer.drawCell(glowCanvas, index, false, edge, posed);
                layer.drawCell(glowCanvas, index, true, edge, posed);
            } else {
                layer.drawCell(glowCanvas, index, phase == Phase::After, std::nullopt, posed);
            }
        });
        layer.composite(canvas, source);
    }
    if (utopia && line.shadow.visible()) {
        const float edge = lineWipeFront(line.cells, tMs, line.decorationInk.left(),
                                        line.shadow.secondaryWidth / 2 + .5f, false);
        line.shadow.drawAnimated(canvas, line.cells, line.decorationInk, edge, lineWipeComplete(line.cells, tMs),
            line.shadow.outlineWidth + line.shadow.secondaryWidth + 4.f, [&](std::size_t index) {
                const auto& state = states[index];
                return StaticShadow::WipePose{state.transform, state.phase == Phase::Before,
                    state.phase == Phase::Wiping ? std::optional<float>(transformedEdge(index)) : std::nullopt};
            });
    } else {
        line.shadow.draw(canvas, line.cells, tMs, line.decorationInk);
    }
    for (const auto& group : line.rubyGroups)
        group.drawShadow(canvas, tMs, line.singingScale, line.rubySingingOrigin);
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
    for (const auto& group : line.rubyGroups) group.draw(canvas, tMs, line.singingScale, line.rubySingingOrigin);
    // The GPU's front particle layer covers the main text and readings.
    if (!line.particles.empty()) drawSingParticles(canvas, line.particles, tMs, true);
    if (line.scanline.upperRuby && line.scanline.frontAt(line.cells, tMs, line.inkStart))
        for (const auto& group : line.rubyGroups) group.drawScanline(canvas, line.scanline, tMs);
}
}
