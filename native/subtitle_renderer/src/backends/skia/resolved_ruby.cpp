#include "resolved_ruby.h"
#include "resolved_wipe.h"
#include "vertical_text.h"
#include "../render_backend.h"
#include "../text_semantics.h"
#include <QtCore/QJsonArray>
#include <algorithm>
#include <cmath>

namespace krok::subtitle::native {
namespace {
int integer(const QJsonValue& value, int minimum, int maximum) {
    const double n = value.toDouble();
    if (!value.isDouble() || !std::isfinite(n) || std::floor(n) != n || n < minimum || n > maximum)
        throw BackendError("Skia 注音最终时钟或样式索引无效");
    return int(n);
}
std::uint32_t argb(const RgbaColor& c) {
    return (std::uint32_t(c.alpha) << 24) | (std::uint32_t(c.red) << 16) | (std::uint32_t(c.green) << 8) | c.blue;
}
std::uint32_t rgbaPaint(const QJsonValue& value) {
    if (!value.isArray() || value.toArray().size() != 4)
        throw BackendError("Skia 注音实色必须为 RGBA 字节");
    const auto color = value.toArray();
    const auto r = integer(color[0], 0, 255), g = integer(color[1], 0, 255);
    const auto b = integer(color[2], 0, 255), a = integer(color[3], 0, 255);
    return (std::uint32_t(a) << 24) | (std::uint32_t(r) << 16) | (std::uint32_t(g) << 8) | b;
}
struct OutlinePaint {
    int width = 0;
    std::uint32_t before = 0, after = 0;
    bool hasAlpha() const { return (before >> 24) < 255 || (after >> 24) < 255; }
};
OutlinePaint outlinePaint(const QJsonObject& outlines, const char* key) {
    if (!outlines.contains(key)) return {};
    const auto layer = outlines.value(key).toObject();
    return {integer(layer.value("width"), 1, 4096),
            rgbaPaint(layer.value("before")), rgbaPaint(layer.value("after"))};
}
bool matchesOutline(const OutlinePaint& outline, const PaintStyle& before, const PaintStyle& after,
                    float width, double dpr) {
    if (std::abs(outline.width * dpr - width) > 1e-4) return false;
    return !outline.width || (before.mode == "solid" && after.mode == "solid"
        && outline.before == argb(before.color) && outline.after == argb(after.color));
}
void validateStandaloneMark(const QString& text, const QJsonObject& cell, const QJsonObject& ir) {
    const auto code = text.toUcs4().front();
    if (!QChar::isMark(code)) return;
    const auto layout = ir.value("skia_layout").toObject();
    if (!ir.value("required_features").toArray().contains("p05_standalone_marks")
        || layout.value("standalone_mark_schema").toDouble(-1) != 1
        || !ir.value("required_features").toArray().contains("p05_ruby_units")
        || layout.value("ruby_unit_schema").toDouble(-1) != 1)
        throw BackendError("Skia 独立组合标记缺少版本化能力声明");
    if (text.size() != 1 || QChar::category(code) != QChar::Mark_NonSpacing
        || !((code >= 0x300 && code <= 0x36f && code != 0x34f && !(code >= 0x35c && code <= 0x362))
             || code == 0x3099 || code == 0x309a))
        throw BackendError("Skia 该独立组合标记尚未完成 GPU 字形兼容");
    const auto metrics = cell.value("metrics").toObject();
    const auto glyphs = metrics.value("glyphs").toArray();
    if (glyphs.size() != 1 || !glyphs[0].isObject()
        || glyphs[0].toObject().value("x").toDouble(1) != 0
        || glyphs[0].toObject().value("y").toDouble(1) != 0
        || metrics.value("selected_font").toDouble(-1) < 0)
        throw BackendError("Skia 独立组合标记必须是单字体原位字形");
}
}
// The vertical profile accepts only upright CJK/Hangul/full-size kana units.
static bool verticalRubyScalar(const QString& text) {
    const auto scalars = text.toUcs4();
    if (scalars.size() != 1) return false;
    return verticalUprightScalar(scalars.front());
}
std::vector<::subtitle::skia::ResolvedRubyGroup> resolvedSkiaRubyGroups(
        const QJsonObject& source, const QJsonObject& ir, const ResolvedGlyphContext& glyphs,
        const RenderScene& scene, int lineIndex, int offset, std::size_t& totalCells, std::size_t& totalGroups) {
    if (!source.contains("ruby_groups")) return {};
    if (!ir.value("required_features").toArray().contains("p05_resolved_ruby")
        || ir.value("skia_layout").toObject().value("ruby_schema").toDouble(-1) != 1
        || !source.value("ruby_groups").isArray())
        throw BackendError("Skia 注音最终布局缺少版本或能力声明");
    std::vector<::subtitle::skia::ResolvedRubyGroup> groups;
    const auto& line = scene.lines[lineIndex];
    const bool vertical = scene.lineStyles[lineIndex].vertical;
    const bool allowOutlines = ir.value("required_features").toArray().contains("p06_ruby_solid_outline");
    const bool allowShadow = ir.value("required_features").toArray().contains("p06_ruby_shadow")
        && !vertical && line.karaokeAnimation != "no_wipe"
        && line.entryAnimation == "none" && line.exitAnimation == "none";
    const auto features = ir.value("required_features").toArray();
    const bool allowSingingGlow = features.contains("p07_singing_glow")
        && line.karaokeAnimation == "utopia" && !line.zoomPulseEnabled;
    const bool allowGlow = features.contains("p06_ruby_glow")
        && !vertical && (line.karaokeAnimation == "none" || allowSingingGlow)
        && line.entryAnimation == "none" && line.exitAnimation == "none"
        && scene.charStyles.empty();
    const double verticalSchema = ir.value("skia_layout").toObject().value("vertical_ruby_schema").toDouble(-1);
    if (source.value("ruby_groups").toArray().size() > 4096 - totalGroups)
        throw BackendError("Skia 注音组总数超限");
    for (const auto& value : source.value("ruby_groups").toArray()) {
        if (!value.isObject() || ++totalGroups > 4096 || line.chars.empty())
            throw BackendError("Skia 注音组无效或总数超限");
        const auto object = value.toObject();
        const bool lower = object.value("lower").toBool();
        const int index = integer(object.value("style_cell"), 0, int(line.chars.size()) - 1);
        const auto& paint = line.chars[index].styleIndex >= 0
            ? scene.charStyles[line.chars[index].styleIndex] : scene.lineStyles[lineIndex];
        const auto shadowState = [&](bool after) -> ::subtitle::skia::ShadowState {
            const auto kind = paint.rubyDecorationKindForState(after);
            if (lower || kind == "none" || kind == "glow") return {};
            const auto& decor = after ? paint.rubyAfterDecorPaint : paint.rubyBeforeDecorPaint;
            const float x = paint.rubyShadowOffsetXForState(after), y = paint.rubyShadowOffsetYForState(after);
            if (!allowShadow || kind != "shadow" || decor.mode != "solid" || !std::isfinite(x) || !std::isfinite(y))
                throw BackendError("Skia 当前仅支持横排上注音的实色阴影");
            return {true, x, y, argb(decor.color)};
        };
        const ::subtitle::skia::StaticShadow shadow{
            shadowState(false), shadowState(true), paint.rubyStrokeWidth, paint.rubyStroke2Width};
        const auto glowState = [&](bool after) -> ::subtitle::skia::GlowState {
            if (lower || paint.rubyDecorationKindForState(after) != "glow") return {};
            const auto& decor = after ? paint.rubyAfterDecorPaint : paint.rubyBeforeDecorPaint;
            const float radius = after ? paint.rubyGlowAfterRadius : paint.rubyGlowBeforeRadius;
            if (!allowGlow || decor.mode != "solid" || !std::isfinite(radius) || radius > 532.f)
                throw BackendError("Skia 当前仅支持横排上注音的实色光晕与普通唱字放大");
            return {int(std::lround(radius)), paint.rubyGlowConcentrationForState(after), argb(decor.color)};
        };
        const ::subtitle::skia::StaticGlow glow{
            glowState(false), glowState(true), paint.rubyStrokeWidth + paint.rubyStroke2Width};
        if (!lower && (paint.rubyBeforeFillPaint.mode != "solid" || paint.rubyAfterFillPaint.mode != "solid"))
            throw BackendError("Skia 注音最终绘制当前仅支持实色");
        const auto fill = object.value("paint").toObject();
        const auto before = lower ? rgbaPaint(fill.value("before")) : argb(paint.rubyBeforeFillPaint.color);
        const auto after = lower ? rgbaPaint(fill.value("after")) : argb(paint.rubyAfterFillPaint.color);
        const auto units = object.value("units");
        if (!units.isArray() || units.toArray().isEmpty() || units.toArray().size() > 4096
            || (totalCells += units.toArray().size()) > 65536)
            throw BackendError("Skia 注音最终单元数量无效或超限");
        const auto outlines = object.value("outlines").toObject();
        if (object.contains("outlines") && (!allowOutlines || vertical || outlines.isEmpty()
            || outlines.size() != int(outlines.contains("stroke")) + int(outlines.contains("stroke2"))))
            throw BackendError("Skia 注音描边要求横排布局和有效的实色描边能力");
        const auto primary = outlinePaint(outlines, "stroke");
        const auto secondary = outlinePaint(outlines, "stroke2");
        if (!lower) {
            const auto reading = units.toArray();
            const bool latin = std::ranges::all_of(reading, [](const auto& item) {
                return isLatinText(item.toObject().value("text").toString().toStdWString());
            });
            const float width = latin ? paint.rubyLatinStrokeWidth : paint.rubyStrokeWidth;
            const float width2 = latin ? paint.rubyLatinStroke2Width : paint.rubyStroke2Width;
            if (!matchesOutline(primary, paint.rubyBeforeStrokePaint, paint.rubyAfterStrokePaint, width, glyphs.dpr)
                || !matchesOutline(secondary, paint.rubyBeforeStroke2Paint, paint.rubyAfterStroke2Paint,
                                   width2, glyphs.dpr))
                throw BackendError("Skia 上注音描边与实色样式不一致");
        }
        const bool protectFill = (before >> 24) < 255 || (after >> 24) < 255;
        const float innerWidth = float(primary.width * glyphs.dpr);
        const float outerWidth = float((primary.width + secondary.width) * glyphs.dpr);
        const double wipePad = (primary.width / 2) / glyphs.dpr;
        std::vector<::subtitle::skia::ResolvedCell> cells;
        for (const auto& unit : units.toArray()) {
            if (!unit.isObject()) throw BackendError("Skia 注音最终单元必须为对象");
            const auto cell = unit.toObject();
            const auto text = cell.value("text"), times = cell.value("interval_ms");
            if (!text.isString() || text.toString().isEmpty() || text.toString().toUtf8().size() > 4096
                || !times.isArray() || times.toArray().size() != 2 || cell.contains("wipe_points"))
                throw BackendError("Skia 注音文本或最终时间区间无效");
            QString drawingText = text.toString();
            if (cell.contains("glyph_text")) {
                const auto glyphText = cell.value("glyph_text");
                if (!ir.value("required_features").toArray().contains("p05_ruby_units")
                    || ir.value("skia_layout").toObject().value("ruby_unit_schema").toDouble(-1) != 1
                    || !glyphText.isString() || glyphText.toString() != drawingText.normalized(QString::NormalizationForm_C))
                    throw BackendError("Skia 注音字形文本缺少能力声明或不是来源单元的 NFC");
                drawingText = glyphText.toString();
            }
            validateStandaloneMark(drawingText, cell, ir);
            const auto interval = times.toArray();
            const int start = integer(interval[0], -86400000, 86400000);
            const int end = integer(interval[1], start, 86400000);
            if (start == end && line.karaokeAnimation == "no_wipe"
                && (!ir.value("required_features").toArray().contains("p07_zero_ruby_no_wipe")
                    || ir.value("skia_layout").toObject().value("zero_ruby_no_wipe_schema").toDouble(-1) != 1))
                throw BackendError("Skia 零时长注音整字变色缺少版本化能力声明");
            const auto shiftedStart = std::int64_t(start) + offset, shiftedEnd = std::int64_t(end) + offset;
            if (shiftedStart < -172800000 || shiftedEnd > 172800000)
                throw BackendError("Skia 注音最终时间偏移超限");
            auto glyph = glyphs.cell(cell, drawingText, int(shiftedStart), int(shiftedEnd),
                before, after, wipePad);
            if (line.karaokeAnimation == "utopia") {
                const auto left = cell.value("utopia_layout_left");
                if (!left.isDouble() || !std::isfinite(left.toDouble()) || std::abs(left.toDouble()) > 1e6)
                    throw BackendError("Skia Utopia 注音缺少有效的单元轴心");
                glyph.pivot.layoutLeft = float(left.toDouble() * glyphs.dpr);
                glyph.pivot.baseline = float(cell.value("baseline_y").toDouble() * glyphs.dpr);
                glyph.pivot.wipePad = lower ? float(int(innerWidth) / 2) : float(int(paint.rubyStrokeWidth) / 2);
            } else if (cell.contains("utopia_layout_left")) {
                throw BackendError("Skia 注音单元轴心缺少 Utopia 能力声明");
            }
            if (!glyph.path.isEmpty()) {
                using namespace ::subtitle::skia;
                if (primary.width)
                    glyph.outline = SolidOutline{prepareOutlinePath(glyph.path, innerWidth), primary.before,
                                                primary.after, protectFill, innerWidth};
                if (secondary.width)
                    glyph.outerOutline = SolidOutline{prepareOutlinePath(glyph.path, outerWidth), secondary.before,
                                                     secondary.after,
                                                     protectFill || (primary.width && primary.hasAlpha()), outerWidth};
            }
            if (vertical) {
                const auto top = cell.value("wipe_top"), bottom = cell.value("wipe_bottom");
                const auto ink = cell.value("metrics").toObject().value("ink").toArray();
                const auto baseline = cell.value("baseline_y").toDouble();
                if (!ir.value("required_features").toArray().contains("p05_vertical_ruby")
                    || (verticalSchema != 1 && verticalSchema != 2)
                    || (verticalSchema == 1 && units.toArray().size() != 1)
                    || lower || !verticalRubyScalar(drawingText)
                    || cell.contains("rotation_pivot_x")
                    || !top.isDouble() || !bottom.isDouble() || !std::isfinite(top.toDouble())
                    || !std::isfinite(bottom.toDouble()) || bottom.toDouble() <= top.toDouble()
                    || std::abs(top.toDouble()) > 1e6 || std::abs(bottom.toDouble()) > 1e6
                    || ink.size() != 4 || std::abs(top.toDouble() - baseline - ink[1].toDouble()) > 1e-4
                    || std::abs(bottom.toDouble() - baseline - ink[3].toDouble()) > 1e-4)
                    throw BackendError("Skia 竖排注音要求直立读音单元及有效墨迹边界");
                glyph.top = float(top.toDouble() * glyphs.dpr);
                glyph.bottom = float(bottom.toDouble() * glyphs.dpr);
            } else if (cell.contains("wipe_top") || cell.contains("wipe_bottom")) {
                throw BackendError("Skia 横排注音单元不能携带竖排范围");
            }
            if (const auto end = resolvedSkiaWipeEnd(cell, ir)) {
                glyph.wipePoints = {{int(shiftedStart), 0.f}, {int(shiftedEnd), *end}};
                glyph.adjustedWipeEnd = true;
            }
            cells.push_back(std::move(glyph));
        }
        const bool hasGlow = glow.before.radius > 0 || glow.after.radius > 0;
        groups.push_back({std::move(cells), shadow, hasGlow ? std::optional(glow) : std::nullopt});
    }
    return groups;
}
}
