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
#include <stdexcept>

namespace subtitle::skia {
namespace {
void includeCellPaint(SkRect& bounds, const ResolvedCell& cell) {
    bounds.join(cell.path.getBounds());
    if (cell.outline) bounds.join(cell.outline->path.getBounds());
    if (cell.outerOutline) bounds.join(cell.outerOutline->path.getBounds());
}
}

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
        line.beforeGradient = source.beforeGradient;
        line.afterGradient = source.afterGradient;
        line.beforeOutlineGradient = source.beforeOutlineGradient;
        line.afterOutlineGradient = source.afterOutlineGradient;
        line.beforeOuterGradient = source.beforeOuterGradient;
        line.afterOuterGradient = source.afterOuterGradient;
        line.images = source.images;
        line.noise = source.noise;
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
            if (line.animation.active()) includeCellPaint(line.paintBounds, cell);
            line.cells.push_back({cell.path, cell.left, cell.right, cell.startMs, cell.endMs,
                                  cell.before, cell.after, cell.wipePoints, cell.adjustedWipeEnd,
                                  cell.top, cell.bottom, cell.outline, cell.outerOutline, cell.pivot, cell.strokeFlashWidth});
        }
        for (const auto& group : source.rubyGroups) {
            totalCells += group.size();
            if (++totalGroups > 4096 || totalCells > 65536) throw std::runtime_error("Skia 注音组或单元总数超限");
            line.rubyGroups.emplace_back(group, source.noWipe, source.vertical);
            if (line.animation.active())
                for (const auto& cell : group) includeCellPaint(line.paintBounds, cell);
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
    for (const auto& line : lines_) {
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
