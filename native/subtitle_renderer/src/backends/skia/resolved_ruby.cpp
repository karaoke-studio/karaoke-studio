#include "resolved_ruby.h"
#include "resolved_wipe.h"
#include "vertical_text.h"
#include "../render_backend.h"
#include "../text_semantics.h"
#include <QtCore/QJsonArray>
#include <algorithm>
#include <cmath>
#include <limits>

namespace krok::subtitle::native {
namespace {
int integer(const QJsonValue& value, int minimum, int maximum) {
    const double n = value.toDouble();
    if (!value.isDouble() || !std::isfinite(n) || std::floor(n) != n || n < minimum || n > maximum)
        throw BackendError("Skia 注音最终时钟或样式索引无效");
    return int(n);
}
double pivotCoordinate(const QJsonValue& value) {
    const double n = value.toDouble();
    if (!value.isDouble() || !std::isfinite(n) || std::abs(n) > 1e6)
        throw BackendError("Skia Utopia 注音缺少有效的单元轴心");
    return n;
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
::subtitle::skia::GlowState lowerGlowState(const QJsonObject& glow, const char* phase, double dpr) {
    if (!glow.contains(phase)) return {};
    const auto state = glow.value(phase).toObject();
    if (state.size() != 3)
        throw BackendError("Skia 下方注音实色光晕参数无效");
    const double radius = integer(state.value("radius"), 1, 532) * dpr;
    if (radius > 532.) throw BackendError("Skia 下方注音实色光晕半径超限");
    return {int(std::lround(radius)), integer(state.value("concentration"), 0, 2), rgbaPaint(state.value("color"))};
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
std::vector<::subtitle::skia::ResolvedRubyGroup> resolvedSkiaRubyGroups(
        const QJsonObject& source, const QJsonObject& ir, const ResolvedGlyphContext& glyphs,
    const RenderScene& scene, int lineIndex, int offset, RubyTransition transition,
        std::size_t& totalCells, std::size_t& totalGroups) {
    if (!source.contains("ruby_groups")) return {};
    if (!ir.value("required_features").toArray().contains("p05_resolved_ruby")
        || ir.value("skia_layout").toObject().value("ruby_schema").toDouble(-1) != 1
        || !source.value("ruby_groups").isArray())
        throw BackendError("Skia 注音最终布局缺少版本或能力声明");
    std::vector<::subtitle::skia::ResolvedRubyGroup> groups;
    const auto& line = scene.lines[lineIndex];
    const bool vertical = scene.lineStyles[lineIndex].vertical;
    const auto features = ir.value("required_features").toArray();
    const bool allowOutlines = features.contains("p06_ruby_solid_outline");
    const bool verticalFade = vertical && features.contains("p07_vertical_decoration_fade")
        && (line.entryAnimation == "none" || line.entryAnimation == "fade")
        && (line.exitAnimation == "none" || line.exitAnimation == "fade");
    const bool staticLine = line.entryAnimation == "none" && line.exitAnimation == "none";
    const bool allowVerticalDecoration = features.contains("p06_vertical_ruby_decoration")
        && line.karaokeAnimation == "none" && (staticLine || verticalFade)
        && !line.wipeReverse && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty();
    const bool allowShadow = features.contains("p06_ruby_shadow")
        && (!vertical || allowVerticalDecoration) && line.karaokeAnimation != "no_wipe" && !line.zoomPulseEnabled
        && (staticLine || verticalFade);
    const bool zoom = line.zoomPulseEnabled && features.contains("p07_ruby_zoom_pulse");
    const bool allowSingingGlow = features.contains("p07_singing_glow")
        && line.karaokeAnimation == "utopia"
        && (!line.zoomPulseEnabled || features.contains("p07_zoom_pulse_decoration"));
    const bool lineGlow = allowSingingGlow && features.contains("p07_utopia_line_animation");
    const bool noiseGlow = features.contains("p07_noise_composition")
        && (line.entryAnimation == "noise" || line.exitAnimation == "noise");
    const bool noWipeGlow = line.karaokeAnimation == "no_wipe" && features.contains("p07_no_wipe_decoration");
    const bool allowGlow = features.contains("p06_ruby_glow")
        && (!vertical || allowVerticalDecoration) && (line.karaokeAnimation == "none" || allowSingingGlow || noWipeGlow)
        && (lineGlow || transition != RubyTransition::None || noiseGlow || staticLine || verticalFade)
        && scene.charStyles.empty();
    const bool lowerWipe = line.karaokeAnimation == "none" || (line.karaokeAnimation == "utopia"
        && features.contains("p07_ruby_singing_scale"));
    const bool allowLowerGlow = features.contains("p06_lower_ruby_glow") && !vertical && staticLine
        && lowerWipe && !line.wipeReverse && !line.zoomPulseEnabled
        && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty() && scene.charStyles.empty();
    const double verticalSchema = ir.value("skia_layout").toObject().value("vertical_ruby_schema").toDouble(-1);
    const bool allowVerticalShift = features.contains("p05_vertical_shifted_ruby");
    const bool allowVerticalRotation = features.contains("p05_vertical_rotated_ruby");
    const bool allowVerticalOutlines = features.contains("p06_vertical_ruby_outline");
    const auto rubyGroups = source.value("ruby_groups").toArray();
    const bool multipleVertical = vertical && rubyGroups.size() > 1;
    if (multipleVertical && allowVerticalOutlines)
        throw BackendError("Skia 竖排描边暂不支持同列多组注音");
    int previousSpanEnd = 0;
    std::optional<::subtitle::skia::StaticGlow> lowerGlowSpec;
    double previousInkBottom = -std::numeric_limits<double>::infinity();
    if (rubyGroups.size() > 4096 - totalGroups)
        throw BackendError("Skia 注音组总数超限");
    for (const auto& value : rubyGroups) {
        if (!value.isObject() || ++totalGroups > 4096 || line.chars.empty())
            throw BackendError("Skia 注音组无效或总数超限");
        const auto object = value.toObject();
        const bool lower = object.value("lower").toBool();
        const bool animatedOwner = transition != RubyTransition::None;
        const bool spinComposition = transition == RubyTransition::Spin;
        const bool affineComposition = transition == RubyTransition::Affine;
        if ((animatedOwner && lower) || object.contains("fade_owner") != animatedOwner)
            throw BackendError("Skia 注音缺少有效的逐字淡化归属");
        std::optional<std::size_t> fadeOwner;
        if (animatedOwner)
            fadeOwner = integer(object.value("fade_owner"), 0, int(line.chars.size()) - 1);
        std::optional<SkPoint> spinPivot;
        if (object.contains("spin_pivot") != spinComposition)
            throw BackendError("Skia 注音翻转缺少最终轴心");
        if (spinComposition) {
            const auto pivot = object.value("spin_pivot").toArray();
            if (pivot.size() != 2) throw BackendError("Skia 注音翻转轴心无效");
            spinPivot = SkPoint::Make(float(pivotCoordinate(pivot[0]) * glyphs.dpr),
                                      float(pivotCoordinate(pivot[1]) * glyphs.dpr));
        }
        if (object.contains("affine_pivot_y") != affineComposition)
            throw BackendError("Skia 注音几何退场缺少纵向轴心");
        const float affineY = affineComposition
            ? float(pivotCoordinate(object.value("affine_pivot_y")) * glyphs.dpr) : 0.f;
        if ((zoom && lower) || object.contains("zoom_pivot_y") != zoom)
            throw BackendError("Skia 注音整字放大轴心与布局不一致");
        const double zoomCenter = zoom ? pivotCoordinate(object.value("zoom_pivot_y")) : 0;
        const int index = integer(object.value("style_cell"), 0, int(line.chars.size()) - 1);
        if (object.contains("span_end_cell") != multipleVertical || (multipleVertical && index < previousSpanEnd))
            throw BackendError("Skia 竖排同列注音的正文范围无效");
        if (multipleVertical)
            previousSpanEnd = integer(object.value("span_end_cell"), index + 1, int(line.chars.size()));
        const auto& paint = line.chars[index].styleIndex >= 0
            ? scene.charStyles[line.chars[index].styleIndex] : scene.lineStyles[lineIndex];
        const auto shadowState = [&](bool after) -> ::subtitle::skia::ShadowState {
            const auto kind = paint.rubyDecorationKindForState(after);
            if (lower || kind == "none" || kind == "glow") return {};
            const auto& decor = after ? paint.rubyAfterDecorPaint : paint.rubyBeforeDecorPaint;
            const float x = paint.rubyShadowOffsetXForState(after), y = paint.rubyShadowOffsetYForState(after);
            if (!allowShadow || kind != "shadow" || decor.mode != "solid" || !std::isfinite(x) || !std::isfinite(y))
                throw BackendError("Skia 上注音实色阴影缺少对应布局能力");
            return {true, x, y, argb(decor.color)};
        };
        const ::subtitle::skia::StaticShadow shadow{
            shadowState(false), shadowState(true), paint.rubyStrokeWidth, paint.rubyStroke2Width};
        const auto glowState = [&](bool after) -> ::subtitle::skia::GlowState {
            if (lower || paint.rubyDecorationKindForState(after) != "glow") return {};
            const auto& decor = after ? paint.rubyAfterDecorPaint : paint.rubyBeforeDecorPaint;
            const float radius = after ? paint.rubyGlowAfterRadius : paint.rubyGlowBeforeRadius;
            if (!allowGlow || decor.mode != "solid" || !std::isfinite(radius) || radius > 532.f)
                throw BackendError("Skia 上注音实色光晕缺少对应布局能力");
            return {int(std::lround(radius)), paint.rubyGlowConcentrationForState(after), argb(decor.color)};
        };
        ::subtitle::skia::StaticGlow glow{
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
        if (object.contains("outlines") && (!allowOutlines || (vertical && !allowVerticalOutlines) || outlines.isEmpty()
            || outlines.size() != int(outlines.contains("stroke")) + int(outlines.contains("stroke2"))))
                throw BackendError("Skia 注音描边要求有效的实色描边能力");
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
        if (object.contains("lower_glow")) {
            const auto lowerGlow = object.value("lower_glow").toObject();
            if (!lower || !allowLowerGlow || lowerGlow.isEmpty()
                || lowerGlow.size() != int(lowerGlow.contains("before")) + int(lowerGlow.contains("after")))
                throw BackendError("Skia 下方注音实色光晕缺少对应布局能力");
            glow = {lowerGlowState(lowerGlow, "before", glyphs.dpr),
                    lowerGlowState(lowerGlow, "after", glyphs.dpr), outerWidth};
            if (lowerGlowSpec && *lowerGlowSpec != glow)
                throw BackendError("Skia 同行下方注音当前仅支持相同的光晕样式");
            lowerGlowSpec = glow;
        }
        const double wipePad = (primary.width / 2) / glyphs.dpr;
        std::vector<::subtitle::skia::ResolvedCell> cells;
        double inkTop = std::numeric_limits<double>::infinity();
        double inkBottom = -std::numeric_limits<double>::infinity();
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
            if (cell.contains("affine_pivot_x") != affineComposition)
                throw BackendError("Skia 注音几何退场缺少横向轴心");
            if (affineComposition) {
                const float x = float(pivotCoordinate(cell.value("affine_pivot_x")) * glyphs.dpr);
                glyph.pivot.layoutLeft = glyph.pivot.layoutRight = x;
                glyph.pivot.centerY = affineY;
            }
            if (cell.contains("utopia_layout_right") != zoom)
                throw BackendError("Skia 注音单元跨度与整字放大能力不一致");
            if (line.karaokeAnimation == "utopia") {
                const double left = pivotCoordinate(cell.value("utopia_layout_left"));
                glyph.pivot.layoutLeft = float(left * glyphs.dpr);
                glyph.pivot.baseline = float(cell.value("baseline_y").toDouble() * glyphs.dpr);
                glyph.pivot.wipePad = lower ? float(int(innerWidth) / 2) : float(int(paint.rubyStrokeWidth) / 2);
                if (zoom) {
                    const double right = pivotCoordinate(cell.value("utopia_layout_right"));
                    if (right < left) throw BackendError("Skia 注音整字放大单元跨度无效");
                    glyph.pivot.layoutRight = float(right * glyphs.dpr);
                    glyph.pivot.centerY = float(zoomCenter * glyphs.dpr);
                }
            } else if (cell.contains("utopia_layout_left")) {
                throw BackendError("Skia 注音单元轴心缺少 Utopia 能力声明");
            }
            if (vertical) {
                const auto top = cell.value("wipe_top"), bottom = cell.value("wipe_bottom");
                const auto ink = cell.value("metrics").toObject().value("ink").toArray();
                const auto baseline = cell.value("baseline_y").toDouble();
                const auto scalars = drawingText.toUcs4();
                const auto scalar = scalars.size() == 1 ? scalars.front() : char32_t{};
                const bool rotated = allowVerticalRotation && verticalRotatedScalar(scalar);
                const bool shifted = allowVerticalShift && verticalShiftedScalar(scalar);
                if (!ir.value("required_features").toArray().contains("p05_vertical_ruby")
                    || (verticalSchema != 1 && verticalSchema != 2)
                    || (verticalSchema == 1 && units.toArray().size() != 1)
                    || lower || !(verticalUprightScalar(scalar) || shifted || rotated)
                    || cell.contains("rotation_pivot_x") || cell.contains("vertical_rotation_pivot") != rotated
                    || !top.isDouble() || !bottom.isDouble() || !std::isfinite(top.toDouble())
                    || !std::isfinite(bottom.toDouble()) || bottom.toDouble() <= top.toDouble()
                    || std::abs(top.toDouble()) > 1e6 || std::abs(bottom.toDouble()) > 1e6
                    || ink.size() != 4)
                    throw BackendError("Skia 竖排注音字形或墨迹范围无效");
                double inkMin = baseline + ink[1].toDouble(), inkMax = baseline + ink[3].toDouble();
                if (rotated) {
                    const auto pivot = cell.value("vertical_rotation_pivot").toArray();
                    if (pivot.size() != 2) throw BackendError("Skia 竖排注音旋转轴无效");
                    const double x = pivotCoordinate(pivot[0]), y = pivotCoordinate(pivot[1]);
                    const auto origin = cell.value("origin_x").toDouble();
                    inkMin = y + origin + ink[0].toDouble() - x;
                    inkMax = y + origin + ink[2].toDouble() - x;
                    glyph.path = glyph.path.makeTransform(SkMatrix::RotateDeg(
                        90.f, {float(x * glyphs.dpr), float(y * glyphs.dpr)}));
                }
                if (std::abs(top.toDouble() - inkMin) > 1e-4 || std::abs(bottom.toDouble() - inkMax) > 1e-4)
                    throw BackendError("Skia 竖排注音走字范围与墨迹不一致");
                glyph.top = float(top.toDouble() * glyphs.dpr);
                glyph.bottom = float(bottom.toDouble() * glyphs.dpr);
                inkTop = std::min(inkTop, top.toDouble());
                inkBottom = std::max(inkBottom, bottom.toDouble());
            } else if (cell.contains("wipe_top") || cell.contains("wipe_bottom") || cell.contains("vertical_rotation_pivot")) {
                throw BackendError("Skia 横排注音单元不能携带竖排范围");
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
            if (const auto end = resolvedSkiaWipeEnd(cell, ir)) {
                glyph.wipePoints = {{int(shiftedStart), 0.f}, {int(shiftedEnd), *end}};
                glyph.adjustedWipeEnd = true;
            }
            cells.push_back(std::move(glyph));
        }
        if (multipleVertical && inkTop < previousInkBottom)
            throw BackendError("Skia 竖排同列注音的墨迹范围重叠");
        previousInkBottom = inkBottom;
        const bool hasGlow = glow.before.radius > 0 || glow.after.radius > 0;
        groups.push_back({std::move(cells), shadow, hasGlow ? std::optional(glow) : std::nullopt,
                          fadeOwner, spinPivot, affineComposition, lower});
    }
    return groups;
}
}
