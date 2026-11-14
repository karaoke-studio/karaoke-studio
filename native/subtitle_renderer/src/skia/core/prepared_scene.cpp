#include "core/prepared_scene.h"
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
        Line line{{}, source.beforeArgb, source.afterArgb, source.visibleStartMs, source.visibleEndMs};
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
                                  cursor, cursor + advance, cell.startMs, cell.endMs});
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
        if (tMs < line.startMs || tMs >= line.endMs) continue;
        for (const auto& cell : line.cells) {
            const float progress = cell.endMs == cell.startMs ? (tMs >= cell.startMs ? 1.f : 0.f)
                : std::clamp(float(double(tMs) - cell.startMs) / float(double(cell.endMs) - cell.startMs), 0.f, 1.f);
            const float edge = cell.left + (cell.right - cell.left) * progress;
            // Two disjoint clips avoid painting translucent after color over before color.
            if (progress < 1) {
                canvas.save();
                canvas.clipRect(SkRect::MakeLTRB(progress == 0 ? -1e6f : edge, -1e6f, 1e6f, 1e6f));
                paint.setColor(line.before);
                canvas.drawPath(cell.path, paint);
                canvas.restore();
            }
            if (progress > 0) {
                canvas.save();
                canvas.clipRect(SkRect::MakeLTRB(-1e6f, -1e6f, progress == 1 ? 1e6f : edge, 1e6f));
                paint.setColor(line.after);
                canvas.drawPath(cell.path, paint);
                canvas.restore();
            }
        }
    }
}
}
