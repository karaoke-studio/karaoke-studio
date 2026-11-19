#include "core/prepared_scene.h"
#include "character_halo.h"
#include "character_wipe.h"
#include "stroke_flash.h"
#include "../../effects/wipe_clock.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkData.h"
#include "include/core/SkFont.h"
#include "include/core/SkFontMetrics.h"
#include "include/core/SkFontMgr.h"
#include "include/core/SkMatrix.h"
#include "include/core/SkPaint.h"
#include "include/core/SkTypeface.h"
#include "include/ports/SkFontMgr_empty.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace subtitle::skia {
PreparedScene::PreparedScene(const ResolvedSubtitleInput& input) : width_(input.width), height_(input.height) {
    if (width_ < 1 || height_ < 1 || width_ > 8192 || height_ > 8192)
        throw std::runtime_error("Skia: invalid resolved canvas size");
    std::size_t totalPoints = 0, totalCells = 0, totalGroups = 0;
    for (const auto& source : input.lines) {
        if (source.endMs < source.startMs) throw std::runtime_error("Skia: invalid resolved line time");
        Line line{{}, source.startMs, source.endMs};
        line.noWipe = source.noWipe;
        line.wipeRightToLeft = source.wipeRightToLeft;
        line.vertical = source.vertical;
        line.animation = source.animation;
        line.characterFade = source.characterFade;
        line.characterGeometry = source.characterGeometry;
        line.transformedWipe = source.transformedWipe;
        line.scanline = source.scanline;
        const auto& band = source.scanline;
        if (band.enabled && (!std::isfinite(band.halfWidth) || band.halfWidth < .5f || band.halfWidth > 8192
                             || !std::isfinite(band.solidPad) || band.solidPad < 4 || band.solidPad > 8192
                             || !std::isfinite(band.restingPad) || band.restingPad < .5f || band.restingPad > 8192
                             || !std::isfinite(band.softness) || band.softness < 0 || band.softness > band.halfWidth
                             || !std::isfinite(band.paint.brightness) || band.paint.brightness < 0 || band.paint.brightness > 1
                             || source.vertical || band.rightToLeft != source.wipeRightToLeft
                             || source.noWipe || !source.rubyGroups.empty()))
            throw std::runtime_error("Skia 扫字线仅支持横排实色高亮带");
        if (source.particles.size() > 4096) throw std::runtime_error("Skia 字幕粒子数量超限");
        for (const auto& item : source.particles)
            if (!item.sprite.isFinite() || !std::isfinite(item.originX) || !std::isfinite(item.originY)
                || !std::isfinite(item.boxW) || !std::isfinite(item.boxH) || item.boxW < 1 || item.boxH < 0
                || source.vertical || source.noWipe || !source.rubyGroups.empty()
                || item.burst.reverse != source.wipeRightToLeft
                || (source.wipeRightToLeft && (!band.enabled || item.burst.kind != "scanline")))
                throw std::runtime_error("Skia 字幕粒子未支持此布局或方向");
        line.particles = source.particles;
        line.particleVisibility = source.particleVisibility;
        // Annotation groups currently support only a forward front.
        if (source.wipeRightToLeft && !source.rubyGroups.empty())
            throw std::runtime_error("Skia 反向走字行不能携带注音组");
        if (source.translations.size() > 4096) throw std::runtime_error("Skia 页面位移窗口数量超限");
        for (const auto& translation : source.translations) {
            if (!std::isfinite(translation.x) || !std::isfinite(translation.y)
                || translation.endMs <= translation.startMs
                || (!line.translations.empty() && translation.startMs < line.translations.back().endMs))
                throw std::runtime_error("Skia 页面位移窗口无效或重叠");
            line.translations.push_back(translation);
        }
        for (const auto& cell : source.cells) {
            if (++totalCells > 65536) throw std::runtime_error("Skia 字形单元总数超限");
            if (!cell.path.isFinite() || !std::isfinite(cell.left) || !std::isfinite(cell.right)
                || cell.right < cell.left || cell.endMs < cell.startMs
                || !std::isfinite(cell.strokeFlashWidth) || cell.strokeFlashWidth < 0)
                throw std::runtime_error("Skia: invalid resolved glyph geometry/time");
            if (source.vertical && (!std::isfinite(cell.top) || !std::isfinite(cell.bottom) || cell.bottom <= cell.top))
                throw std::runtime_error("Skia 竖排单元框无效");
            if (cell.adjustedWipeEnd && cell.wipePoints.empty())
                throw std::runtime_error("Skia 重叠走字缺少基本时钟");
            if (!cell.wipePoints.empty()) {
                if (cell.wipePoints.size() < 2 || cell.wipePoints.size() > 4096
                    || (totalPoints += cell.wipePoints.size()) > 1048576
                    || cell.wipePoints.front().position != 0
                    || (!cell.adjustedWipeEnd && cell.wipePoints.back().position != 1))
                    throw std::runtime_error("Skia 分段走字端点或数量无效");
                int previousTime = -172800000;
                float previousPosition = 0;
                for (const auto& point : cell.wipePoints) {
                    const bool terminal = cell.adjustedWipeEnd && &point == &cell.wipePoints.back();
                    if (point.timeMs < previousTime || point.timeMs > 172800000
                        || !std::isfinite(point.position) || point.position < 0
                        || (!terminal && point.position < previousPosition) || point.position > 1)
                        throw std::runtime_error("Skia 分段走字包含无效、反向或非有限数据");
                    previousTime = point.timeMs;
                    previousPosition = point.position;
                }
            }
            if (cell.outline) line.hasOutline = true;
            if (cell.outerOutline) line.hasOuterOutline = true;
            if (cell.strokeFlashWidth > 0) line.hasStrokeFlash = true;
            if (line.animation.active()) {
                line.paintBounds.join(cell.path.getBounds());
                if (cell.outline) line.paintBounds.join(cell.outline->path.getBounds());
                if (cell.outerOutline) line.paintBounds.join(cell.outerOutline->path.getBounds());
            }
            line.cells.push_back({cell.path, cell.left, cell.right, cell.startMs, cell.endMs,
                                  cell.before, cell.after, cell.wipePoints, cell.adjustedWipeEnd,
                                  cell.top, cell.bottom, cell.outline, cell.outerOutline, cell.pivot, cell.strokeFlashWidth});
        }
        for (const auto& group : source.rubyGroups) {
            totalCells += group.size();
            if (++totalGroups > 4096 || totalCells > 65536) throw std::runtime_error("Skia 注音组或单元总数超限");
            line.rubyGroups.emplace_back(group, source.noWipe, source.vertical);
            if (line.animation.active())
                for (const auto& cell : group) line.paintBounds.join(cell.path.getBounds());
        }
        if (!line.paintBounds.isEmpty()) line.paintBounds.outset(1.f, 1.f);
        if (line.scanline.enabled || line.characterGeometry.active()) {
            SkRect ink = SkRect::MakeEmpty();
            for (const auto& cell : line.cells)
                if (!cell.path.isEmpty()) ink.join(cell.path.computeTightBounds());
            line.inkStart = ink.isEmpty() ? 0.f : line.scanline.rightToLeft ? ink.fRight : ink.fLeft;
            line.inkTop = ink.isEmpty() ? 0.f : ink.fTop;
            if (!ink.isEmpty()) line.inkCenter = ink.centerX();
        }
        lines_.push_back(std::move(line));
    }
}
PreparedScene::PreparedScene(const SubtitleInput& input) : width_(input.width), height_(input.height) {
    if (width_ < 1 || height_ < 1 || width_ > 8192 || height_ > 8192)
        throw std::runtime_error("Skia P04: invalid canvas size");
    auto manager = SkFontMgr_New_Custom_Empty();
    for (const auto& source : input.lines) {
        if (!std::isfinite(source.fontSize) || source.fontSize <= 0 || source.fontSize > 2048
            || !std::isfinite(source.letterSpacing) || !std::isfinite(source.spaceWidth)
            || !std::isfinite(source.centerX) || !std::isfinite(source.bottomY)
            || source.visibleEndMs < source.visibleStartMs)
            throw std::runtime_error("Skia P04: invalid line geometry/time");
        auto face = manager->makeFromData(SkData::MakeWithCopy(source.fontBytes.data(), source.fontBytes.size()));
        if (!face) throw std::runtime_error("Skia P04: cannot decode supplied font snapshot");
        SkFont font(face, source.fontSize);
        font.setHinting(SkFontHinting::kNone);
        font.setEdging(SkFont::Edging::kAntiAlias);
        SkFontMetrics metrics;
        font.getMetrics(&metrics);
        const float baseline = source.bottomY - metrics.fDescent;
        Line line{{}, source.visibleStartMs, source.visibleEndMs};
        float cursor = 0;
        for (const auto& cell : source.cells) {
            if (cell.codepoint < 32 || cell.codepoint > 126 || cell.endMs < cell.startMs)
                throw std::runtime_error("Skia P04: only printable ASCII cells are supported; P05 shaping required");
            auto glyph = font.unicharToGlyph(cell.codepoint);
            if (!glyph) throw std::runtime_error("Skia P04: supplied font misses a required glyph");
            float advance = 0;
            font.getWidths({&glyph, 1}, {&advance, 1});
            if (cell.codepoint == ' ') advance = source.spaceWidth;
            auto path = font.getPath(glyph);
            if (!path && cell.codepoint != ' ')
                throw std::runtime_error("Skia P04: outline unavailable (bitmap/color glyph not supported)");
            line.cells.push_back({path.value_or(SkPath()).makeTransform(SkMatrix::Translate(cursor, baseline)),
                                  cursor, cursor + advance, cell.startMs, cell.endMs, source.beforeArgb, source.afterArgb});
            cursor += advance + source.letterSpacing;
        }
        const float left = source.centerX - (cursor - (source.cells.empty() ? 0 : source.letterSpacing)) / 2;
        for (auto& cell : line.cells) {
            cell.path = cell.path.makeTransform(SkMatrix::Translate(left, 0));
            cell.left += left;
            cell.right += left;
        }
        lines_.push_back(std::move(line));
    }
}
void PreparedScene::draw(SkCanvas& canvas, int tMs) const {
    canvas.clear(SK_ColorTRANSPARENT);
    SkPaint paint;
    paint.setAntiAlias(true);
    for (const auto& line : lines_) {
        const bool textVisible = line.startMs <= tMs && tMs < line.endMs;
        if (!textVisible && !line.particleVisibility.outside(line.particles, tMs, line.startMs))
            continue;
        const auto animation = textVisible ? line.animation.at(tMs, line.startMs, line.endMs)
                                           : LineAnimation::State{1.f, 0.f, 0.f};
        if (animation.opacity <= 0.f) continue;
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
            continue;
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
        const auto wipeStart = [](const Cell& c) { return c.wipePoints.empty() ? c.startMs : c.wipePoints.front().timeMs; };
        const auto wipeEnd = [](const Cell& c) { return c.wipePoints.empty() ? c.endMs : c.wipePoints.back().timeMs; };
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
        const auto drawCell = [&](std::size_t index, Layer layer) {
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
                paint.setColor(drawOutline ? (sung ? stroke->after : stroke->before)
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
        const auto drawLayer = [&](Layer layer) {
            for (std::size_t index = line.cells.size(); index > 0; --index)
                if (states[index-1].phase == Phase::Before) drawCell(index-1, layer);
            for (std::size_t index = 0; index < line.cells.size(); ++index)
                if (states[index].phase == Phase::After) drawCell(index, layer);
            for (std::size_t index = 0; index < line.cells.size(); ++index)
                if (states[index].phase == Phase::Wiping) drawCell(index, layer);
        };
        if (!line.particles.empty()) drawSingParticles(canvas, line.particles, tMs, false);
        if (geometry)
            for (std::size_t i = 0; i < line.cells.size(); ++i) {
                const auto& state = states[i];
                drawCharacterHalo(canvas, state.animatedInk ? *state.animatedInk : line.cells[i].path,
                                  state.transform, line.characterGeometry.fontPx);
            }
        if (line.hasOuterOutline) drawLayer(Layer::OuterOutline);
        if (line.hasOutline) drawLayer(Layer::Outline);
        drawLayer(Layer::Body);
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
}
