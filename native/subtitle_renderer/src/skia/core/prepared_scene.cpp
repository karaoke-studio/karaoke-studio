#include "core/prepared_scene.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkData.h"
#include "include/core/SkFont.h"
#include "include/core/SkFontMetrics.h"
#include "include/core/SkFontMgr.h"
#include "include/core/SkMatrix.h"
#include "include/core/SkTypeface.h"
#include "include/core/SkSurface.h"
#include "include/ports/SkFontMgr_empty.h"
#include <algorithm>
#include <cmath>
#include <map>
#include <stdexcept>

namespace subtitle::skia {
namespace {
void includeCellPaint(SkRect& bounds, const ResolvedCell& cell) {
    bounds.join(cell.path.getBounds());
    if (cell.bitmap) bounds.join(cell.bitmap->box);
    if (cell.outline) bounds.join(cell.outline->path.getBounds());
    if (cell.outerOutline) bounds.join(cell.outerOutline->path.getBounds());
}
void includeShadowPaint(SkRect& bounds, const StaticShadow& shadow) {
    if (bounds.isEmpty()) return;
    const auto silhouette = bounds;
    for (const auto& state : {shadow.before, shadow.after})
        if (state.visible()) bounds.join(silhouette.makeOffset(state.x, state.y));
}
}

PreparedScene::PreparedScene(const ResolvedSubtitleInput& input)
    : width_(input.width), height_(input.height), viewport_(input.viewport),
      lineBand_(input.lineBand) {
    if (width_ < 1 || height_ < 1 || width_ > 8192 || height_ > 8192)
        throw std::runtime_error("Skia: invalid resolved canvas size");
    if (!viewport_.isFinite())
        throw std::runtime_error("Skia 字幕视口变换无效");
    std::size_t totalPoints = 0, totalCells = 0, totalGroups = 0;
    std::size_t remainingBitmapHaloBytes = std::size_t(256) << 20;
    for (const auto& source : input.lines) {
        if (source.endMs < source.startMs) throw std::runtime_error("Skia: invalid resolved line time");
        Line line{{}, source.startMs, source.endMs};
        line.titleWindows = source.titleWindows;
        line.roleBodyPaints = source.roleBodyPaints;
        line.noWipe = source.noWipe;
        line.wipeRightToLeft = source.wipeRightToLeft;
        line.vertical = source.vertical;
        line.animation = source.animation;
        line.lineBand = source.lineBand;
        lineBand_.validateEffectBounds(line.lineBand);
        line.lineBandTransition = source.lineBandTransition;
        line.beforeGradient = source.beforeGradient;
        line.afterGradient = source.afterGradient;
        line.beforeOutlineGradient = source.beforeOutlineGradient;
        line.afterOutlineGradient = source.afterOutlineGradient;
        line.beforeOuterGradient = source.beforeOuterGradient;
        line.afterOuterGradient = source.afterOuterGradient;
        line.images = source.images;
        line.shadow = source.shadow;
        line.glow = PreparedGlow::prepare(source.glow);
        line.byteParticleEntryGlow = !line.glow.empty() && source.rubyGeometryComposition
            && (source.characterGeometry.entry == "heart" || source.characterGeometry.entry == "petal"
                || source.characterGeometry.entry == "feather"
                || source.characterGeometry.entry == "snow" || source.characterGeometry.entry == "snow_solid")
            && (source.characterGeometry.exit.empty() || source.characterGeometry.exit == "note");
        if (line.byteParticleEntryGlow)
            for (auto& layer : line.glow) layer.prepareBytePasses();
        line.bodyGlowTransitionBounds = source.bodyGlowTransitionBounds;
        line.isolateVerticalDecoration = line.vertical && (line.shadow.visible() || !line.glow.empty()
            || std::ranges::any_of(source.rubyGroups, [](const auto& group) { return group.shadow.visible() || group.glow; }));
        line.noise = source.noise;
        line.characterFade = source.characterFade;
        const bool needsPaintBounds = line.animation.active() || line.characterFade.active()
            || line.isolateVerticalDecoration || !line.titleWindows.empty();
        line.characterGeometry = source.characterGeometry;
        line.rubyGeometryComposition = source.rubyGeometryComposition;
        line.singingScale = source.singingScale;
        line.rubySingingOrigin = source.rubySingingOrigin;
        line.utopiaTransition = source.utopiaTransition;
        line.transformedWipe = source.transformedWipe;
        line.scanline = source.scanline;
        const auto& band = source.scanline;
        if (band.enabled && (!std::isfinite(band.halfWidth) || band.halfWidth < .5f || band.halfWidth > 8192
                             || !std::isfinite(band.solidPad) || band.solidPad < 4 || band.solidPad > 8192
                             || !std::isfinite(band.restingPad) || band.restingPad < .5f || band.restingPad > 8192
                             || !std::isfinite(band.softness) || band.softness < 0 || band.softness > band.halfWidth
                             || !std::isfinite(band.paint.brightness) || band.paint.brightness < 0 || band.paint.brightness > 1
                             || band.vertical != source.vertical || band.rightToLeft != source.wipeRightToLeft
                             || (band.vertical && (band.shape || band.image || source.utopiaTransition.active()
                                 || (band.rightToLeft && !source.rubyGroups.empty())))
                             || source.noWipe || (!band.rubyHighlight && !source.rubyGroups.empty())))
            throw std::runtime_error("Skia 扫字线的方向、画笔或布局组合无效");
        if (source.particles.size() > 4096) throw std::runtime_error("Skia 字幕粒子数量超限");
        for (const auto& item : source.particles) {
            const auto& burst = item.burst;
            const bool verticalStar = (burst.kind == "twinkle" && burst.sweep == 1)
                || (burst.kind == "twinkle_classic" && burst.sweep == 0);
            const bool verticalShape = burst.sweep == 0
                && (burst.kind == "note" || burst.kind == "heart" || burst.kind == "ripple"
                    || burst.kind == "petal" || burst.kind == "feather" || burst.kind == "snow"
                    || burst.kind == "snow_solid" || burst.kind == "lightning" || burst.kind == "lightning_glow");
            const bool verticalFlame = (burst.sweep == 2 && (burst.kind == "flame" || burst.kind == "flame_alt"))
                || (burst.sweep == 0 && (burst.kind == "flame_glow" || burst.kind == "smoke"
                    || burst.kind == "ember" || burst.kind == "spark"));
            const bool verticalSinging = source.vertical && (verticalStar || verticalShape || verticalFlame);
            const bool verticalScanline = source.vertical && band.enabled && burst.kind == "scanline" && burst.sweep == 0;
            const bool reversedScanline = source.wipeRightToLeft && !verticalSinging;
            if (!item.sprite.isFinite() || !std::isfinite(item.originX) || !std::isfinite(item.originY)
                || !std::isfinite(item.boxW) || !std::isfinite(item.boxH) || item.boxW < 1 || item.boxH < 0
                || (source.vertical && !verticalSinging && !verticalScanline) || (source.noWipe && !verticalSinging)
                || burst.reverse != reversedScanline || (reversedScanline && (!band.enabled || burst.kind != "scanline")))
                throw std::runtime_error("Skia 字幕粒子未支持此布局或方向");
        }
        line.particles = source.particles;
        line.particleVisibility = source.particleVisibility;
        line.shapeSignal = source.shapeSignal;
        line.volumeSignal = source.volumeSignal;
        if (source.wipeRightToLeft && std::ranges::any_of(source.rubyGroups, [](const auto& ruby) {
                return !ruby.rightToLeft;
            }))
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
            if (cell.bitmap && (!cell.path.isEmpty() || cell.outline || cell.outerOutline || source.vertical
                                || source.noWipe || cell.strokeFlashWidth > 0))
                throw std::runtime_error("Skia 图片导唱符仅支持横排走字单元");
            if (cell.bodyPaintIndex < -1 || cell.bodyPaintIndex >= int(line.roleBodyPaints.size()))
                throw std::runtime_error("Skia 正文角色画笔索引无效");
            if (cell.outline) line.hasOutline = true;
            if (cell.outerOutline) line.hasOuterOutline = true;
            if (cell.strokeFlashWidth > 0) line.hasStrokeFlash = true;
            if (needsPaintBounds) includeCellPaint(line.paintBounds, cell);
            for (auto& glow : line.glow)
                glow.appendCell(cell.path, line.singingScale.peakMatrix(cell.pivot));
            line.cells.push_back({cell.path, cell.left, cell.right, cell.startMs, cell.endMs,
                                  cell.before, cell.after, cell.wipePoints, cell.adjustedWipeEnd,
                                  cell.top, cell.bottom, cell.outline, cell.outerOutline, cell.pivot, cell.strokeFlashWidth,
                                  cell.bitmap ? std::make_shared<const PreparedBitmapGuide>(*cell.bitmap, remainingBitmapHaloBytes)
                                              : nullptr, cell.bodyPaintIndex});
        }
        for (const auto& group : source.roleGlows) {
            auto layers = PreparedGlow::prepare(group.glow, false);
            for (auto index : group.cells) {
                if (index >= line.cells.size()) throw std::runtime_error("Skia 角色光晕单元索引无效");
                const auto& cell = line.cells[index];
                const auto peak = line.singingScale.peakMatrix(cell.pivot);
                for (auto& layer : layers) layer.appendCell(cell.path, peak, group.gradients);
            }
            line.roleGlows.push_back({std::move(layers), group.cells});
        }
        if (line.utopiaTransition.leave) {
            line.followingDone.resize(line.cells.size());
            const Cell* next = nullptr;
            for (std::size_t cursor = line.cells.size(); cursor > 0; --cursor) {
                const auto& cell = line.cells[cursor - 1];
                line.followingDone[cursor - 1] = next ? std::max(next->endMs, cell.endMs)
                    : cell.endMs + std::max(line.utopiaTransition.tailMs - 750, 0);
                if (!cell.path.isEmpty()) next = &cell;
            }
        }
        if (line.isolateVerticalDecoration || line.animation.active()) includeShadowPaint(line.paintBounds, line.shadow);
        struct RubyGlowEntry { std::size_t index, nextCell; };
        std::map<std::pair<bool, int>, RubyGlowEntry> rubyGlowEntries;
        for (const auto& group : source.rubyGroups) {
            line.hasRubyShadow |= group.shadow.visible();
            if (group.fadeOwner && *group.fadeOwner >= line.cells.size())
                throw std::runtime_error("Skia 注音逐字淡化索引超限");
            totalCells += group.cells.size();
            if (++totalGroups > 4096 || totalCells > 65536) throw std::runtime_error("Skia 注音组或单元总数超限");
            std::optional<std::size_t> glowOffset;
            if (group.glow) {
                auto& lane = line.rubyGlows[group.lower];
                auto [entry, inserted] = rubyGlowEntries.try_emplace(std::pair{group.lower, group.glowStyle},
                                                                    RubyGlowEntry{lane.size(), 0});
                if (inserted) lane.push_back({PreparedGlow::prepare(*group.glow, false), {}});
                auto& preparedGlow = lane[entry->second.index];
                glowOffset = entry->second.nextCell;
                entry->second.nextCell += group.cells.size();
                preparedGlow.readings.push_back(line.rubyGroups.size());
                for (auto& layer : preparedGlow.phases)
                    for (const auto& cell : group.cells)
                        layer.appendCell(cell.path, line.singingScale.peakMatrix(cell.pivot), group.glowGradients);
            }
            if (group.utopiaExitOwner && (!line.utopiaTransition.leave || *group.utopiaExitOwner >= line.followingDone.size()))
                throw std::runtime_error("Skia 上注音 Utopia 退场归属无效");
            line.rubyGroups.emplace_back(group, source.noWipe, source.vertical, glowOffset);
            if (needsPaintBounds) {
                SkRect groupBounds = SkRect::MakeEmpty();
                for (const auto& cell : group.cells) includeCellPaint(groupBounds, cell);
                if (line.isolateVerticalDecoration || line.animation.active()) includeShadowPaint(groupBounds, group.shadow);
                line.paintBounds.join(groupBounds);
            }
        }
        if (line.characterFade.active() || line.isolateVerticalDecoration || line.animation.active()) {
            for (const auto& layer : line.glow) line.paintBounds.join(layer.sourceBounds());
            for (const auto& group : line.roleGlows)
                for (const auto& layer : group.phases) line.paintBounds.join(layer.sourceBounds());
            for (const auto& lane : line.rubyGlows)
                for (const auto& group : lane)
                    for (const auto& layer : group.phases) line.paintBounds.join(layer.sourceBounds());
        }
        if (!line.paintBounds.isEmpty()) line.paintBounds.outset(1.f, 1.f);
        if (line.scanline.enabled || line.characterGeometry.active() || line.shadow.visible()) {
            SkRect ink = SkRect::MakeEmpty();
            for (const auto& cell : line.cells)
                if (!cell.path.isEmpty()) ink.join(cell.path.computeTightBounds());
            if (!ink.isEmpty()) {
                if (line.scanline.vertical)
                    line.inkStart = line.scanline.rightToLeft ? ink.bottom() : ink.top();
                else
                    line.inkStart = line.scanline.rightToLeft ? ink.right() : ink.left();
            }
            line.inkTop = ink.isEmpty() ? 0.f : ink.fTop;
            if (!ink.isEmpty()) line.inkCenter = ink.centerX();
            if (line.shadow.visible()) line.decorationInk = ink;
            line.bodyInk = ink;
        }
        if (line.scanline.shape)
            for (const auto& group : source.rubyGroups)
                for (const auto& cell : group.cells)
                    if (!cell.path.isEmpty()) {
                        const float top = cell.path.computeTightBounds().fTop;
                        line.rubyInkTop = std::min(line.rubyInkTop.value_or(top), top);
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

SkPoint PreparedScene::Line::translationAt(int timeMs) const {
    for (const auto& translation : translations)
        if (translation.startMs <= timeMs && timeMs < translation.endMs) return {translation.x, translation.y};
    return {0, 0};
}

void PreparedScene::drawLineBands(SkCanvas& canvas, int timeMs) const {
    if (!lineBand_.image || lineBand_.opacity <= 0) return;
    struct PaintedBand { SkRect bounds; float alpha; };
    std::vector<PaintedBand> painted;
    for (const auto& line : lines_) {
        if (timeMs < line.startMs || timeMs >= line.endMs || line.lineBand.isEmpty()) continue;
        const auto animation = line.animation.at(timeMs, line.startMs, line.endMs);
        const float alpha = lineBand_.opacity * animation.opacity
            * line.lineBandTransition.opacityAt(timeMs, line.startMs, line.endMs);
        if (alpha <= 0) continue;
        const auto origin = line.translationAt(timeMs);
        const auto bounds = line.lineBand.makeOffset(origin.x() + animation.dx, origin.y() + animation.dy);
        if (std::ranges::any_of(painted, [&](const auto& other) {
                SkRect overlap;
                if (alpha > other.alpha + 0.02f || !overlap.intersect(bounds, other.bounds)) return false;
                const float smaller = std::min(bounds.width() * bounds.height(), other.bounds.width() * other.bounds.height());
                return overlap.width() * overlap.height() > 0.55f * smaller;
            })) continue;
        painted.push_back({bounds, alpha});
        lineBand_.draw(canvas, bounds, alpha, timeMs);
    }
}

void PreparedScene::draw(SkCanvas& canvas, int tMs) const {
    canvas.clear(SK_ColorTRANSPARENT);
    if (lineBand_.image) {
        SkAutoCanvasRestore restoreBand(&canvas, !viewport_.isIdentity());
        if (!viewport_.isIdentity()) canvas.concat(viewport_);
        drawLineBands(canvas, tMs);
    }
    for (const auto& line : lines_) {
        if (line.titleWindows.empty()) break;
        float opacity = 0.f;
        for (const auto& window : line.titleWindows) {
            if (tMs < window.startMs || tMs > window.endMs) continue;
            const float head = window.fadeInMs ? float(tMs - window.startMs) / window.fadeInMs : 1.f;
            const float tail = window.fadeOutMs ? float(window.endMs - tMs) / window.fadeOutMs : 1.f;
            opacity = std::max(opacity, std::min({1.f, head, tail}));
        }
        if (opacity > 0.f) drawLine(canvas, line, 0, true, opacity, tMs);
    }
    SkAutoCanvasRestore restore(&canvas, !viewport_.isIdentity());
    if (!viewport_.isIdentity()) canvas.concat(viewport_);
    for (const auto& line : lines_) {
        if (!line.titleWindows.empty()) continue;
        const bool textVisible = line.startMs <= tMs && tMs < line.endMs;
        if (!textVisible && !line.particleVisibility.outside(line.particles, tMs, line.startMs))
            continue;
        const int level = line.noise.levelAt(tMs, line.startMs, line.endMs);
        if (level == 0) continue;
        if (level == 64) {
            drawLine(canvas, line, tMs, textVisible);
            continue;
        }
        auto scratch = canvas.makeSurface(SkImageInfo::MakeN32Premul(width_, height_));
        if (!scratch) throw std::runtime_error("Skia noise surface allocation failed");
        auto& source = *scratch->getCanvas();
        source.clear(SK_ColorTRANSPARENT);
        drawLine(source, line, tMs, textVisible);
        auto image = scratch->makeImageSnapshot();
        if (!image) throw std::runtime_error("Skia noise surface snapshot failed");
        line.noise.composite(canvas, *image, level);
    }
}

}
