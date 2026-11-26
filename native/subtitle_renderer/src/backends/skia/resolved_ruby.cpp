#include "resolved_ruby.h"
#include "image_fill_cache.h"
#include "resolved_role_paints.h"
#include "resolved_wipe.h"
#include "scene_input.h"
#include "vertical_text.h"
#include "../render_backend.h"
#include "../text_semantics.h"
#include "../../scene/paint_projection.h"
#include <QtCore/QJsonArray>
#include <algorithm>
#include <cmath>
#include <limits>

namespace krok::subtitle::native {
namespace {
int integer(const QJsonValue& value, int minimum, int maximum,
            const char* failure = "Skia 注音最终时钟或样式索引无效") {
    const double n = value.toDouble();
    if (!value.isDouble() || !std::isfinite(n) || std::floor(n) != n || n < minimum || n > maximum)
        throw BackendError(failure);
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
                    float width, double dpr, bool allowGradient, bool allowHorizontal, bool allowSplit) {
    if (std::abs(outline.width * dpr - width) > 1e-4) return false;
    // An image outline reaches here only when its file cannot be decoded; the
    // GPU then paints the paint's own colour (createPaintBrush).
    const auto supported = [allowGradient, allowHorizontal, allowSplit](const PaintStyle& paint) {
        return paint.mode == "solid" || paint.mode == "image" || (allowGradient
            && (paint.mode == "gradient_vertical" || (allowHorizontal && paint.mode == "gradient_horizontal")
                || (allowSplit && paint.mode == "split_vertical")));
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
                // Without a body gradient domain the reading spans its own ink.
                const auto domain = line.value("body_gradient_bounds").toArray();
                if (domain.size() == 4) {
                    bounds.fLeft = float(pivotCoordinate(domain[0]) * dpr);
                    bounds.fRight = float(pivotCoordinate(domain[2]) * dpr);
                }
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
PaintStyle lowerGradient(const QJsonObject& authored, const char* phase, const QJsonObject& legacy = {}) {
    PaintStyle fill;
    QJsonArray stops;
    if (authored.contains(phase)) {
        const auto paint = authored.value(phase).toObject();
        fill.mode = paint.value("mode").toString().toStdString();
        if (paint.size() != 2 || !rubyFillGradientMode(fill) || legacy.contains(phase))
            throw BackendError("Skia 下方注音渐变画笔无效");
        stops = paint.value("stops").toArray();
    } else if (legacy.contains(phase)) {
        fill.mode = "gradient_vertical";
        stops = legacy.value(phase).toArray();
    } else {
        return fill;
    }
    if (stops.size() < 2 || stops.size() > 4096) throw BackendError("Skia 下方注音渐变色标无效");
    double previous = -1;
    for (const auto& stop : stops) {
        const auto pair = stop.toArray();
        const double position = pair.size() == 2 ? pair[0].toDouble(-1) : -1;
        if (!std::isfinite(position) || position < 0 || position > 100 || position < previous)
            throw BackendError("Skia 下方注音渐变色标位置无效");
        const auto color = rgbaPaint(pair[1]);
        fill.stops.push_back({float(position / 100), {std::uint8_t(color >> 16), std::uint8_t(color >> 8),
                                                      std::uint8_t(color), std::uint8_t(color >> 24)}});
        previous = position;
    }
    return fill;
}
PaintStyle lowerImage(const QJsonObject& payload, const char* phase) {
    const auto value = payload.value(phase);
    const auto image = value.toObject();
    const auto path = image.value("path");
    if (!value.isObject() || image.size() != 2 || !path.isString() || path.toString().size() > 32768
        || path.toString().contains(QChar(u'\0')))
        throw BackendError("Skia 下方注音图片画笔参数无效");
    protocol::PaintFillSpec fill;
    fill.mode = QStringLiteral("image");
    fill.imagePath = path.toString();
    fill.imageScalePct = integer(image.value("scale_pct"), 1, 1000, "Skia 下方注音图片画笔缩放参数无效");
    return scene::projectPaint(fill, QStringLiteral("#FFFFFF"));
}
::subtitle::skia::GlowState lowerGlowState(const QJsonObject& glow, const char* phase, double dpr) {
    if (!glow.contains(phase)) return {};
    const auto state = glow.value(phase).toObject();
    if (state.size() != 4)
        throw BackendError("Skia 下方注音实色光晕参数无效");
    const double radius = integer(state.value("radius"), 1, 532) * dpr;
    if (radius > 532.) throw BackendError("Skia 下方注音实色光晕半径超限");
    return {int(std::lround(radius)), integer(state.value("concentration"), 0, 2), rgbaPaint(state.value("color")),
            integer(state.value("strength"), 100, 400)};
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
std::vector<::subtitle::skia::ResolvedRubyGroup> resolvedSkiaRubyGroups(
        const QJsonObject& source, const QJsonObject& ir, const ResolvedGlyphContext& glyphs,
        const RenderScene& scene, int lineIndex, int offset, RubyTransition transition, SkiaImageFillCache& imageCache,
        GlowPaintSupport glowPaints,
        const RolePaintBounds* roleBounds, RubyDecorationMotion decorationMotion, std::size_t& totalCells,
        std::size_t& totalGroups) {
    if (!source.contains("ruby_groups")) return {};
    if (!source.value("ruby_groups").isArray())
        throw BackendError("Skia 注音最终布局缺少逐行注音组");
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
    // Outline gradients span the reading's own ink (or the shared role
    // domain), so they compose with every line layout and transition.
    const bool allowOutlineGradient = features.contains("p06_ruby_outline_gradient");
    const bool allowHorizontalOutline = features.contains("p06_ruby_horizontal_outline");
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
            || (roleLineFade && features.contains("p05_role_vector_guides"))
            || (verticalSingingShadow && features.contains("p07_vertical_singing_line_animation")));
    const bool allowShadowGradient = features.contains("p06_ruby_gradient_shadow")
        && roleBounds && !vertical && !rtl && !line.wipeReverse && staticLine && line.karaokeAnimation == "none"
        && !line.zoomPulseEnabled && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty();
    const bool allowShadowImage = features.contains("p06_ruby_image_shadow")
        && !vertical && !rtl && !line.wipeReverse && staticLine && line.karaokeAnimation == "none"
        && !line.zoomPulseEnabled && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty();
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
    // Karaoke modes only change the reading clock, not which paints a reading may use.
    const bool lowerWipe = true;
    const bool lowerScanline = line.scanlineEnabled && line.karaokeAnimation != "no_wipe"
        && features.contains("p07_lower_ruby_scanline")
        && (staticLine || (decorationMotion.lower && features.contains("p07_lower_ruby_scanline_line_animation")));
    const bool lowerScanlineParticles = lowerScanline && fadedUtopiaScanline && fixedScanlinePaint && !line.bursts.empty()
        && features.contains("p07_lower_ruby_scanline_singing_particles");
    // Lower reading decorations compose with the line's animation and effects.
    const bool allowLowerDecoration = lowerWipe;
    const double verticalSchema = ir.value("skia_layout").toObject().value("vertical_ruby_schema").toDouble(-1);
    const bool allowVerticalShift = features.contains("p05_vertical_shifted_ruby");
    const bool allowVerticalRotation = features.contains("p05_vertical_rotated_ruby");
    const bool allowVerticalOutlines = features.contains("p06_vertical_ruby_outline");
    const auto rubyGroups = source.value("ruby_groups").toArray();
    const bool multipleVertical = vertical && rubyGroups.size() > 1;
    const bool overlapping = source.contains("vertical_overlapping_ruby");
    if (overlapping && (!source.value("vertical_overlapping_ruby").isBool()
        || !source.value("vertical_overlapping_ruby").toBool()
        || !multipleVertical
        || !staticLine || line.karaokeAnimation != "none" || line.scanlineEnabled
        || line.zoomPulseEnabled || line.strokeFlashEnabled || !line.bursts.empty()))
        throw BackendError("Skia 竖排重叠注音缺少布局能力或走字模式无效");
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
        const bool utopiaExit = transition == RubyTransition::UtopiaExit && object.contains("utopia_exit_owner");
        // The IR names the transition owner; lower readings follow it like upper ones (as in D2D).
        // A Utopia exit reading still follows its owner through the entry.
        const bool animatedOwner = transition != RubyTransition::None && object.contains("fade_owner");
        // Pose data present in the IR selects the composed reading transition.
        // A warp phase (spin/drip) and a geometry phase can share one line;
        // the core poses each in its own window, so keep both kinds of data.
        const bool sharedGeometry = transition == RubyTransition::Affine || transition == RubyTransition::Translation;
        const bool spinComposition = (transition == RubyTransition::Spin || sharedGeometry)
            && object.contains("spin_pivot");
        const auto unitsCarry = [&](const char* key) {
            const auto values = object.value("units").toArray();
            return !values.isEmpty() && std::ranges::all_of(values, [&](const auto& value) {
                return value.toObject().contains(QLatin1String(key));
            });
        };
        const bool dripComposition = (transition == RubyTransition::Drip || sharedGeometry)
            && unitsCarry("drip_layout_right");
        const bool affineComposition = (transition == RubyTransition::Affine || utopiaExit)
            && object.contains("affine_pivot_y") && unitsCarry("affine_pivot_x");
        std::optional<std::size_t> fadeOwner;
        if (animatedOwner)
            fadeOwner = integer(object.value("fade_owner"), 0, int(line.chars.size()) - 1);
        std::optional<std::size_t> utopiaExitOwner;
        if (utopiaExit) utopiaExitOwner = integer(object.value("utopia_exit_owner"), 0, int(line.chars.size()) - 1);
        std::optional<std::pair<std::size_t, std::size_t>> utopiaExitSpan;
        if (transition == RubyTransition::UtopiaExit && object.contains("utopia_exit_first")
            && object.contains("utopia_exit_owner")) {
            const auto last = integer(object.value("utopia_exit_owner"), 0, int(line.chars.size()) - 1);
            utopiaExitSpan = std::pair<std::size_t, std::size_t>(
                integer(object.value("utopia_exit_first"), 0, int(last)), last);
        }
        std::optional<SkPoint> spinPivot;
        if (spinComposition) {
            const auto pivot = object.value("spin_pivot").toArray();
            if (pivot.size() != 2) throw BackendError("Skia 注音翻转轴心无效");
            spinPivot = SkPoint::Make(float(pivotCoordinate(pivot[0]) * glyphs.dpr),
                                      float(pivotCoordinate(pivot[1]) * glyphs.dpr));
        }
        const float affineY = affineComposition
            ? float(pivotCoordinate(object.value("affine_pivot_y")) * glyphs.dpr) : 0.f;
        // Readings zoom with their line whenever the IR prepared their pivot.
        const bool groupZoom = zoom && object.contains("zoom_pivot_y");
        const double zoomCenter = groupZoom ? pivotCoordinate(object.value("zoom_pivot_y")) : 0;
        const int index = integer(object.value("style_cell"), 0, int(line.chars.size()) - 1);
        if (object.contains("span_end_cell") != multipleVertical
            || (multipleVertical && !overlapping && index < previousSpanEnd))
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
            const bool imagePaint = decor.mode == "image";
            ::subtitle::skia::ShadowState state{true, x, y, (imagePaint || decor.mode == "solid") ? argb(decor.color) : 0u};
            if (imagePaint) state.image = imageCache.fill(decor, scene.layoutReferenceScale);
            return state;
        };
        ::subtitle::skia::StaticShadow shadow{
            shadowState(false), shadowState(true), paint.rubyStrokeWidth, paint.rubyStroke2Width};
        const auto glowState = [&](bool after) -> ::subtitle::skia::GlowState {
            if (lower || paint.rubyDecorationKindForState(after) != "glow") return {};
            const auto& decor = after ? paint.rubyAfterDecorPaint : paint.rubyBeforeDecorPaint;
            const float radius = after ? paint.rubyGlowAfterRadius : paint.rubyGlowBeforeRadius;
            return {int(std::lround(radius)), paint.rubyGlowConcentrationForState(after), argb(decor.color),
                    paint.glowStrengthPercent};
        };
        ::subtitle::skia::StaticGlow glow{
            glowState(false), glowState(true), paint.rubyStrokeWidth + paint.rubyStroke2Width};
        const auto lowerGradients = object.value("fill_gradient").toObject();
        if (object.contains("fill_gradient") && (!lower || !validPhases(lowerGradients)))
            throw BackendError("Skia 下方注音渐变缺少对应画笔能力");
        const auto staticLowerGradients = object.value("static_fill_gradient").toObject();
        const auto shareLowerGradient = object.value("fill_gradient_with_main");
        if (object.contains("static_fill_gradient") && (!lower || !allowLowerDecoration
            || !validPhases(staticLowerGradients) || !shareLowerGradient.isBool()))
            throw BackendError("Skia 下方注音渐变缺少对应布局或画笔能力");
        if (!object.contains("static_fill_gradient") && object.contains("fill_gradient_with_main"))
            throw BackendError("Skia 下方注音渐变范围缺少画笔");
        auto lowerBefore = lowerGradient(staticLowerGradients, "before", lowerGradients);
        auto lowerAfter = lowerGradient(staticLowerGradients, "after", lowerGradients);
        if (object.contains("static_fill_image")) {
            const auto payload = object.value("static_fill_image").toObject();
            if (!lower || !allowLowerDecoration || !validPhases(payload)
                || (payload.contains("before") && lowerBefore.mode != "solid")
                || (payload.contains("after") && lowerAfter.mode != "solid"))
                throw BackendError("Skia 下方注音图片画笔缺少对应布局能力或与其他画笔重叠");
            if (payload.contains("before")) lowerBefore = lowerImage(payload, "before");
            if (payload.contains("after")) lowerAfter = lowerImage(payload, "after");
        }
        const auto& beforeFill = lower ? lowerBefore : paint.rubyBeforeFillPaint;
        const auto& afterFill = lower ? lowerAfter : paint.rubyAfterFillPaint;
        const bool gradientFill = rubyFillGradientMode(beforeFill) || rubyFillGradientMode(afterFill);
        const bool imageFill = beforeFill.mode == "image" || afterFill.mode == "image";
        // Reading paints compose with any line animation, as their body paints do.
        const bool allowFillPaint = lowerWipe;
        const bool allowRoleFill = roleBounds && features.contains("p06_ruby_role_paints")
            && (line.karaokeAnimation == "none" || features.contains("p07_role_singing_scale"));
        const bool independentFill = staticLine && roleBounds && !rtl && scene.charStyles.empty()
            && features.contains("p06_independent_ruby_fill");
        const bool allowGradientFill = allowFillPaint;
        const bool allowImageFill = allowFillPaint;
        const auto validFill = [](const auto& fill) {
            return fill.mode == "solid" || rubyFillGradientMode(fill) || fill.mode == "image";
        };
        if (!lower && (!validFill(paint.rubyBeforeFillPaint) || !validFill(paint.rubyAfterFillPaint)))
            throw BackendError("Skia 注音填充画笔模式无效");
        const auto fill = object.value("paint").toObject();
        const auto before = lower ? rgbaPaint(fill.value("before")) : argb(paint.rubyBeforeFillPaint.color);
        const auto after = lower ? rgbaPaint(fill.value("after")) : argb(paint.rubyAfterFillPaint.color);
        if (lower) {
            const auto rgba = [](std::uint32_t color) {
                return RgbaColor{std::uint8_t(color >> 16), std::uint8_t(color >> 8),
                                 std::uint8_t(color), std::uint8_t(color >> 24)};
            };
            lowerBefore.color = rgba(before);
            lowerAfter.color = rgba(after);
        }
        const auto units = object.value("units");
        if (!units.isArray() || units.toArray().isEmpty() || units.toArray().size() > 4096
            || (totalCells += units.toArray().size()) > 65536)
            throw BackendError("Skia 注音最终单元数量无效或超限");
        const auto outlines = object.value("outlines").toObject();
        if (object.contains("outlines") && (outlines.isEmpty()
            || outlines.size() != int(outlines.contains("stroke")) + int(outlines.contains("stroke2"))))
                throw BackendError("Skia 注音描边格式无效");
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
                                width, glyphs.dpr, true, true, true)
                || !matchesOutline(secondary, paint.rubyBeforeStroke2Paint, paint.rubyAfterStroke2Paint,
                                   width2, glyphs.dpr, true, true, true))
                throw BackendError("Skia 上注音描边与样式不一致");
        }
        using ::subtitle::skia::paintNeedsBodyProtection;
        // Lower readings outline with their own stroke fills, as in the GPU.
        const auto lowerOutlineGradients = object.value("lower_outline_gradient").toObject();
        if (object.contains("lower_outline_gradient")
            && (!lower || lowerOutlineGradients.isEmpty()
                || lowerOutlineGradients.size() != int(lowerOutlineGradients.contains("stroke"))
                    + int(lowerOutlineGradients.contains("stroke2"))))
            throw BackendError("Skia 下方注音渐变描边格式无效");
        const auto lowerOutlinePaint = [&](const char* layer, const char* phase) {
            const auto authored = lowerOutlineGradients.value(layer).toObject();
            if (lowerOutlineGradients.contains(layer) && !validPhases(authored))
                throw BackendError("Skia 下方注音渐变描边缺少能力声明或格式无效");
            return lowerGradient(authored, phase);
        };
        const std::array<PaintStyle, 2> lowerStroke{lowerOutlinePaint("stroke", "before"), lowerOutlinePaint("stroke", "after")};
        const std::array<PaintStyle, 2> lowerStroke2{lowerOutlinePaint("stroke2", "before"),
                                                     lowerOutlinePaint("stroke2", "after")};
        const auto lowerOutlineWithMain = object.value("outline_gradient_with_main");
        if (object.contains("lower_outline_gradient") != lowerOutlineWithMain.isBool())
            throw BackendError("Skia 下方注音渐变描边缺少能力声明或格式无效");
        const bool gradientOutlines = (primary.width || secondary.width)
            && (lower ? object.contains("lower_outline_gradient") : allowOutlineGradient);
        // The GPU protects the body under any image paint, decoded or not.
        const bool imageOutline = !lower && (paint.rubyBeforeStrokePaint.mode == "image"
                                             || paint.rubyAfterStrokePaint.mode == "image");
        const bool protectPrimary = primary.width && (primary.hasAlpha() || imageOutline || (gradientOutlines
            && (paintNeedsBodyProtection(paint.rubyBeforeStrokePaint) || paintNeedsBodyProtection(paint.rubyAfterStrokePaint))));
        const bool protectFill = imageFill || (before >> 24) < 255 || (after >> 24) < 255 || (gradientFill
            && (paintNeedsBodyProtection(beforeFill) || paintNeedsBodyProtection(afterFill)));
        const float innerWidth = float(primary.width * glyphs.dpr);
        const float secondaryWidth = float(secondary.width * glyphs.dpr);
        const float outerWidth = float((primary.width + secondary.width) * glyphs.dpr);
        const auto lowerGlow = object.value("lower_glow").toObject();
        if (object.contains("lower_glow")) {
            if (!lower || !allowLowerDecoration || !validPhases(lowerGlow))
                throw BackendError("Skia 下方注音实色光晕缺少对应布局能力");
            glow = {lowerGlowState(lowerGlow, "before", glyphs.dpr),
                    lowerGlowState(lowerGlow, "after", glyphs.dpr), outerWidth};
            lowerGlowSpec = glow;
        }
        const auto lowerShadow = object.value("lower_shadow").toObject();
        if (object.contains("lower_shadow")) {
            if (!lower || !allowLowerDecoration || !validPhases(lowerShadow)
                || (lowerGlow.contains("before") && lowerShadow.contains("before"))
                || (lowerGlow.contains("after") && lowerShadow.contains("after")))
                throw BackendError("Skia 下方注音实色阴影缺少对应布局能力或同一阶段重复装饰");
            shadow = {lowerShadowState(lowerShadow, "before", glyphs.dpr),
                      lowerShadowState(lowerShadow, "after", glyphs.dpr), innerWidth, secondaryWidth};
        }
        const auto lowerShadowGradients = object.value("lower_shadow_gradient").toObject();
        if (object.contains("lower_shadow_image")) {
            const auto images = object.value("lower_shadow_image").toObject();
            if (!lower || !validPhases(images))
                throw BackendError("Skia 下方注音图片阴影缺少对应布局或画笔能力");
            const auto prepareImage = [&](auto& state, const char* phase) {
                if (!images.contains(phase)) return;
                if (!lowerShadow.contains(phase) || lowerShadowGradients.contains(phase))
                    throw BackendError("Skia 下方注音图片阴影阶段无效");
                state.image = imageCache.fill(lowerImage(images, phase), scene.layoutReferenceScale);
            };
            prepareImage(shadow.before, "before");
            prepareImage(shadow.after, "after");
        }
        const auto shadowWithMain = object.value("shadow_gradient_with_main");
        if (object.contains("lower_shadow_gradient")
            && (!lower || !roleBounds || !validPhases(lowerShadowGradients)
                || !shadowWithMain.isBool()
                || (lowerShadowGradients.contains("before") && !lowerShadow.contains("before"))
                || (lowerShadowGradients.contains("after") && !lowerShadow.contains("after"))))
            throw BackendError("Skia 下方注音渐变阴影缺少对应布局或画笔能力");
        if (!object.contains("lower_shadow_gradient") && object.contains("shadow_gradient_with_main"))
            throw BackendError("Skia 下方注音渐变阴影范围缺少画笔");
        const auto beforeShadow = lowerGradient(lowerShadowGradients, "before");
        const auto afterShadow = lowerGradient(lowerShadowGradients, "after");
        const auto* beforeShadowPaint = lower ? &beforeShadow : &paint.rubyBeforeDecorPaint;
        const auto* afterShadowPaint = lower ? &afterShadow : &paint.rubyAfterDecorPaint;
        const bool beforeShadowGradient = shadow.before.enabled && hasVisibleGradientStops(*beforeShadowPaint);
        const bool afterShadowGradient = shadow.after.enabled && hasVisibleGradientStops(*afterShadowPaint);
        if (lowerShadowGradients.contains("before")) shadow.before.color = 0u;
        if (lowerShadowGradients.contains("after")) shadow.after.color = 0u;
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
                if (!glyphText.isString() || glyphText.toString() != drawingText.normalized(QString::NormalizationForm_C))
                    throw BackendError("Skia 注音字形文本不是来源单元的 NFC");
                drawingText = glyphText.toString();
            }
            validateStandaloneMark(drawingText, cell, ir);
            const auto interval = times.toArray();
            const int start = integer(interval[0], -86400000, 86400000);
            const int end = integer(interval[1], start, 86400000);
            const auto shiftedStart = std::int64_t(start) + offset, shiftedEnd = std::int64_t(end) + offset;
            if (shiftedStart < -172800000 || shiftedEnd > 172800000)
                throw BackendError("Skia 注音最终时间偏移超限");
            auto glyph = glyphs.cell(cell, drawingText, int(shiftedStart), int(shiftedEnd),
                before, after, wipePad);
            if (affineComposition) {
                const float x = float(pivotCoordinate(cell.value("affine_pivot_x")) * glyphs.dpr);
                glyph.pivot.layoutLeft = glyph.pivot.layoutRight = x;
                glyph.pivot.centerY = affineY;
                glyph.affinePivot = SkPoint{x, affineY};
            }
            // Utopia entry/exit poses readings around the same layout anchor as Utopia singing.
            const bool utopiaPose = line.karaokeAnimation == "utopia"
                || ((line.entryAnimation == "utopia" || line.exitAnimation == "utopia")
                    && cell.contains("utopia_layout_left"));
            if (utopiaPose && !vertical) {
                const double left = pivotCoordinate(cell.value("utopia_layout_left"));
                glyph.pivot.layoutLeft = float(left * glyphs.dpr);
                glyph.pivot.baseline = float(cell.value("baseline_y").toDouble() * glyphs.dpr);
                glyph.pivot.wipePad = lower ? float(int(innerWidth) / 2) : float(int(paint.rubyStrokeWidth) / 2);
                if (groupZoom) {
                    const double right = pivotCoordinate(cell.value("utopia_layout_right"));
                    if (right < left) throw BackendError("Skia 注音整字放大单元跨度无效");
                    glyph.pivot.layoutRight = float(right * glyphs.dpr);
                    glyph.pivot.centerY = float(zoomCenter * glyphs.dpr);
                }
            } else if (cell.contains("utopia_layout_left")) {
                throw BackendError("Skia 注音单元轴心缺少 Utopia 能力声明");
            }
            if (dripComposition)
                glyph.pivot.layoutRight = float(pivotCoordinate(cell.value("drip_layout_right")) * glyphs.dpr);
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
                const bool kanaMark = scalars.size() == 2 && (scalars[1] == 0x3099 || scalars[1] == 0x309a)
                    && ((scalars[0] >= 0x3041 && scalars[0] <= 0x3096)
                        || (scalars[0] >= 0x30a1 && scalars[0] <= 0x30fa));
                const bool extended = features.contains("p05_vertical_ruby_extended_units")
                    && (kanaMark || (scalars.size() == 1 && (scalar == 0x30fb || isLatinText(drawingText.toStdWString()))));
                if ((verticalSchema != 1 && verticalSchema != 2)
                    || (verticalSchema == 1 && units.toArray().size() != 1)
                    || lower || !(verticalUprightScalar(scalar) || shifted || rotated || extended)
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
        if (multipleVertical && !overlapping && inkTop < previousInkBottom)
            throw BackendError("Skia 竖排同列注音的墨迹范围重叠");
        previousInkBottom = inkBottom;
        const bool hasGlow = glow.before.radius > 0 || glow.after.radius > 0;
        // Lower glows paint with their own gradient, spanning the main line like the lower fill.
        const auto lowerGlowGradients = object.value("lower_glow_gradient").toObject();
        const auto lowerGlowWithMain = object.value("glow_gradient_with_main");
        if (object.contains("lower_glow_gradient")
            && (!lower || !object.contains("lower_glow") || !validPhases(lowerGlowGradients)
                || !lowerGlowWithMain.isBool()))
            throw BackendError("Skia 下方注音渐变光晕缺少对应布局或画笔能力");
        const auto lowerGlowBefore = lowerGradient(lowerGlowGradients, "before");
        const auto lowerGlowAfter = lowerGradient(lowerGlowGradients, "after");
        const bool lowerGlowGradient = lower && object.contains("lower_glow_gradient") && hasGlow;
        SkRect ink = SkRect::MakeEmpty();
        if (gradientFill || gradientOutlines || beforeShadowGradient || afterShadowGradient || lowerGlowGradient
            || (hasGlow && glowPaints != GlowPaintSupport::Solid))
            for (const auto& cell : cells)
                if (!cell.path.isEmpty()) ink.join(cell.path.computeTightBounds());
        const auto outlinePad = ::subtitle::skia::outlineVisualPad(innerWidth, secondaryWidth, float(glyphs.dpr));
        SkRect outlineInk = ink;
        if (object.contains("vertical_paint_span")) {
            const auto span = object.value("vertical_paint_span").toArray();
            const double top = span.size() == 2 ? pivotCoordinate(span[0]) : 0, bottom = span.size() == 2 ? pivotCoordinate(span[1]) : 0;
            if (!vertical || span.size() != 2 || !(bottom > top))
                throw BackendError("Skia 竖排注音画笔跨度无效");
            outlineInk.fTop = float(top * glyphs.dpr);
            outlineInk.fBottom = float(bottom * glyphs.dpr);
        }
        if (beforeShadowGradient || afterShadowGradient) {
            auto shaders = rubyGradients({beforeShadowGradient ? beforeShadowPaint : nullptr,
                                          afterShadowGradient ? afterShadowPaint : nullptr},
                lower ? shadowWithMain.toBool() : paint.rubyHorizontalGradientWithMain, source, ink, outlinePad,
                glyphs.dpr, lower ? &roleBounds->line : &roleBounds->forRole(role));
            shadow.before.shader = std::move(shaders[0]);
            shadow.after.shader = std::move(shaders[1]);
        }
        const bool shareHorizontalFill = lower ? shareLowerGradient.toBool() : paint.rubyHorizontalGradientWithMain;
        const auto* sharedFillBounds = lower ? (roleBounds ? &roleBounds->line : nullptr)
            : (allowRoleFill || independentFill) ? &roleBounds->forRole(role) : nullptr;
        auto gradients = gradientFill ? rubyGradients({&beforeFill, &afterFill},
            shareHorizontalFill, source, ink, outlinePad, glyphs.dpr, sharedFillBounds)
            : std::array<sk_sp<SkShader>, 2>{};
        std::array<sk_sp<SkShader>, 2> outlineGradients, outerOutlineGradients;
        // Lower outlines span the main line like the lower fill.
        const auto* outlineBounds = lower ? (roleBounds ? &roleBounds->line : nullptr)
            : allowHorizontalOutline && roleBounds ? &roleBounds->forRole(role) : nullptr;
        const bool sharedHorizontalOutline = lower ? lowerOutlineWithMain.toBool()
            : outlineBounds && paint.rubyHorizontalGradientWithMain;
        if (gradientOutlines && primary.width)
            outlineGradients = rubyGradients(lower ? std::array<const PaintStyle*, 2>{&lowerStroke[0], &lowerStroke[1]}
                                                   : std::array<const PaintStyle*, 2>{&paint.rubyBeforeStrokePaint,
                                                                                      &paint.rubyAfterStrokePaint},
                                           sharedHorizontalOutline, source, outlineInk, outlinePad, glyphs.dpr, outlineBounds);
        if (gradientOutlines && secondary.width)
            outerOutlineGradients = rubyGradients(lower ? std::array<const PaintStyle*, 2>{&lowerStroke2[0], &lowerStroke2[1]}
                                                        : std::array<const PaintStyle*, 2>{&paint.rubyBeforeStroke2Paint,
                                                                                           &paint.rubyAfterStroke2Paint},
                                                sharedHorizontalOutline, source, outlineInk, outlinePad, glyphs.dpr, outlineBounds);
        ::subtitle::skia::GlowGradients glowGradients;
        if (lowerGlowGradient) {
            glowGradients = rubyGradients({glow.before.radius > 0 ? &lowerGlowBefore : nullptr,
                                           glow.after.radius > 0 ? &lowerGlowAfter : nullptr},
                                          lowerGlowWithMain.toBool(), source, ink, outlinePad, glyphs.dpr,
                                          roleBounds ? &roleBounds->line : nullptr);
        } else if (hasGlow && glowPaints != GlowPaintSupport::Solid && !lower) {
            glowGradients = rubyGradients({glow.before.radius > 0 ? &paint.rubyBeforeDecorPaint : nullptr,
                                           glow.after.radius > 0 ? &paint.rubyAfterDecorPaint : nullptr},
                                          paint.rubyHorizontalGradientWithMain, source, ink, outlinePad, glyphs.dpr,
                                          roleBounds ? &roleBounds->forRole(role) : nullptr);
        }
        std::array<::subtitle::skia::ImageFill, 2> images;
        if (imageFill) {
            images[0] = imageCache.fill(beforeFill, scene.layoutReferenceScale);
            images[1] = imageCache.fill(afterFill, scene.layoutReferenceScale);
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
        groups.push_back({std::move(cells), std::move(shadow), hasGlow ? std::optional(glow) : std::nullopt,
                          fadeOwner, spinPivot, affineComposition, lower, std::move(gradients), std::move(images),
                          roleGlow && !lower ? role : -1, std::move(glowGradients), rtl, utopiaExitOwner,
                          std::move(outlineGradients), std::move(outerOutlineGradients), std::move(scanlineGradients),
                          dripComposition});
        groups.back().utopiaExitSpan = utopiaExitSpan;
        // Image-painted outlines use the GPU's device-anchored bitmap brush.
        if (!lower)
            for (const bool after : {false, true}) {
                const auto& stroke = after ? paint.rubyAfterStrokePaint : paint.rubyBeforeStrokePaint;
                const auto& stroke2 = after ? paint.rubyAfterStroke2Paint : paint.rubyBeforeStroke2Paint;
                if (primary.width && stroke.mode == "image")
                    groups.back().outlineImages[after] = imageCache.fill(stroke, scene.layoutReferenceScale);
                if (secondary.width && stroke2.mode == "image")
                    groups.back().outerOutlineImages[after] = imageCache.fill(stroke2, scene.layoutReferenceScale);
            }
        // The GPU strokes an image-painted glow with its bitmap brush.
        if (hasGlow && !lower)
            for (const bool after : {false, true}) {
                const auto& decor = after ? paint.rubyAfterDecorPaint : paint.rubyBeforeDecorPaint;
                if ((after ? glow.after : glow.before).radius > 0 && decor.mode == "image")
                    groups.back().glowImages[after] = imageCache.fill(decor, scene.layoutReferenceScale);
            }
    }
    return groups;
}
}
