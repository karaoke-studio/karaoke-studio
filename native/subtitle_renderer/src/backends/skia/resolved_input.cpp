#include "resolved_input.h"
#include "resolved_wipe.h"
#include "resolved_glyph.h"
#include "resolved_ruby.h"
#include "scene_input.h"
#include "vertical_text.h"
#include "../render_backend.h"
#include "../../scene/skia_ir_projection.h"
#include "../../scene/skia_text_projection.h"
#include "include/core/SkMatrix.h"
#include <QtCore/QJsonArray>
#include <cmath>
#include <algorithm>
#include <map>
#include <string_view>

namespace krok::subtitle::native {
namespace {
std::uint32_t argb(const RgbaColor& c) {
    return (std::uint32_t(c.alpha) << 24) | (std::uint32_t(c.red) << 16) | (std::uint32_t(c.green) << 8) | c.blue;
}

void applyVerticalGeometry(::subtitle::skia::ResolvedCell& glyph, const QJsonObject& cell,
                           const std::wstring& text, double version, bool japaneseGlyphs, double dpr) {
    if (text.size() > 1) throw BackendError("Skia 竖排只支持单码点或空文本单元");
    const auto code = text.empty() ? L'\0' : text.front();
    // Mirror the host's supported Japanese punctuation and small kana. The
    // fixed cell's clock and bounds are independent of the glyph transform.
    constexpr std::wstring_view rotate = L"\u300c\u300d\u300e\u300f\u30fc\u301c";
    const bool rotated = rotate.find(code) != rotate.npos;
    const bool shifted = code == 0x3001 || code == 0x3002 || verticalSmallKana.find(code) != verticalSmallKana.npos;
    const bool upright = verticalUprightScalar(code);
    const bool extended = text.empty() || code == L' ' || code == 0x3000
        || (code >= L'0' && code <= L'9') || (code >= L'A' && code <= L'Z') || (code >= L'a' && code <= L'z')
        || (code >= 0x00c0 && code <= 0x00d6) || (code >= 0x00d8 && code <= 0x00f6) || (code >= 0x00f8 && code <= 0x00ff);
    if ((rotated || shifted) ? !japaneseGlyphs : !(upright || (version == 2 && extended)))
        throw BackendError("Skia 竖排单元包含未验证字符或缺少日文字形能力");
    if (rotated != cell.contains("rotation_pivot_x"))
        throw BackendError("Skia 竖排单元的字形旋转与源字符不符");

    const auto top = cell.value("wipe_top"), bottom = cell.value("wipe_bottom");
    if (!top.isDouble() || !bottom.isDouble() || !std::isfinite(top.toDouble()) || !std::isfinite(bottom.toDouble())
        || bottom.toDouble() <= top.toDouble() || std::abs(top.toDouble()) > 1e6 || std::abs(bottom.toDouble()) > 1e6)
        throw BackendError("Skia 竖排单元框无效");
    glyph.top = float(top.toDouble() * dpr);
    glyph.bottom = float(bottom.toDouble() * dpr);
    if (rotated) {
        const auto pivot = cell.value("rotation_pivot_x");
        if (!pivot.isDouble() || !std::isfinite(pivot.toDouble()) || std::abs(pivot.toDouble()) > 1e6)
            throw BackendError("Skia 竖排旋转轴无效");
        glyph.path = glyph.path.makeTransform(SkMatrix::RotateDeg(
            90.f, {float(pivot.toDouble() * dpr), (glyph.top + glyph.bottom) * 0.5f}));
    }
}
}
::subtitle::skia::ResolvedSubtitleInput resolvedSkiaInput(const QJsonObject& ir) {
    auto projected = scene::skiaSceneFromIr(ir);
    const auto features = ir.value("required_features").toArray();
    const int outlineLayers = features.contains("p06_secondary_solid_outline") ? 2
        : features.contains("p06_primary_solid_outline") ? 1 : 0;
    validateSkiaScene(projected, true, outlineLayers);
    const auto layout = ir.value("skia_layout").toObject();
    const auto transformVersion = layout.value("character_transform_schema").toDouble();
    const bool allowTransformedWipe = transformVersion >= 2;
    const bool allowAnimatedOutlines = transformVersion >= 3;
    const auto verticalVersion = layout.value("vertical_solid_schema").toDouble(-1);
    const bool japaneseGlyphs = ir.value("required_features").toArray().contains("p05_vertical_japanese_glyphs");
    if (layout.value("version").toInt() != 2 || layout.value("metrics_identity").toString().size() != 64
        || !layout.value("fonts").isObject() || !layout.value("lines").isArray()
        || !layout.value("runs").isArray()
        || !ir.value("required_features").toArray().contains("p05_resolved_horizontal"))
        throw BackendError("Skia 缺少版本化的最终布局与度量快照");
    const auto lines = layout.value("lines").toArray();
    const auto resources = lines.isEmpty() && layout.value("fonts").toObject().isEmpty()
        ? scene::FontResourceTable{} : scene::loadSkiaFontResources(layout.value("fonts").toObject());
    const auto originalLines = ir.value("track").toObject().value("lines").toArray();
    if (lines.size() != int(projected.lines.size()) || lines.size() > 4096)
        throw BackendError("Skia 最终布局行数不一致或超限");
    const auto runs = layout.value("runs").toArray();
    if (runs.size() > 4096) throw BackendError("Skia 字体实例数量超限");
    ResolvedGlyphContext glyphs;
    for (const auto& value : runs) {
        if (!value.isObject()) throw BackendError("Skia 字体实例必须为对象");
        const auto run = value.toObject();
        if (run.value("mixed_fallback").toBool()
            && (!ir.value("required_features").toArray().contains("p05_mixed_fonts")
                || layout.value("mixed_font_schema").toDouble(-1) != 1))
            throw BackendError("Skia 混合字体缺少版本化能力声明");
        if ((run.value("font_ids").toArray().size() > 1 || run.contains("font_options"))
            && (!ir.value("required_features").toArray().contains("p05_font_fallback")
                || layout.value("font_chain_schema").toDouble(-1) != 1))
            throw BackendError("Skia 显式字体链缺少版本化能力声明");
        if (run.value("rtl").toBool()) throw BackendError("Skia 当前规范布局尚不支持 RTL");
        glyphs.fonts.push_back(scene::skiaRunFonts(run, resources));
        glyphs.mixedFallback.push_back(run.value("mixed_fallback").toBool());
        glyphs.languages.push_back(run.value("language").toString("und").toStdString());
    }
    glyphs.dpr = ir.value("screen").toObject().value("dpr").toDouble(1);
    std::map<std::string, SkPath> particleSprites;
    for (const auto& [name, shape] : projected.fxSprites)
        particleSprites.emplace(name, vectorGlyphPath(shape, 1000.0));
    ::subtitle::skia::ResolvedSubtitleInput out{projected.width, projected.height, {}};
    std::size_t totalCells = 0, totalPoints = 0, totalGroups = 0;
    for (int index = 0; index < lines.size(); ++index) {
        const auto source = lines[index].toObject();
        const auto& line = projected.lines[index];
        const auto& style = projected.lineStyles[index];
        if (!source.value("cells").isArray()) throw BackendError("Skia 最终布局缺少单元");
        const auto cells = source.value("cells").toArray();
        const auto original = originalLines[index].toObject();
        const auto intervals = original.value("resolved_intervals").toArray();
        const auto sourceChars = original.value("chars").toArray();
        if (cells.size() != int(line.chars.size()) || intervals.size() != cells.size() || (totalCells += cells.size()) > 65536)
            throw BackendError("Skia 最终布局与解析计时单元不一致或超限");
        ::subtitle::skia::ResolvedLine prepared{{}, line.displayWindows[0].startMs, line.displayWindows[0].endMs};
        prepared.noWipe = line.karaokeAnimation == "no_wipe";
        const float slide = std::max(style.fontSize * .9f, 36.f) * (line.lane == 0 ? -1.f : 1.f);
        const float rise = std::max(style.fontSize * .35f, 18.f);
        using Kind = ::subtitle::skia::CharacterFade::Kind;
        const auto kindOf = [](const std::string& animation) {
            return animation == "char_drip" ? Kind::Drip : animation == "spin_flip" ? Kind::Spin : Kind::Fade;
        };
        const auto perCharacter = [](const std::string& animation) {
            return animation == "char_fade" || animation == "char_drip" || animation == "spin_flip";
        };
        const bool charEntry = perCharacter(line.entryAnimation), charExit = perCharacter(line.exitAnimation);
        prepared.animation = {line.entryAnimation == "none" || charEntry ? 0 : line.entryDurationMs,
                              line.exitAnimation == "none" || charExit ? 0 : line.exitDurationMs,
                              line.entryAnimation == "slide_in" ? slide : 0.f,
                              line.entryAnimation == "rise" ? rise : 0.f,
                              line.exitAnimation == "slide_out" ? slide : 0.f,
                              line.exitAnimation == "rise" ? -rise : 0.f};
        const int delay = cells.size() > 1 ? 350 / (int(cells.size()) - 1) : 0;
        prepared.characterFade = {charEntry && line.entryDurationMs > 0, charExit && line.exitDurationMs > 0,
                                  line.endMs, delay, kindOf(line.entryAnimation), kindOf(line.exitAnimation)};
        const bool transforms = prepared.characterFade.transforms();
        prepared.transformedWipe = transforms && allowTransformedWipe;
        const bool pivots = transforms || !line.bursts.empty();
        if (line.scanlineEnabled) {
            const float halfWidth = std::max(style.scanlineWidth, 1.f) * .5f;
            using Mode = ::subtitle::skia::ScanlineColorMode;
            const auto mode = style.scanlineMode == "brighten" ? Mode::Brighten
                : style.scanlineMode == "follow_before" ? Mode::FollowBefore
                : style.scanlineMode == "follow_after" ? Mode::FollowAfter : Mode::Color;
            const auto color = style.scanlineMode == "role" ? style.scanlineRolePaint.color : style.scanlineColor;
            prepared.scanline = {true, halfWidth, {mode, argb(color), style.scanlineBrightness},
                                 std::max(style.strokeWidth, 0.f) + std::max(style.stroke2Width, 0.f) + 4.f,
                                 std::max(style.stroke2Width, 0.f) * .5f + .5f,
                                 std::min(style.scanlineGlowRadius, halfWidth)};
        }
        // vertical_solid_schema=1: the reverse flag means bottom-to-top on a column.
        prepared.vertical = style.vertical;
        prepared.wipeRightToLeft = style.vertical ? line.wipeReverse : style.rightToLeft != line.wipeReverse;
        if (style.vertical) {
            if (!ir.value("required_features").toArray().contains("p05_vertical_solid")
                || !source.value("text_flow").isString() || source.value("text_flow").toString() != "vertical"
                || (!source.value("ruby_groups").toArray().isEmpty()
                    && (!ir.value("required_features").toArray().contains("p05_vertical_ruby")
                        || source.value("ruby_groups").toArray().size() != 1)))
                throw BackendError("Skia 竖排缺少能力声明或最终方向无效");
        } else if (style.rightToLeft || source.contains("text_flow")) {
            if (!ir.value("required_features").toArray().contains("p05_horizontal_rtl")
                || layout.value("horizontal_rtl_schema").toDouble(-1) != 1
                || !source.value("text_flow").isString()
                || source.value("text_flow").toString() != (style.rightToLeft ? "rtl" : "ltr")
                || !source.value("ruby_groups").toArray().isEmpty())
                throw BackendError("Skia 右至左单元排列缺少能力声明或最终方向无效");
        }
        // The existing GPU places vertical columns from margins only; page offsets do not apply.
        if (!style.vertical)
            for (const auto& window : line.placementWindows)
                prepared.translations.push_back({window.startMs, window.endMs, window.offsetX, window.offsetY});
        for (int j = 0; j < cells.size(); ++j) {
            const auto cell = cells[j].toObject();
            const int clock = line.wipeReverse ? cells.size()-1-j : j;
            if ((line.wipeReverse && (!cell.value("clock_index").isDouble()
                    || cell.value("clock_index").toDouble(-1) != clock))
                || (!line.wipeReverse && cell.contains("clock_index")))
                throw BackendError("Skia 反向走字时钟所有权无效");
            const auto interval = intervals[clock].toArray();
            if (interval.size() != 2 || !interval[0].isDouble() || !interval[1].isDouble()
                || !std::isfinite(interval[0].toDouble()) || !std::isfinite(interval[1].toDouble())
                || std::floor(interval[0].toDouble()) != interval[0].toDouble()
                || std::floor(interval[1].toDouble()) != interval[1].toDouble()
                || interval[0].toDouble() < -86400000 || interval[1].toDouble() > 86400000
                || interval[1].toDouble() < interval[0].toDouble())
                throw BackendError("Skia 最终 IR 必须提供有效且完整的解析计时区间");
            const auto& paint = line.chars[j].styleIndex >= 0 ? projected.charStyles[line.chars[j].styleIndex] : style;
            const auto beforeColor = argb(paint.beforeFillPaint.color);
            const auto afterColor = argb(paint.afterFillPaint.color);
            const double wipePad = std::floor(std::round(paint.strokeWidth / glyphs.dpr) / 2);
            const auto& vector = line.chars[j].vectorGlyph;
            const auto glyphId = sourceChars[j].toObject().value("vector_glyph_id");
            if ((glyphId.isString() && !vector) || (vector && line.chars[j].text != L"\ufffc"))
                throw BackendError("Skia 导唱符缺少有效的来源轮廓引用");
            auto glyph = vector
                ? glyphs.vectorCell(cell, *vector, glyphId.toString(), paint.fontSize / glyphs.dpr, line.chars[clock].startMs,
                                    line.chars[clock].endMs, beforeColor, afterColor)
                : glyphs.cell(cell, QString::fromStdWString(line.chars[j].text), line.chars[clock].startMs,
                              line.chars[clock].endMs, beforeColor, afterColor, wipePad);
            if (outlineLayers > 0 && !glyph.path.isEmpty()) {
                const bool bodyAlpha = (beforeColor >> 24) < 255 || (afterColor >> 24) < 255;
                const auto beforeOutline = argb(paint.beforeStrokePaint.color);
                const auto afterOutline = argb(paint.afterStrokePaint.color);
                if (paint.strokeWidth > 0)
                    glyph.outline = ::subtitle::skia::SolidOutline{
                        ::subtitle::skia::prepareOutlinePath(glyph.path, paint.strokeWidth),
                        beforeOutline, afterOutline, bodyAlpha, paint.strokeWidth};
                if (paint.stroke2Width > 0)
                    glyph.outerOutline = ::subtitle::skia::SolidOutline{
                        ::subtitle::skia::prepareOutlinePath(glyph.path, paint.strokeWidth + paint.stroke2Width),
                        argb(paint.beforeStroke2Paint.color), argb(paint.afterStroke2Paint.color),
                        bodyAlpha || (beforeOutline >> 24) < 255 || (afterOutline >> 24) < 255, paint.strokeWidth + paint.stroke2Width};
            }
            glyph.wipePoints = resolvedSkiaWipePoints(cell, ir, line.chars[clock].startMs - int(interval[0].toDouble()), totalPoints);
            if (const auto end = resolvedSkiaWipeEnd(cell, ir)) {
                if (glyph.wipePoints.empty()) glyph.wipePoints = {{glyph.startMs, 0.f}, {glyph.endMs, 1.f}};
                glyph.wipePoints.back().position = *end;
                glyph.adjustedWipeEnd = true;
            }
            // Canonical layout cell and glyph-run box center.
            if (pivots) {
                const auto pivot = [&](const char* key) {
                    const auto value = cell.value(QLatin1String(key));
                    if (!value.isDouble() || !std::isfinite(value.toDouble()) || std::abs(value.toDouble()) > 1e6)
                        throw BackendError("Skia 逐字变换缺少有效的版面单元轴心");
                    return float(value.toDouble() * glyphs.dpr);
                };
                glyph.pivot = {pivot("layout_left"), pivot("layout_right"), pivot("pivot_y"), glyph.left, glyph.right};
                if (glyph.pivot.layoutRight < glyph.pivot.layoutLeft)
                    throw BackendError("Skia 逐字变换的版面单元轴心无效");
                if (transforms && (glyph.outline || glyph.outerOutline)) {
                    const auto opaque = [](const ::subtitle::skia::SolidOutline& stroke) {
                        return !stroke.protectBody && (stroke.before >> 24) == 255 && (stroke.after >> 24) == 255;
                    };
                    if (!allowAnimatedOutlines || (glyph.outline && !opaque(*glyph.outline))
                        || (glyph.outerOutline && !opaque(*glyph.outerOutline)))
                        throw BackendError("Skia 逐字变换当前仅支持不透明实色描边");
                    const auto ink = glyph.path.computeTightBounds();
                    glyph.pivot.inkLeft = ink.fLeft;
                    glyph.pivot.inkRight = ink.fRight;
                    glyph.pivot.wipePad = float(int(paint.strokeWidth) / 2);
                }
            } else if (cell.contains("layout_left") || cell.contains("layout_right") || cell.contains("pivot_y")) {
                throw BackendError("Skia 版面单元轴心缺少逐字特效能力声明");
            }
            if (style.vertical) {
                applyVerticalGeometry(glyph, cell, line.chars[j].text, verticalVersion, japaneseGlyphs, glyphs.dpr);
            } else if (cell.contains("wipe_top") || cell.contains("wipe_bottom") || cell.contains("rotation_pivot_x")) {
                throw BackendError("Skia 横排单元不能携带竖排单元框");
            }
            prepared.cells.push_back(std::move(glyph));
        }
        const int timingOffset = cells.isEmpty() ? 0
            : line.chars.front().startMs - int(intervals[0].toArray()[0].toDouble());
        prepared.rubyGroups = resolvedSkiaRubyGroups(source, ir, glyphs, projected, index, timingOffset, totalCells, totalGroups);
        if (prepared.characterFade.active() && !prepared.rubyGroups.empty())
            throw BackendError("Skia 逐字淡化暂不支持注音组合");
        // Each burst anchors at its character pivot; the box is
        // the wipe extent by the line's metric ascent + descent (glyph runs, never ink).
        if (!line.bursts.empty()) {
            const auto box = source.value("particle_box_height");
            if (!box.isDouble() || !std::isfinite(box.toDouble()) || box.toDouble() < 0 || box.toDouble() > 1e5
                || !prepared.rubyGroups.empty() || style.vertical || style.rightToLeft || line.wipeReverse)
                throw BackendError("Skia 唱字星光缺少有效的行盒高度");
            for (const auto& burst : line.bursts) {
                const auto sprite = particleSprites.find(burst.sprite);
                if (sprite == particleSprites.end() || sprite->second.isEmpty()
                    || burst.charIndex < 0 || burst.charIndex >= int(prepared.cells.size()))
                    throw BackendError("Skia 唱字星光缺少粒子轮廓或锚点");
                const auto& anchor = prepared.cells[std::size_t(burst.charIndex)];
                prepared.particles.push_back({burst, sprite->second, argb(burst.color),
                    (anchor.pivot.layoutLeft + anchor.pivot.layoutRight) * .5f, anchor.pivot.centerY,
                    std::max(anchor.right - anchor.left, 1.f), float(box.toDouble() * glyphs.dpr)});
            }
        } else if (source.contains("particle_box_height")) {
            throw BackendError("Skia 行盒高度缺少唱字星光能力声明");
        }
        if (transforms) {
            // Transformed cells are never wiping: entry finishes before each cell starts,
            // exit begins after every cell ends. Overlap uses a per-glyph front (unverified).
            const auto& fade = prepared.characterFade;
            for (std::size_t i = 0; !allowTransformedWipe && i < prepared.cells.size(); ++i) {
                const auto& c = prepared.cells[i];
                const int start = c.wipePoints.empty() ? c.startMs : c.wipePoints.front().timeMs;
                const int end = c.wipePoints.empty() ? c.endMs : c.wipePoints.back().timeMs;
                if ((fade.entry && fade.entryKind != Kind::Fade && start < prepared.startMs + fade.delay * int(i) + 250)
                    || (fade.exit && fade.exitKind != Kind::Fade && end > std::max(line.endMs, prepared.endMs - 600)))
                    throw BackendError("Skia 逐字滴落/翻转期间暂不支持走字");
            }
            if (style.vertical || style.rightToLeft || line.wipeReverse || prepared.noWipe || line.scanlineEnabled)
                throw BackendError("Skia 逐字滴落/翻转仅支持正向横排正文");
        }
        if (line.wipeReverse) std::reverse(prepared.cells.begin(), prepared.cells.end());
        out.lines.push_back(std::move(prepared));
    }
    return out;
}
}
