#include "core/prepared_scene.h"
#include "character_halo.h"
#include "character_wipe.h"
#include "stroke_flash.h"
#include "wipe_front.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColorFilter.h"
#include "include/core/SkPaint.h"
#include "include/effects/SkRuntimeEffect.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <stdexcept>
#include <string_view>

namespace subtitle::skia {
namespace {
sk_sp<SkColorFilter> byteOpacityFilter(float opacityByte) {
    static const auto compiled = SkRuntimeEffect::MakeForColorFilter(SkString(R"(
        uniform float opacityByte;
        float4 main(float4 color) {
            float4 stored = floor(color * 255.0 + 0.5);
            return floor(stored * (opacityByte / 255.0) + 0.5) / 255.0;
        }
    )"));
    if (!compiled.effect) throw std::runtime_error("Skia 整行淡化滤镜无法编译");
    SkRuntimeColorFilterBuilder builder(compiled.effect);
    builder.uniform("opacityByte") = opacityByte;
    return builder.makeColorFilter();
}
}

void PreparedScene::drawLine(SkCanvas& canvas, const Line& line, int tMs, bool textVisible, float titleOpacity,
                             int mediaTimeMs) const {
    SkPaint paint;
    paint.setAntiAlias(true);
    const auto animation = textVisible ? line.animation.at(tMs, line.startMs, line.endMs)
                                       : LineAnimation::State{1.f, 0.f, 0.f};
    const bool reverseDecoration = line.rubyGroups.empty() && (!line.glow.empty() || line.shadow.visible());
    const bool rtlRubyWithoutScale = !line.rubyGroups.empty() && !line.singingScale.active;
    const bool lowerReadingDecoration = !line.rubyGlows[1].empty()
        || (line.hasRubyShadow && std::ranges::any_of(line.rubyGroups, [](const auto& group) { return group.lower(); }));
    const bool ltrDecorationWithoutScale = !line.singingScale.active
        && (line.shadow.visible() || line.hasRubyShadow || line.scanline.enabled);
    const bool plainGlowOpacity = line.animation.quantizeOpacity && (!line.glow.empty() || line.hasRubyGlow());
    const bool particleTransitionOpacity = line.characterGeometry.active() && !line.particles.empty();
    const bool fixedOpacity = plainGlowOpacity || particleTransitionOpacity || (!line.vertical && (line.wipeRightToLeft
        ? (reverseDecoration || rtlRubyWithoutScale) : (ltrDecorationWithoutScale || lowerReadingDecoration)));
    // Match the reference's fixed-point layer opacity before 8-bit blending.
    constexpr float opacitySteps = 4096.f;
    const float lineOpacity = fixedOpacity ? std::floor(animation.opacity * opacitySteps) / opacitySteps
                                           : animation.opacity;
    if (lineOpacity <= 0.f) return;
    SkAutoCanvasRestore restore(&canvas, true);
    if (animation.dx != 0 || animation.dy != 0)
        canvas.translate(animation.dx, animation.dy);
    const auto placement = line.translationAt(tMs);
    if (placement.x() != 0 || placement.y() != 0) canvas.translate(placement.x(), placement.y());
    const bool rubyGeometryPhase = line.rubyGeometryComposition
        && line.characterGeometry.inWindow(tMs, line.startMs, line.endMs);
    const bool hasParticleEntryGlow = line.particleEntryGlow != ParticleEntryGlow::None;
    const bool particleEntryGlow = hasParticleEntryGlow && line.characterGeometry.inEntryWindow(tMs, line.startMs);
    const bool sampledSymbols = line.particleEntryGlow == ParticleEntryGlow::SampledSymbols;
    const bool symbolEntryGlow = particleEntryGlow && sampledSymbols;
    const std::string_view exitKind = line.characterGeometry.exit;
    const std::string_view entryKind = line.characterGeometry.entry;
    const bool pairedSparkle = entryKind == "petal" && exitKind == "sparkle";
    auto sampledKind = exitKind == "note" || exitKind == "snow" || pairedSparkle ? exitKind : std::string_view{};
    auto quantizedKind = exitKind == "ripple" || pairedSparkle ? exitKind : std::string_view{};
    const bool snowEntry = line.rubyGeometryComposition && exitKind.empty()
        && (entryKind == "snow" || entryKind == "snow_solid");
    if (snowEntry) {
        sampledKind = entryKind;
        quantizedKind = entryKind;
    } else if (sampledSymbols) {
        sampledKind = entryKind;
        if (entryKind != "note") quantizedKind = entryKind;
    } else if (line.rubyGeometryComposition && exitKind.empty() && (entryKind == "note" || entryKind == "ripple")) {
        sampledKind = entryKind;
        if (entryKind == "ripple") quantizedKind = entryKind;
    } else if (hasParticleEntryGlow && entryKind == "feather") {
        sampledKind = entryKind;
    }
    const bool particleExitFade = rubyGeometryPhase && !line.particles.empty() && line.glow.empty() && !line.hasRubyGlow()
        && !line.shadow.visible() && !line.hasRubyShadow
        && line.characterGeometry.exitMs > 0 && tMs >= line.characterGeometry.exitStart(line.endMs);
    if (!textVisible) {
        if (particleExitFade)
            canvas.saveLayer(SkCanvas::SaveLayerRec(nullptr, nullptr, SkCanvas::kF16ColorType));
        drawSingParticles(canvas, line.particles, tMs, false, line.vertical, sampledKind, quantizedKind);
        drawSingParticles(canvas, line.particles, tMs, true, line.vertical, sampledKind, quantizedKind);
        return;
    }
    // Fade the composed line, preserving overlaps and translucent strokes.
    // Sheared characters leave the static paint bounds, so they fade in an unbounded layer.
    const bool geometry = line.characterGeometry.active();
    // The opacity-only character transition owns the entry window.
    const int fadeSide = line.characterFade.side(tMs, line.startMs, line.endMs);
    const bool utopia = !rubyGeometryPhase && (line.singingScale.active || line.utopiaTransition.active())
        && fadeSide == 0;
    const bool rubyUtopiaEntry = utopia && !line.rubyGroups.empty()
        && line.utopiaTransition.inEntryWindow(tMs, line.startMs);
    const bool fadeActive = fadeSide != 0;
    const bool rubyExitFade = fadeSide > 0 && line.characterFade.exitKind == CharacterFade::Kind::Fade
        && !line.rubyGroups.empty();
    const bool compositingSpin = fadeActive
        && std::ranges::any_of(line.rubyGroups, [](const auto& group) { return group.spins(); });
    const bool entrySpin = compositingSpin && fadeSide < 0
        && line.characterFade.entryKind == CharacterFade::Kind::Spin;
    const bool fadeBeforeSpin = compositingSpin && fadeSide < 0
        && line.characterFade.entryKind == CharacterFade::Kind::Fade;
    const bool compositingDrip = fadeActive
        && std::ranges::any_of(line.rubyGroups, [](const auto& group) { return group.drips(); });
    const bool movingGlow = compositingSpin || compositingDrip;
    const SingingScale rubyScale = utopia ? line.singingScale : SingingScale{};
    const bool movingCells = line.characterFade.transforms() || geometry || utopia;
    // Volume bars and particles can extend outside the text paint bounds.
    const bool dynamicPaint = movingCells || line.volumeSignal || !line.particles.empty();
    if ((lineOpacity < 1.f || line.isolateVerticalDecoration) && (dynamicPaint || !line.paintBounds.isEmpty())) {
        const auto* bounds = dynamicPaint ? nullptr : &line.paintBounds;
        // Keep the same bounded destination-format layer at every opacity
        // for vertical decoration. Raster edge coverage depends on the target
        // origin when translucent outlines are composited over the shadow.
        if (!line.roleBodyPaints.empty() || !line.roleGlows.empty() || fixedOpacity) {
            // The GPU reference composites the paint stack into bytes,
            // then applies the 8-bit group opacity to its premultiplied pixels.
            SkPaint fade;
            fade.setColorFilter(byteOpacityFilter(std::round(lineOpacity * 255.f)));
            canvas.saveLayer(bounds, &fade);
        } else if (!line.vertical && (!line.glow.empty() || line.hasRubyGlow())) {
            // Keep the halo coverage and the line opacity at float precision
            // until the composed line is written to the 8-bit output.
            SkPaint fade;
            fade.setAlphaf(lineOpacity);
            canvas.saveLayer(SkCanvas::SaveLayerRec(bounds, &fade, SkCanvas::kF16ColorType));
        } else {
            canvas.saveLayerAlphaf(bounds, lineOpacity);
        }
    }
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
    // Vertical columns retain the local half-plane when the viewport rotates.
    const float wipeViewportHeight = line.vertical || viewport_.rectStaysRect() ? 0.f : float(height_);
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
            if (cell.bitmap) return CharacterTransformState{}; // The GPU never poses a bitmap guide.
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
            && (cell.outline || cell.outerOutline || cell.bodyPaintIndex >= 0 || transform.glowAlpha > 0
                || !line.glow.empty() || !line.roleGlows.empty() || line.hasStrokeFlash))
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
        const bool useCellProgress = line.noWipe || (utopia && tMs <= wipeEnd(cell));
        const float ratio = transform.utopiaExit ? 1.f
            : useCellProgress ? rawProgress(cell)
            : transformedWipeRatio(cell.wipePoints, cell.adjustedWipeEnd, wipeStart(cell), wipeEnd(cell), tMs);
        const float origin = utopia && !line.rubyGroups.empty()
            ? line.rubySingingOrigin : std::min(line.cells.front().pivot.layoutLeft, line.cells.back().pivot.layoutLeft);
        return transformedWipeEdge(origin, transform.matrix.mapPoint({cell.pivot.inkLeft, 0}).x(),
            transform.matrix.mapPoint({cell.pivot.inkRight, 0}).x(), ratio, cell.pivot.wipePad, line.wipeRightToLeft);
    };
    // bitmap_guide_schema=1: the existing GPU draws a bitmap guide (glow, then
    // image) in the body pass. A wiping guide clips each side around its own
    // box by the stroke pad; after its own clock the front is the next
    // outlined cell's front, else its own front extrapolated past the end.
    const auto drawBitmapGuide = [&](std::size_t index) {
        const auto& cell = line.cells[index];
        const auto& guide = *cell.bitmap;
        if (!line.titleWindows.empty()) {
            guide.draw(canvas, false, titleOpacity, mediaTimeMs);
            return;
        }
        const auto phase = states[index].phase;
        if (!guide.wipes() || phase != Phase::Wiping) {
            if (guide.wipes() && phase == Phase::Before && !line.verticalBitmapClipInk.isEmpty()
                && tMs > wipeStart(line.cells.front())) {
                // Before tiles are still bounded by the owner's wipe once
                // singing has started in another cell of this column.
                const auto& ink = line.verticalBitmapClipInk;
                const float pad = guide.clipPad();
                const float start = line.wipeRightToLeft ? ink.bottom() : ink.top();
                const float edge = lineWipeFront(line.cells, tMs, start, line.scanline.restingPad,
                                                 line.wipeRightToLeft, true);
                const auto clip = SkRect::MakeLTRB(ink.left() - pad, line.wipeRightToLeft ? -1e6f : edge,
                                                  ink.right() + pad, line.wipeRightToLeft ? edge : 1e6f);
                SkAutoCanvasRestore keep(&canvas, true);
                canvas.clipRect(clip, SkClipOp::kIntersect, true);
                guide.draw(canvas, false, titleOpacity, tMs);
                return;
            }
            guide.draw(canvas, guide.wipes() && phase == Phase::After, titleOpacity, tMs);
            return;
        }
        float edge = front(cell, rawProgress(cell));
        if (guide.usesLineFront() && line.bitmapLineFrontOrigin) {
            // A guide displaced from the lyric row keeps its own box and takes
            // the owning line's wipe position while its before/after parts draw.
            edge = lineWipeFront(line.cells, tMs, *line.bitmapLineFrontOrigin, line.scanline.restingPad,
                                 line.wipeRightToLeft, line.vertical);
        } else if (tMs > wipeEnd(cell)) {
            if (index + 1 < line.cells.size() && !line.cells[index + 1].path.isEmpty()) {
                edge = front(line.cells[index + 1], rawProgress(line.cells[index + 1]));
            } else {
                auto points = cell.wipePoints;
                if (points.size() < 2) points = {{cell.startMs, 0.f}, {cell.endMs, 1.f}};
                std::size_t begin = 0;
                for (std::size_t i = points.size() - 1; i > 0; --i)
                    if (points[i - 1].timeMs <= tMs) { begin = i - 1; break; }
                const auto& from = points[begin];
                const auto& to = points[begin + 1];
                const int duration = cell.wipePoints.size() < 2 ? std::max(to.timeMs - from.timeMs, 1)
                                                                : to.timeMs - from.timeMs;
                edge = front(cell, duration == 0 ? to.position
                    : from.position + (to.position - from.position) * float(tMs - from.timeMs) / float(duration));
            }
        }
        const auto& box = guide.box();
        const float pad = guide.clipPad();
        for (const bool sung : {false, true}) {
            SkAutoCanvasRestore keep(&canvas, true);
            const bool leading = sung != line.wipeRightToLeft;
            const auto clip = line.vertical
                ? SkRect::MakeLTRB(box.left() - pad, leading ? box.top() - pad : edge,
                                  box.right() + pad, leading ? edge : box.bottom() + pad)
                : band(leading ? box.left() - pad : edge, leading ? edge : box.right() + pad);
            clipViewportWipe(canvas, clip, wipeViewportHeight);
            guide.draw(canvas, sung, titleOpacity, tMs);
        }
    };
    const auto drawCell = [&](std::size_t index, Layer layer, const PhaseShaders& shaders) {
        const auto& cell = line.cells[index];
        if (cell.bitmap) {
            if (layer == Layer::Body) drawBitmapGuide(index);
            return;
        }
        const auto& stroke = layer == Layer::OuterOutline ? cell.outerOutline : cell.outline;
        const bool drawOutline = layer != Layer::Body;
        if (drawOutline && !stroke) return;
        const auto& path = drawOutline ? stroke->path : cell.path;
        const auto& [progress, edge, phase, transform, animatedInk] = states[index];
        const float opacity = transform.opacity * titleOpacity;
        if (opacity <= 0) return;
        // The outline can reach beyond the integer wipe padding, just as
        // the vertical glyph can reach beyond its fixed cell.
        const bool animatedClip = !line.vertical && (utopia || (line.transformedWipe && transform.transformed))
            && phase == Phase::Wiping;
        const float clipEdge = animatedClip ? transformedEdge(index) : edge;
        const bool atFront = (line.vertical || drawOutline || animatedClip) && phase == Phase::Wiping;
        const bool before = !atFront && progress == 0;
        const bool after = !atFront && progress == 1;
        // The existing GPU poses the cached rings that protect translucent
        // paint with their glyph. Opaque outlines retain their drawn width.
        const bool poseProtected = drawOutline && animatedInk && stroke->protectBody;
        const auto posedStroke = poseProtected ? std::optional(path.makeTransform(transform.matrix)) : std::nullopt;
        const auto draw = [&](bool sung) {
            if (drawOutline && stroke->protectBody) {
                const auto& body = animatedInk ? *animatedInk : cell.path;
                canvas.clipPath(body, SkClipOp::kDifference, true);
                if (layer == Layer::OuterOutline && cell.outline) {
                    auto inner = cell.outline->path;
                    if (animatedInk)
                        inner = poseProtected ? inner.makeTransform(transform.matrix)
                                              : prepareOutlinePath(body, cell.outline->width);
                    canvas.clipPath(inner, SkClipOp::kDifference, true);
                }
            }
            const auto& shader = shaders[sung];
            paint.setShader(shader);
            paint.setColor(shader ? SK_ColorWHITE : drawOutline ? (sung ? stroke->after : stroke->before)
                                                               : (sung ? cell.after : cell.before));
            if ((particleExitFade || symbolEntryGlow) && opacity < 1.f && !transform.transformed && !shader) {
                // Sample stroke coverage before applying the fixed-point fade.
                // Round the painted pass before blending it into the target.
                const float fixed = std::floor(opacity * opacitySteps) / opacitySteps;
                SkPaint fade;
                fade.setColorFilter(byteOpacityFilter(fixed * 255.f));
                drawSupersampledSolidPath(canvas, path, paint, fade);
                return;
            }
            if (opacity < 1.f) paint.setAlphaf(paint.getAlphaf() * opacity);
            // Entry also needs consistent edge coverage once the paint
            // contributes more than one rounded alpha step.
            if ((rubyExitFade || (fadeBeforeSpin && opacity * 255.f >= 1.5f))
                && opacity < .125f && !transform.transformed && !shader) {
                drawSamplePatternPath(canvas, path, paint, true);
                return;
            }
            // Near the start of a spin, a one-byte change in edge coverage
            // can remove the only visible part of a letter from one target.
            if (animatedInk && !shader && transform.opacity < .125f
                && (rubyUtopiaEntry || (entrySpin && drawOutline))) {
                const auto posed = posedStroke ? *posedStroke
                    : drawOutline ? prepareOutlinePath(*animatedInk, stroke->width) : *animatedInk;
                drawSamplePatternPath(canvas, posed, paint, true);
                return;
            }
            if (animatedInk) {
                if (posedStroke) canvas.drawPath(*posedStroke, paint);
                else if (drawOutline) drawFixedOutline(canvas, *animatedInk, paint, stroke->width);
                else canvas.drawPath(*animatedInk, paint);
                return;
            }
            if (transform.transformed) canvas.concat(transform.matrix); // Clips stay in line space.
            canvas.drawPath(path, paint);
        };
        // Each phase owns its wipe clip, including the viewport's device bounds.
        if (!after) {
            canvas.save();
            clipViewportWipe(canvas, line.wipeRightToLeft
                ? band(-1e6f, before ? 1e6f : clipEdge)
                : band(before ? -1e6f : clipEdge, 1e6f),
                wipeViewportHeight);
            draw(false);
            canvas.restore();
        }
        if (!before) {
            canvas.save();
            clipViewportWipe(canvas, line.wipeRightToLeft
                ? band(after ? -1e6f : clipEdge, 1e6f)
                : band(-1e6f, after ? 1e6f : clipEdge),
                wipeViewportHeight);
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
        std::vector<PhaseShaders> roles;
        if (!line.roleBodyPaints.empty()) {
            roles.reserve(line.roleBodyPaints.size());
            for (const auto& paint : line.roleBodyPaints) {
                auto pair = layer == Layer::Body ? paint.gradients
                    : layer == Layer::Outline ? paint.outlineGradients : paint.outerGradients;
                const auto& roleImages = layer == Layer::Body ? paint.images.body
                    : layer == Layer::Outline ? paint.images.outline : paint.images.outer;
                for (std::size_t phase = 0; phase < pair.size(); ++phase)
                    if (roleImages[phase]) pair[phase] = roleImages[phase].shaderAt(canvas.getTotalMatrix());
                roles.push_back(std::move(pair));
            }
        }
        inPhaseOrder([&](std::size_t index, Phase) {
            const int paint = line.cells[index].bodyPaintIndex;
            drawCell(index, layer, roles.empty() || paint < 0 ? shaders : roles[paint]);
        });
    };
    if (!line.particles.empty())
        drawSingParticles(canvas, line.particles, tMs, false, line.vertical, sampledKind, quantizedKind);
    if (geometry)
        for (std::size_t i = 0; i < line.cells.size(); ++i) {
            const auto& state = states[i];
            drawCharacterHalo(canvas, state.animatedInk ? *state.animatedInk : line.cells[i].path,
                              state.transform, line.characterGeometry.fontPx);
        }
    const auto rubyOpacity = [&](const PreparedWipeGroup& group) {
        const auto owner = group.fadeOwner();
        return owner ? states[*owner].transform.opacity : 1.f;
    };
    const auto rubyEntryScale = [&](const PreparedWipeGroup& group) -> std::optional<float> {
        if (const auto owner = group.fadeOwner(); rubyUtopiaEntry && owner)
            return states[*owner].transform.matrix.getScaleX();
        return std::nullopt;
    };
    const auto rubyPose = [&](const PreparedWipeGroup& group) {
        const auto owner = group.fadeOwner();
        if (rubyGeometryPhase && owner) return states[*owner].transform.matrix;
        if (fadeActive && owner && group.drips() && states[*owner].transform.transformed)
            return states[*owner].transform.matrix;
        return fadeActive && owner ? group.spinMatrix(states[*owner].transform) : SkMatrix::I();
    };
    // Accumulate faint fill, outline and halo contributions before rounding
    // the final pixel to the destination's 8-bit channels.
    const bool hasGlow = !line.glow.empty() || line.hasRubyGlow();
    const bool utopiaExitGlow = line.utopiaTransition.leave
        && (line.hasRubyGlow() || !line.bodyGlowTransitionBounds.isEmpty());
    const bool opacityTransition = (line.characterFade.active() || rubyGeometryPhase || utopiaExitGlow)
        && (hasGlow || !line.rubyGroups.empty())
        && std::ranges::any_of(states, [](const State& state) {
            return state.transform.opacity > 0.f && state.transform.opacity < 1.f;
        });
    // At the tail, each halo contributes only a few alpha steps, so a
    // difference in the blur source's rounding can change the visible edge.
    const bool canonicalFadeGlow = (rubyExitFade || symbolEntryGlow) && hasGlow
        && std::ranges::none_of(states, [](const State& state) { return state.transform.opacity > .02f; });
    const bool canonicalUtopiaGlow = rubyUtopiaEntry && hasGlow
        && std::ranges::none_of(states, [](const State& state) { return state.transform.opacity > .125f; });
    const auto glowOpacity = [=](float opacity) {
        return canonicalFadeGlow ? std::floor(opacity * opacitySteps) / opacitySteps : opacity;
    };
    // Accumulate contributions of at most one rounded alpha step before
    // quantizing the complete stack.
    const bool subByteFade = canonicalFadeGlow && std::ranges::none_of(states, [](const State& state) {
        return state.transform.opacity * 255.f >= 1.5f;
    });
    if (opacityTransition && !particleExitFade && !particleEntryGlow && (!rubyExitFade || subByteFade)) {
        const auto* bounds = line.characterFade.transforms() || rubyGeometryPhase || utopiaExitGlow ? nullptr : &line.paintBounds;
        canvas.saveLayer(SkCanvas::SaveLayerRec(bounds, nullptr, SkCanvas::kF16ColorType));
    }
    // Tracking and wave restore static glow as each unit settles. Spin keeps
    // a separate blurred source for moving glyphs.
    const auto drawRubyGlow = [&](const PreparedGlow& layer, const std::vector<std::size_t>& readings) {
        const bool after = layer.visible(true);
        if (!after && !layer.visible(false)) return;
        const auto usesStaticGlow = [&](std::size_t index) {
            const auto& group = line.rubyGroups[index];
            return rubyPose(group).isIdentity() && rubyOpacity(group) > 0.f;
        };
        SkRect transitionBounds = SkRect::MakeEmpty();
        if (line.utopiaTransition.leave)
            for (const auto index : readings) transitionBounds.join(line.rubyGroups[index].inkBounds());
        const auto source = std::ranges::any_of(readings, usesStaticGlow)
            ? layer.createSource(canvas, transitionBounds, canonicalFadeGlow) : PreparedGlow::Source{};
        if (source.surface) {
            for (const auto index : readings) {
                if (!usesStaticGlow(index)) continue;
                const auto& group = line.rubyGroups[index];
                const auto owner = group.utopiaExitOwner();
                const int done = owner ? line.followingDone[*owner] : 0;
                group.drawGlowSource(*source.surface->getCanvas(), layer, tMs, after,
                                     rubyScale, line.rubySingingOrigin, glowOpacity(rubyOpacity(group)),
                                     owner && tMs > done ? &line.utopiaTransition : nullptr, done,
                                     rubyEntryScale(group));
            }
            if (canonicalFadeGlow || canonicalUtopiaGlow) layer.compositeCanonical(canvas, source);
            else layer.composite(canvas, source, rubyUtopiaEntry, compositingSpin);
        }
        for (const auto index : readings) {
            const auto& group = line.rubyGroups[index];
            if (const auto pose = rubyPose(group); !rubyGeometryPhase && !pose.isIdentity())
                group.compositeTransformedGlow(canvas, layer, tMs, after, rubyOpacity(group), pose);
        }
    };
    for (const auto& lane : line.rubyGlows)
        for (const auto& group : lane)
            for (const auto& layer : group.phases) drawRubyGlow(layer, group.readings);
    for (const auto& group : line.roleGlows) {
        for (const auto& layer : group.phases) {
            const bool after = layer.visible(true);
            if (!after && !layer.visible(false)) continue;
            const auto visible = [after](const State& state) {
                return state.transform.opacity > 0 && state.phase != (after ? Phase::Before : Phase::After);
            };
            if (std::ranges::none_of(group.cells, [&](std::size_t index) { return visible(states[index]); })) continue;
            const auto source = layer.createSource(canvas);
            if (!source.surface) continue;
            for (std::size_t local = 0; local < group.cells.size(); ++local) {
                const auto index = group.cells[local];
                const auto& state = states[index];
                if (!visible(state)) continue;
                const auto edge = state.phase == Phase::Wiping
                    ? std::optional(utopia ? transformedEdge(index) : state.edge) : std::nullopt;
                const auto* ink = state.animatedInk ? &*state.animatedInk : nullptr;
                layer.drawCell(*source.surface->getCanvas(), local, after, edge, ink, state.transform.opacity);
            }
            layer.composite(canvas, source);
        }
    }
    for (const auto& layer : line.glow) {
        const bool before = layer.visible(false), after = layer.visible(true);
        const auto usesStaticGlow = [&](const State& state) {
            return state.transform.opacity > 0.f
                && !((movingGlow || rubyGeometryPhase) && state.transform.transformed)
                && ((before && state.phase != Phase::After) || (after && state.phase != Phase::Before));
        };
        const auto source = std::ranges::any_of(states, usesStaticGlow)
            ? layer.createSource(canvas, line.bodyGlowTransitionBounds, canonicalFadeGlow) : PreparedGlow::Source{};
        if (source.surface) {
            auto& glowCanvas = *source.surface->getCanvas();
            inPhaseOrder([&](std::size_t index, Phase phase) {
                if (!usesStaticGlow(states[index])) return;
                const auto& ink = states[index].animatedInk;
                const auto* posed = ink ? &*ink : nullptr;
                const float opacity = glowOpacity(states[index].transform.opacity * titleOpacity);
                const bool sample = rubyUtopiaEntry && opacity < .125f;
                if (phase == Phase::Wiping) {
                    const float edge = utopia && !line.vertical ? transformedEdge(index) : states[index].edge;
                    layer.drawCell(glowCanvas, index, false, edge, posed, opacity, line.vertical, line.wipeRightToLeft, sample);
                    layer.drawCell(glowCanvas, index, true, edge, posed, opacity, line.vertical, line.wipeRightToLeft, sample);
                } else {
                    layer.drawCell(glowCanvas, index, phase == Phase::After, std::nullopt, posed, opacity,
                                   false, false, sample);
                }
            });
            if (canonicalFadeGlow || canonicalUtopiaGlow) layer.compositeCanonical(canvas, source);
            else layer.composite(canvas, source, particleEntryGlow || rubyUtopiaEntry, compositingSpin);
        }
        if (movingGlow)
            inPhaseOrder([&](std::size_t index, Phase phase) {
                const auto& state = states[index];
                if (!state.transform.transformed) return;
                const auto draw = [&](bool afterPhase, std::optional<float> edge) {
                    layer.compositeTransformedCell(canvas, index, afterPhase, edge,
                                                   state.transform.matrix, state.transform.opacity);
                };
                if (phase == Phase::Wiping) {
                    draw(false, state.edge);
                    draw(true, state.edge);
                } else {
                    draw(phase == Phase::After, std::nullopt);
                }
            });
    }
    if (utopia && (line.shadow.visible() || !line.cellShadows.empty())) {
        const bool reverse = line.vertical && line.wipeRightToLeft;
        const float origin = line.vertical
            ? (reverse ? line.decorationInk.bottom() : line.decorationInk.top())
            : line.decorationInk.left();
        const float edge = lineWipeFront(line.cells, tMs, origin, line.shadow.secondaryWidth / 2 + .5f,
                                        reverse, line.vertical);
        line.shadow.drawAnimated(canvas, line.cells, line.decorationInk, edge, lineWipeComplete(line.cells, tMs),
            line.shadow.outlineWidth + line.shadow.secondaryWidth + 4.f, [&](std::size_t index) {
                const auto& state = states[index];
                return StaticShadow::WipePose{state.transform, state.phase == Phase::Before,
                    state.phase == Phase::Wiping
                        ? std::optional<float>(line.vertical ? state.edge : transformedEdge(index)) : std::nullopt};
            }, line.vertical, reverse, line.cellShadows);
    } else {
        line.shadow.draw(canvas, line.cells, tMs, line.decorationInk, line.vertical, line.wipeRightToLeft,
                         titleOpacity, line.noWipe, line.cellShadows);
    }
    float previousReadingShadowOffset = 0.f;
    for (const auto& group : line.rubyGroups)
        group.drawShadow(canvas, tMs, line.singingScale, line.rubySingingOrigin, previousReadingShadowOffset);
    if (line.hasOuterOutline)
        drawLayer(Layer::OuterOutline, line.images.outer, {line.beforeOuterGradient, line.afterOuterGradient});
    if (line.hasOutline)
        drawLayer(Layer::Outline, line.images.outline, {line.beforeOutlineGradient, line.afterOutlineGradient});
    drawLayer(Layer::Body, line.images.body, {line.beforeGradient, line.afterGradient});
    // The GPU repaints the sweeping cells after the body.
    const float scanlineAlpha = line.scanline.alphaAt(tMs);
    if (utopia) {
        line.scanline.drawPosed(canvas, line.cells, tMs, line.inkStart,
            [&](std::size_t index) { return line.vertical ? states[index].edge : transformedEdge(index); },
            [&](std::size_t index, int layer, bool after, float slice) {
                const auto& cell = line.cells[index];
                const auto& state = states[index];
                const float opacity = state.transform.opacity * slice * scanlineAlpha;
                const auto& stroke = layer == 0 ? cell.outerOutline : cell.outline;
                if (opacity <= 0 || (layer < 2 && !stroke)) return;
                SkAutoCanvasRestore keep(&canvas, true);
                if (layer < 2 && stroke->protectBody) {
                    canvas.clipPath(cell.path, SkClipOp::kDifference, true);
                    if (layer == 0 && cell.outline) canvas.clipPath(cell.outline->path, SkClipOp::kDifference, true);
                }
                const auto& shader = line.scanline.gradients[layer][after];
                paint.setShader(shader);
                paint.setColor(shader ? SK_ColorWHITE
                    : layer < 2 ? line.scanline.paint.shade(stroke->before, stroke->after, after)
                                : line.scanline.paint.shade(cell.before, cell.after, after));
                paint.setAlphaf(paint.getAlphaf() * opacity);
                // The same posed outline as the body pass: fixed width around the posed glyph.
                if (state.animatedInk) {
                    if (layer < 2) drawFixedOutline(canvas, *state.animatedInk, paint, stroke->width);
                    else canvas.drawPath(*state.animatedInk, paint);
                    return;
                }
                if (state.transform.transformed) canvas.concat(state.transform.matrix);
                canvas.drawPath(layer < 2 ? stroke->path : cell.path, paint);
            });
    } else line.scanline.draw(canvas, line.cells, tMs, line.inkStart, line.inkTop, [&](std::size_t index, bool sung) {
        const auto& cell = line.cells[index];
        const auto& state = states[index];
        const float opacity = state.transform.opacity * scanlineAlpha;
        if (opacity <= 0) return;
        if (state.transform.transformed) {
            const float pad = line.scanline.solidPad;
            // The line-wide scanline mask stays within the unposed ink band.
            canvas.clipRect(SkRect::MakeLTRB(-1e6f, line.bodyInk.top() - pad, 1e6f, line.bodyInk.bottom() + pad),
                            SkClipOp::kIntersect, true);
            if (!state.animatedInk) canvas.concat(state.transform.matrix);
        }
        const auto& body = state.animatedInk ? *state.animatedInk : cell.path;
        const auto setPaint = [&](int layer, std::uint32_t before, std::uint32_t after) {
            const auto& shader = line.scanline.gradients[layer][sung];
            paint.setShader(shader);
            paint.setColor(shader ? SK_ColorWHITE : line.scanline.paint.shade(before, after, sung));
            if (opacity < 1.f) paint.setAlphaf(paint.getAlphaf() * opacity);
        };
        for (const bool outer : {true, false}) {
            const auto& stroke = outer ? cell.outerOutline : cell.outline;
            if (!stroke) continue;
            SkAutoCanvasRestore keep(&canvas, true);
            if (stroke->protectBody) {
                canvas.clipPath(body, SkClipOp::kDifference, true);
                if (outer && cell.outline) {
                    const auto inner = state.animatedInk ? cell.outline->path.makeTransform(state.transform.matrix)
                                                         : cell.outline->path;
                    canvas.clipPath(inner, SkClipOp::kDifference, true);
                }
            }
            setPaint(outer ? 0 : 1, stroke->before, stroke->after);
            if (!state.animatedInk) canvas.drawPath(stroke->path, paint);
            else if (stroke->protectBody) canvas.drawPath(stroke->path.makeTransform(state.transform.matrix), paint);
            else drawFixedOutline(canvas, body, paint, stroke->width);
        }
        setPaint(2, cell.before, cell.after);
        canvas.drawPath(body, paint);
        paint.setShader(nullptr);
    });
    if (line.hasStrokeFlash)
        for (std::size_t index = 0; index < line.cells.size(); ++index) {
            const auto& cell = line.cells[index];
            const auto& ink = states[index].animatedInk;
            drawStrokeFlash(canvas, ink ? *ink : cell.path, tMs - cell.startMs, cell.strokeFlashWidth);
        }
    for (const auto& group : line.rubyGroups) {
        if (const auto owner = group.utopiaExitOwner(); owner && tMs > line.followingDone[*owner]) {
            group.drawUtopiaExit(canvas, tMs, line.followingDone[*owner], line.utopiaTransition,
                                 line.rubySingingOrigin, wipeViewportHeight);
            continue;
        }
        SkPoint ownerPivot{};
        if (const auto owner = group.fadeOwner(); rubyGeometryPhase && owner) {
            const auto& pivot = line.cells[*owner].pivot;
            ownerPivot = {(pivot.layoutLeft + pivot.layoutRight) * .5f, pivot.centerY};
        }
        group.draw(canvas, tMs, rubyScale, line.rubySingingOrigin, rubyOpacity(group), wipeViewportHeight,
                   rubyPose(group), ownerPivot, rubyEntryScale(group));
    }
    // The GPU's front particle layer covers the main text and readings.
    if (!line.particles.empty()) {
        SkAutoCanvasRestore keep(&canvas, false);
        if (particleExitFade && opacityTransition)
            canvas.saveLayer(SkCanvas::SaveLayerRec(nullptr, nullptr, SkCanvas::kF16ColorType));
        drawSingParticles(canvas, line.particles, tMs, true, line.vertical, sampledKind, quantizedKind);
    }
    if (line.scanline.rubyHighlight && line.scanline.repaintsGlyphs()
        && line.scanline.frontAt(line.cells, tMs, line.inkStart))
        for (const auto& group : line.rubyGroups)
            group.drawScanline(canvas, line.scanline, tMs, line.singingScale, line.rubySingingOrigin);
    if (line.scanline.shape && !line.bodyInk.isEmpty()) {
        if (const auto front = line.scanline.frontAt(line.cells, tMs, line.inkStart, false)) {
            if (line.scanline.image)
                line.scanline.image->draw(canvas, tMs, *front, line.bodyInk.centerY(), scanlineAlpha);
            else
                line.scanline.shape.draw(canvas, tMs, *front, line.bodyInk, line.rubyInkTop);
        }
    }
    if (line.volumeSignal) line.volumeSignal->draw(canvas, tMs);
    if (line.shapeSignal) line.shapeSignal->draw(canvas, tMs);
}
}
