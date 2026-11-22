#include "resolved_ruby.h"
#include "image_fill_cache.h"
#include "resolved_role_paints.h"
#include "resolved_wipe.h"
#include "scene_input.h"
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
                    float width, double dpr, bool allowGradient, bool allowSplit) {
    if (std::abs(outline.width * dpr - width) > 1e-4) return false;
    const auto supported = [allowGradient, allowSplit](const PaintStyle& paint) {
        return paint.mode == "solid" || (allowGradient
            && (paint.mode == "gradient_vertical" || (allowSplit && paint.mode == "split_vertical")));
    };
    return !outline.width || (supported(before) && supported(after)
        && outline.before == argb(before.color) && outline.after == argb(after.color));
}
bool rubyFillGradientMode(const PaintStyle& fill) {
    return fill.mode == "gradient_horizontal" || fill.mode == "gradient_vertical" || fill.mode == "split_vertical";
}

std::array<sk_sp<SkShader>, 2> rubyGradients(const std::array<const PaintStyle*, 2>& paints,
    bool shareHorizontal, const QJsonObject& line,
    const SkRect& ink, float outlinePad, double dpr,
    const SkRect* roleBounds) {
    std::array<sk_sp<SkShader>, 2> result;
    if (ink.isEmpty()) return result;
    for (int phase = 0; phase < 2; ++phase) {
        if (!paints[phase]) continue;
        const auto& fill = *paints[phase];
        if (!rubyFillGradientMode(fill)) continue;
        auto bounds = ink;
        if (fill.mode == "gradient_horizontal" && shareHorizontal) {
            if (roleBounds) {
                bounds.fLeft = roleBounds->left();
                bounds.fRight = roleBounds->right();
            } else {
                const auto domain = line.value("body_gradient_bounds").toArray();
                if (domain.size() != 4) throw BackendError("Skia 上注音共享横向渐变缺少正文范围");
                bounds.fLeft = float(pivotCoordinate(domain[0]) * dpr);
                bounds.fRight = float(pivotCoordinate(domain[2]) * dpr);
            }
            if (bounds.fRight <= bounds.fLeft) throw BackendError("Skia 上注音共享横向渐变范围无效");
        }
        result[phase] = ::subtitle::skia::lineGradient(fill, bounds, outlinePad);
    }
    return result;
}
bool validPhases(const QJsonObject& layer) {
    return !layer.isEmpty() && layer.size() == int(layer.contains("before")) + int(layer.contains("after"));
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
::subtitle::skia::ShadowState lowerShadowState(const QJsonObject& shadow, const char* phase, double dpr) {
    if (!shadow.contains(phase)) return {};
    const auto state = shadow.value(phase).toObject();
    const auto offset = state.value("offset").toArray();
    if (state.size() != 2 || offset.size() != 2)
        throw BackendError("Skia 下方注音实色阴影参数无效");
    return {true, float(integer(offset[0], -4096, 4096) * dpr),
            float(integer(offset[1], -4096, 4096) * dpr), rgbaPaint(state.value("color"))};
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
        const RenderScene& scene, int lineIndex, int offset, RubyTransition transition, SkiaImageFillCache& imageCache,
        GlowPaintSupport glowPaints,
        const RolePaintBounds* roleBounds, RubyDecorationMotion decorationMotion, std::size_t& totalCells,
        std::size_t& totalGroups) {
    if (!source.contains("ruby_groups")) return {};
    if (!ir.value("required_features").toArray().contains("p05_resolved_ruby")
        || ir.value("skia_layout").toObject().value("ruby_schema").toDouble(-1) != 1
        || !source.value("ruby_groups").isArray())
        throw BackendError("Skia 注音最终布局缺少版本或能力声明");
    std::vector<::subtitle::skia::ResolvedRubyGroup> groups;
    const auto& line = scene.lines[lineIndex];
    const bool vertical = scene.lineStyles[lineIndex].vertical;
    const bool rtl = scene.lineStyles[lineIndex].rightToLeft;
    const auto features = ir.value("required_features").toArray();
    const bool allowOutlines = features.contains("p06_ruby_solid_outline");
    const bool fadeOnly = (line.entryAnimation == "none" || line.entryAnimation == "fade")
        && (line.exitAnimation == "none" || line.exitAnimation == "fade");
    const bool verticalFade = vertical && features.contains("p07_vertical_decoration_fade") && fadeOnly;
    const bool staticLine = line.entryAnimation == "none" && line.exitAnimation == "none";
    const bool roleLineFade = features.contains("p07_role_line_fade") && fadeOnly;
    const auto& scanlineStyle = scene.lineStyles[lineIndex];
    const bool fixedScanlinePaint = scanlineStyle.scanlineMode == "default" || scanlineStyle.scanlineMode == "color";
    const bool fadedUtopiaScanline = usesFadedUtopiaScanline(line, scanlineStyle);
    const bool roleFadedScanline = fadedUtopiaScanline && fixedScanlinePaint && line.bursts.empty()
        && features.contains("p07_inline_role_faded_scanline");
    const bool fadedScanlineGradient = fadedUtopiaScanline && fixedScanlinePaint
        && features.contains("p06_faded_scanline_gradient_fill")
        && (line.bursts.empty() ? features.contains("p07_faded_scanline_prefix_guides")
                                : features.contains("p07_faded_scanline_singing_particles"));
    const bool brightenedScanlineGradient = fadedUtopiaScanline && line.bursts.empty()
        && scanlineStyle.scanlineMode == "brighten" && features.contains("p06_faded_scanline_palette_gradient");
    const bool avatarOutline = features.contains("p08_inline_bitmap_guides") && fadeOnly && !line.scanlineEnabled
        && !line.strokeFlashEnabled && line.bursts.empty();
    const bool allowOutlineGradient = features.contains("p06_ruby_outline_gradient")
        && !vertical && !rtl && !line.wipeReverse && !line.zoomPulseEnabled && !line.strokeFlashEnabled
        && (!line.scanlineEnabled || fixedScanlinePaint || brightenedScanlineGradient) && (scene.charStyles.empty() || avatarOutline)
        && transition == RubyTransition::None;
    const bool gradientLineFade = !vertical && fadeOnly && features.contains("p06_gradient_glow")
        && features.contains("p07_ruby_line_animation");
    const bool verticalNoWipe = line.karaokeAnimation == "no_wipe"
        && (staticLine || (features.contains("p07_vertical_decoration_no_wipe_animation")
                           && (verticalFade || decorationMotion.upper)));
    const bool verticalMixedNoWipe = features.contains("p07_vertical_mixed_decoration_no_wipe");
    const bool verticalGlowNoWipe = verticalNoWipe && (features.contains("p07_vertical_glow_no_wipe") || verticalMixedNoWipe);
    const bool verticalShadowNoWipe = verticalNoWipe && (features.contains("p07_vertical_shadow_no_wipe") || verticalMixedNoWipe);
    const bool verticalSinging = vertical && line.karaokeAnimation == "utopia"
        && features.contains("p07_vertical_singing_scale");
    const bool verticalSingingShadow = verticalSinging && features.contains("p07_vertical_ruby_singing_shadow");
    const bool allowVerticalDecoration = features.contains("p06_vertical_ruby_decoration")
        && ((line.karaokeAnimation == "none" && (staticLine || verticalFade || decorationMotion.upper))
            || verticalSinging || verticalGlowNoWipe || verticalShadowNoWipe)
        && !line.wipeReverse && (!line.scanlineEnabled || features.contains("p07_vertical_scanline_decoration"))
        && !line.strokeFlashEnabled
        && (line.bursts.empty() || features.contains("p07_vertical_decorated_singing_particles"));
    const bool rtlShadow = rtl && features.contains("p06_rtl_ruby_shadow")
        && (line.karaokeAnimation == "none" || line.karaokeAnimation == "no_wipe");
    const bool allowShadow = features.contains("p06_ruby_shadow")
        && (!rtl || rtlShadow) && (!vertical || (allowVerticalDecoration && (!verticalSinging || verticalSingingShadow)))
        && (line.karaokeAnimation != "no_wipe" || verticalShadowNoWipe || rtlShadow || decorationMotion.upper)
        && (!line.zoomPulseEnabled || verticalSingingShadow)
        && (staticLine || verticalFade || decorationMotion.upper || decorationMotion.singingGradient
            || (verticalSingingShadow && features.contains("p07_vertical_singing_line_animation")));
    const bool zoom = !vertical && line.zoomPulseEnabled && features.contains("p07_ruby_zoom_pulse");
    const bool allowSingingGlow = features.contains("p07_singing_glow")
        && line.karaokeAnimation == "utopia"
        && (!line.zoomPulseEnabled || features.contains("p07_zoom_pulse_decoration"));
    const bool lineGlow = allowSingingGlow && features.contains("p07_utopia_line_animation");
    const bool unscaledScanlineGlow = line.scanlineEnabled && line.karaokeAnimation == "none"
        && features.contains("p07_scanline_unscaled_line_animation");
    const bool noiseGlow = features.contains("p07_noise_composition")
        && (line.entryAnimation == "noise" || line.exitAnimation == "noise");
    const bool noWipeGlow = line.karaokeAnimation == "no_wipe" && features.contains("p07_no_wipe_decoration");
    const bool roleGlow = features.contains("p06_ruby_role_glow") && !vertical && (staticLine || roleLineFade)
        && (line.karaokeAnimation == "none" || (allowSingingGlow && features.contains("p07_role_singing_glow")))
        && !line.wipeReverse && !line.zoomPulseEnabled
        && (!line.scanlineEnabled || roleFadedScanline) && !line.strokeFlashEnabled && line.bursts.empty();
    const bool latinGuideGlow = lineGlow && features.contains("p06_latin_primary_outline")
        && features.contains("p05_vector_guide_composition") && !features.contains("p06_role_solid_outlines");
    const bool allowGlow = features.contains("p06_ruby_glow")
        && (!vertical || allowVerticalDecoration) && (line.karaokeAnimation == "none" || allowSingingGlow || noWipeGlow)
        && (lineGlow || transition != RubyTransition::None || noiseGlow || staticLine || verticalFade
            || decorationMotion.upper || decorationMotion.lower || unscaledScanlineGlow || roleLineFade || gradientLineFade)
        && (scene.charStyles.empty() || roleGlow || latinGuideGlow);
    const bool lowerWipe = line.karaokeAnimation == "none" || (line.karaokeAnimation == "utopia"
        && features.contains("p07_ruby_singing_scale")) || (line.karaokeAnimation == "no_wipe"
        && (staticLine || decorationMotion.lower) && features.contains("p07_lower_ruby_no_wipe_decoration"));
    const bool lowerScanline = line.scanlineEnabled && line.karaokeAnimation != "no_wipe"
        && features.contains("p07_lower_ruby_scanline")
        && (staticLine || (decorationMotion.lower && features.contains("p07_lower_ruby_scanline_line_animation")));
    const bool allowLowerDecoration = !vertical && (staticLine || decorationMotion.lower)
        && lowerWipe && !line.wipeReverse && !line.zoomPulseEnabled
        && (!line.scanlineEnabled || lowerScanline) && !line.strokeFlashEnabled && line.bursts.empty() && scene.charStyles.empty();
    const double verticalSchema = ir.value("skia_layout").toObject().value("vertical_ruby_schema").toDouble(-1);
    const bool allowVerticalShift = features.contains("p05_vertical_shifted_ruby");
    const bool allowVerticalRotation = features.contains("p05_vertical_rotated_ruby");
    const bool allowVerticalOutlines = features.contains("p06_vertical_ruby_outline");
    const auto rubyGroups = source.value("ruby_groups").toArray();
    const bool multipleVertical = vertical && rubyGroups.size() > 1;
    if (multipleVertical && allowVerticalOutlines && !features.contains("p06_vertical_ruby_group_paint"))
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
        if (rtl && lower) throw BackendError("Skia 右至左单元排列暂不支持下方注音组合");
        const bool utopiaExit = transition == RubyTransition::UtopiaExit;
        const bool animatedOwner = transition != RubyTransition::None && !utopiaExit;
        const bool spinComposition = transition == RubyTransition::Spin;
        const bool affineComposition = transition == RubyTransition::Affine || utopiaExit;
        if ((animatedOwner && lower) || object.contains("fade_owner") != animatedOwner)
            throw BackendError("Skia 注音缺少有效的逐字淡化归属");
        std::optional<std::size_t> fadeOwner;
        if (animatedOwner)
            fadeOwner = integer(object.value("fade_owner"), 0, int(line.chars.size()) - 1);
        if ((utopiaExit && lower) || object.contains("utopia_exit_owner") != utopiaExit)
            throw BackendError("Skia 注音 Utopia 退场缺少有效归属");
        std::optional<std::size_t> utopiaExitOwner;
        if (utopiaExit) utopiaExitOwner = integer(object.value("utopia_exit_owner"), 0, int(line.chars.size()) - 1);
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
        const int role = line.chars[index].styleIndex;
        const auto& paint = role >= 0 ? scene.charStyles[role] : scene.lineStyles[lineIndex];
        const auto shadowState = [&](bool after) -> ::subtitle::skia::ShadowState {
            const auto kind = paint.rubyDecorationKindForState(after);
            if (lower || kind == "none" || kind == "glow") return {};
            const auto& decor = after ? paint.rubyAfterDecorPaint : paint.rubyBeforeDecorPaint;
            const float x = paint.rubyShadowOffsetXForState(after), y = paint.rubyShadowOffsetYForState(after);
            if (!allowShadow || kind != "shadow" || decor.mode != "solid" || !std::isfinite(x) || !std::isfinite(y))
                throw BackendError("Skia 上注音实色阴影缺少对应布局能力");
            return {true, x, y, argb(decor.color)};
        };
        ::subtitle::skia::StaticShadow shadow{
            shadowState(false), shadowState(true), paint.rubyStrokeWidth, paint.rubyStroke2Width};
        const auto glowState = [&](bool after) -> ::subtitle::skia::GlowState {
            if (lower || paint.rubyDecorationKindForState(after) != "glow") return {};
            const auto& decor = after ? paint.rubyAfterDecorPaint : paint.rubyBeforeDecorPaint;
            const float radius = after ? paint.rubyGlowAfterRadius : paint.rubyGlowBeforeRadius;
            if (!allowGlow || !supportsSkiaGlowPaint(decor, glowPaints) || !std::isfinite(radius) || radius > 532.f)
                throw BackendError("Skia 上注音光晕缺少对应画笔或布局能力");
            return {int(std::lround(radius)), paint.rubyGlowConcentrationForState(after), argb(decor.color)};
        };
        ::subtitle::skia::StaticGlow glow{
            glowState(false), glowState(true), paint.rubyStrokeWidth + paint.rubyStroke2Width};
        const bool gradientFill = !lower && (rubyFillGradientMode(paint.rubyBeforeFillPaint)
                                           || rubyFillGradientMode(paint.rubyAfterFillPaint));
        const bool imageFill = !lower && (paint.rubyBeforeFillPaint.mode == "image" || paint.rubyAfterFillPaint.mode == "image");
        const bool allowFillPaint = !vertical && (staticLine || roleLineFade) && lowerWipe && line.karaokeAnimation != "no_wipe"
            && !line.wipeReverse && !line.zoomPulseEnabled && !line.scanlineEnabled && !line.strokeFlashEnabled
            && line.bursts.empty();
        const bool allowRoleFill = roleBounds && features.contains("p06_ruby_role_paints")
            && (line.karaokeAnimation == "none" || features.contains("p07_role_singing_scale"));
        const bool allowGradientFill = allowFillPaint && (allowRoleFill ||
            (scene.charStyles.empty() && features.contains("p06_ruby_fill_gradient")));
        const bool allowImageFill = allowFillPaint && (allowRoleFill ||
            (scene.charStyles.empty() && features.contains("p06_ruby_fill_image")));
        const auto validFill = [&](const auto& fill) {
            return fill.mode == "solid" || (allowGradientFill && rubyFillGradientMode(fill))
                || (roleFadedScanline && allowRoleFill && fill.mode == "gradient_vertical")
                || ((fadedScanlineGradient || brightenedScanlineGradient || decorationMotion.singingGradient)
                    && scene.charStyles.empty()
                    && features.contains("p06_ruby_fill_gradient")
                    && fill.mode == "gradient_vertical")
                || (allowImageFill && fill.mode == "image");
        };
        if (!lower && (!validFill(paint.rubyBeforeFillPaint) || !validFill(paint.rubyAfterFillPaint)))
            throw BackendError("Skia 注音最终绘制缺少对应画笔能力");
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
            if (!matchesOutline(primary, paint.rubyBeforeStrokePaint, paint.rubyAfterStrokePaint,
                                width, glyphs.dpr, allowOutlineGradient, avatarOutline)
                || !matchesOutline(secondary, paint.rubyBeforeStroke2Paint, paint.rubyAfterStroke2Paint,
                                   width2, glyphs.dpr, allowOutlineGradient, avatarOutline))
                throw BackendError("Skia 上注音描边与样式或画笔能力不一致");
        }
        using ::subtitle::skia::paintNeedsBodyProtection;
        const bool gradientOutlines = !lower && allowOutlineGradient && (primary.width || secondary.width);
        const bool protectPrimary = primary.width && (primary.hasAlpha() || (gradientOutlines
            && (paintNeedsBodyProtection(paint.rubyBeforeStrokePaint) || paintNeedsBodyProtection(paint.rubyAfterStrokePaint))));
        const bool protectFill = imageFill || (before >> 24) < 255 || (after >> 24) < 255 || (gradientFill
            && (::subtitle::skia::paintNeedsBodyProtection(paint.rubyBeforeFillPaint)
                || ::subtitle::skia::paintNeedsBodyProtection(paint.rubyAfterFillPaint)));
        const float innerWidth = float(primary.width * glyphs.dpr);
        const float secondaryWidth = float(secondary.width * glyphs.dpr);
        const float outerWidth = float((primary.width + secondary.width) * glyphs.dpr);
        const auto lowerGlow = object.value("lower_glow").toObject();
        if (object.contains("lower_glow")) {
            if (!lower || !allowLowerDecoration || !features.contains("p06_lower_ruby_glow") || !validPhases(lowerGlow))
                throw BackendError("Skia 下方注音实色光晕缺少对应布局能力");
            glow = {lowerGlowState(lowerGlow, "before", glyphs.dpr),
                    lowerGlowState(lowerGlow, "after", glyphs.dpr), outerWidth};
            if (lowerGlowSpec && *lowerGlowSpec != glow)
                throw BackendError("Skia 同行下方注音当前仅支持相同的光晕样式");
            lowerGlowSpec = glow;
        }
        if (object.contains("lower_shadow")) {
            const auto lowerShadow = object.value("lower_shadow").toObject();
            if (!lower || !allowLowerDecoration || !features.contains("p06_lower_ruby_shadow") || !validPhases(lowerShadow)
                || (lowerGlow.contains("before") && lowerShadow.contains("before"))
                || (lowerGlow.contains("after") && lowerShadow.contains("after")))
                throw BackendError("Skia 下方注音实色阴影缺少对应布局能力或同一阶段重复装饰");
            shadow = {lowerShadowState(lowerShadow, "before", glyphs.dpr),
                      lowerShadowState(lowerShadow, "after", glyphs.dpr), innerWidth, secondaryWidth};
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
            if (line.karaokeAnimation == "utopia" && !vertical) {
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
            if (cell.contains("vertical_singing_pivot") != verticalSinging)
                throw BackendError("Skia 竖排注音轴心与唱字放大能力不匹配");
            if (verticalSinging) {
                const auto pivot = cell.value("vertical_singing_pivot").toArray();
                if (pivot.size() != 2) throw BackendError("Skia 竖排注音唱字轴心无效");
                glyph.pivot.layoutLeft = float(pivotCoordinate(pivot[0]) * glyphs.dpr);
                glyph.pivot.baseline = float(pivotCoordinate(pivot[1]) * glyphs.dpr);
                if (line.zoomPulseEnabled) {
                    glyph.pivot.layoutRight = glyph.pivot.layoutLeft;
                    glyph.pivot.centerY = glyph.pivot.baseline;
                }
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
                                                     protectFill || protectPrimary, outerWidth};
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
        SkRect ink = SkRect::MakeEmpty();
        if (gradientFill || gradientOutlines || (hasGlow && glowPaints != GlowPaintSupport::Solid))
            for (const auto& cell : cells)
                if (!cell.path.isEmpty()) ink.join(cell.path.computeTightBounds());
        const auto outlinePad = ::subtitle::skia::outlineVisualPad(innerWidth, secondaryWidth, float(glyphs.dpr));
        auto gradients = gradientFill ? rubyGradients({&paint.rubyBeforeFillPaint, &paint.rubyAfterFillPaint},
            paint.rubyHorizontalGradientWithMain, source, ink, outlinePad, glyphs.dpr,
            allowRoleFill ? &roleBounds->forRole(role) : nullptr)
            : std::array<sk_sp<SkShader>, 2>{};
        std::array<sk_sp<SkShader>, 2> outlineGradients, outerOutlineGradients;
        if (gradientOutlines && primary.width)
            outlineGradients = rubyGradients({&paint.rubyBeforeStrokePaint, &paint.rubyAfterStrokePaint},
                                           false, source, ink, outlinePad, glyphs.dpr, nullptr);
        if (gradientOutlines && secondary.width)
            outerOutlineGradients = rubyGradients({&paint.rubyBeforeStroke2Paint, &paint.rubyAfterStroke2Paint},
                                                false, source, ink, outlinePad, glyphs.dpr, nullptr);
        ::subtitle::skia::GlowGradients glowGradients;
        if (hasGlow && glowPaints != GlowPaintSupport::Solid) {
            glowGradients = rubyGradients({glow.before.radius > 0 ? &paint.rubyBeforeDecorPaint : nullptr,
                                           glow.after.radius > 0 ? &paint.rubyAfterDecorPaint : nullptr},
                                          paint.rubyHorizontalGradientWithMain, source, ink, outlinePad, glyphs.dpr,
                                          roleBounds ? &roleBounds->forRole(role) : nullptr);
        }
        std::array<::subtitle::skia::ImageFill, 2> images;
        if (imageFill) {
            images[0] = imageCache.fill(paint.rubyBeforeFillPaint, scene.layoutReferenceScale);
            images[1] = imageCache.fill(paint.rubyAfterFillPaint, scene.layoutReferenceScale);
        }
        ::subtitle::skia::ScanlineGradients scanlineGradients;
        if (!lower && brightenedScanlineGradient && (gradientFill || gradientOutlines)) {
            const ::subtitle::skia::ScanlinePaint highlight{
                ::subtitle::skia::ScanlineColorMode::Brighten, 0, scanlineStyle.scanlineBrightness};
            const PaintStyle* layers[]{
                secondary.width ? &paint.rubyBeforeStroke2Paint : nullptr,
                secondary.width ? &paint.rubyAfterStroke2Paint : nullptr,
                primary.width ? &paint.rubyBeforeStrokePaint : nullptr,
                primary.width ? &paint.rubyAfterStrokePaint : nullptr,
                &paint.rubyBeforeFillPaint, &paint.rubyAfterFillPaint,
            };
            for (int i = 0; i < 6; ++i)
                if (layers[i])
                    scanlineGradients[i / 2][i % 2] = ::subtitle::skia::scanlineGradient(*layers[i], ink, outlinePad, highlight);
        }
        groups.push_back({std::move(cells), shadow, hasGlow ? std::optional(glow) : std::nullopt,
                          fadeOwner, spinPivot, affineComposition, lower, std::move(gradients), std::move(images),
                          roleGlow && !lower ? role : -1, std::move(glowGradients), rtl, utopiaExitOwner,
                          std::move(outlineGradients), std::move(outerOutlineGradients), std::move(scanlineGradients)});
    }
    return groups;
}
}
