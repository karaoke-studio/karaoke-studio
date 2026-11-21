#include "resolved_input.h"
#include "resolved_wipe.h"
#include "resolved_glyph.h"
#include "resolved_bitmap_guide.h"
#include "resolved_line_band.h"
#include "resolved_ruby.h"
#include "resolved_role_paints.h"
#include "resolved_signal.h"
#include "image_fill_cache.h"
#include "scene_input.h"
#include "vertical_text.h"
#include "../render_backend.h"
#include "../text_semantics.h"
#include "../../scene/skia_ir_projection.h"
#include "../../scene/skia_ir_contract.h"
#include "../../scene/skia_text_projection.h"
#include "../../skia/core/stroke_flash.h"
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
    const bool rotated = verticalRotatedScalar(code);
    const bool shifted = verticalShiftedScalar(code);
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
::subtitle::skia::ResolvedSubtitleInput resolvedSkiaSource(
    const QJsonObject& ir, SkiaImageFillCache& imageCache, const ::subtitle::skia::LineBandTexture* sharedBand,
    bool staticTitle) {
    auto projected = scene::skiaSceneFromIr(ir, staticTitle);
    const auto features = ir.value("required_features").toArray();
    const int outlineLayers = features.contains("p06_secondary_solid_outline") ? 2
        : features.contains("p06_primary_solid_outline") ? 1 : 0;
    const bool roleBodyPaints = features.contains("p06_role_body_paints");
    const bool roleStaticGlow = features.contains("p06_role_static_glow");
    const bool gradientGlowWithoutTexturedRoles = features.contains("p06_gradient_glow");
    const auto glowPaints = (gradientGlowWithoutTexturedRoles || features.contains("p06_role_gradient_glow"))
        ? GlowPaintSupport::LineGradients
        : features.contains("p06_vertical_gradient_glow") ? GlowPaintSupport::VerticalGradient : GlowPaintSupport::Solid;
    const bool gradientGlow = glowPaints != GlowPaintSupport::Solid;
    const bool latinPrimaryOutline = features.contains("p06_latin_primary_outline");
    const bool bodyGradients = features.contains("p06_body_gradient") || roleBodyPaints;
    const bool outlineGradients = features.contains("p06_outline_gradient");
    const bool imageFills = features.contains("p06_image_fill") || roleBodyPaints;
    validateSkiaScene(projected, true, {.outlineLayers = outlineLayers, .bodyGradients = bodyGradients,
        .outlineGradients = outlineGradients, .imageFills = imageFills,
        .staticShadow = features.contains("p06_static_shadow"), .staticGlow = features.contains("p06_static_glow"),
        .customShapeSignal = features.contains("p08_custom_shape_signal"),
        .imageShapeSignal = features.contains("p08_custom_image_signal"),
        .decoratedShapeSignal = features.contains("p08_decorated_shape_signal"),
        .vectorShapeSignal = features.contains("p08_vector_shape_signal"),
        .volumeSignal = features.contains("p08_volume_signal"),
        .staticLineBand = features.contains("p08_static_line_band"),
        .roleOutlines = (staticTitle && features.contains("p08_title_role_outlines"))
            || features.contains("p06_role_solid_outlines"), .roleBodyPaints = roleBodyPaints,
        .roleStaticGlow = roleStaticGlow, .latinPrimaryOutline = latinPrimaryOutline, .glowPaints = glowPaints});
    const bool staticGlow = features.contains("p06_static_glow");
    if (latinPrimaryOutline && (roleBodyPaints || roleStaticGlow || staticGlow))
        splitLatinOutlineStyles(projected);
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
    out.viewport = skiaViewport(projected);
    if (projected.lineBand.enabled)
        out.lineBand = sharedBand ? *sharedBand : resolveLineBandTexture(projected.lineBand, imageCache,
            features.contains("p08_animated_staff_line_band") || features.contains("p08_animated_line_band")
                || features.contains("p08_line_band_image_sequence"));
    std::size_t totalCells = 0, totalPoints = 0, totalGroups = 0;
    const bool columnDecoration = !features.contains("p05_vertical_columns")
        || (features.contains("p06_vertical_columns_decoration")
            && (features.contains("p06_vertical_mixed_column_decoration")
                || std::ranges::all_of(projected.lines, [&](const auto& other) {
                    return other.wipeReverse == projected.lines.front().wipeReverse;
                })));
    for (int index = 0; index < lines.size(); ++index) {
        const auto source = lines[index].toObject();
        const auto& line = projected.lines[index];
        const auto& style = projected.lineStyles[index];
        if (gradientGlowWithoutTexturedRoles && (style.vertical || style.rightToLeft || line.wipeReverse
            || line.karaokeAnimation != "none" || line.scanlineEnabled || line.strokeFlashEnabled || !line.bursts.empty()
            || (line.entryAnimation != "none" && line.entryAnimation != "fade")
            || (line.exitAnimation != "none" && line.exitAnimation != "fade")))
            throw BackendError("Skia 渐变光晕需要正向横排普通走字，可选整行淡化");
        if (!source.value("cells").isArray()) throw BackendError("Skia 最终布局缺少单元");
        const auto cells = source.value("cells").toArray();
        const auto original = originalLines[index].toObject();
        const auto intervals = original.value("resolved_intervals").toArray();
        const auto sourceChars = original.value("chars").toArray();
        if (cells.size() != int(line.chars.size()) || intervals.size() != cells.size() || (totalCells += cells.size()) > 65536)
            throw BackendError("Skia 最终布局与解析计时单元不一致或超限");
        ::subtitle::skia::ResolvedLine prepared{{}, line.displayWindows[0].startMs, line.displayWindows[0].endMs};
        if (projected.lineBand.enabled) {
            const auto rect = source.value("line_band_rect").toArray();
            if (rect.size() != 4 || std::ranges::any_of(rect, [](const auto& value) {
                    return !value.isDouble() || !std::isfinite(value.toDouble()) || std::abs(value.toDouble()) > 1e6;
                }))
                throw BackendError("Skia 行背景带缺少有效的目标矩形");
            const auto px = [&](int i) { return float(rect[i].toDouble() * glyphs.dpr); };
            prepared.lineBand = SkRect::MakeLTRB(px(0), px(1), px(2), px(3));
            if (features.contains("p08_line_band_transition")) {
                const auto duration = [](const std::string& kind, int value) {
                    if (kind != "char_fade" && kind != "flame" && kind != "lightning") return 0;
                    return value > 0 ? value : 600;
                };
                prepared.lineBandTransition = {duration(line.entryAnimation, line.entryDurationMs),
                                               duration(line.exitAnimation, line.exitDurationMs)};
            }
        } else if (source.contains("line_band_rect")) {
            throw BackendError("Skia 行背景带缺少能力声明");
        }
        prepared.shapeSignal = resolveShapeSignal(source, line, style, glyphs.dpr, imageCache,
            projected.litDecorStyle ? &*projected.litDecorStyle : nullptr);
        prepared.volumeSignal = resolveVolumeSignal(source, line, style, glyphs.dpr,
            projected.volumeDecorStyle ? &*projected.volumeDecorStyle : nullptr);
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
        const auto entryParticleKind = scene::skiaParticleKindForTransition(QString::fromStdString(line.entryAnimation), true);
        const auto exitParticleKind = scene::skiaParticleKindForTransition(QString::fromStdString(line.exitAnimation), false);
        const bool particleEntry = !entryParticleKind.isEmpty(), particleExit = !exitParticleKind.isEmpty();
        const bool geoEntry = particleEntry || line.entryAnimation == "tracking_in" || line.entryAnimation == "wave_in"
            || line.entryAnimation == "stretch_in" || line.entryAnimation == "glow_in";
        const bool geoExit = particleExit || line.exitAnimation == "scatter_out" || line.exitAnimation == "converge_out"
            || line.exitAnimation == "stretch_out" || line.exitAnimation == "glow_out";
        const bool noiseEntry = line.entryAnimation == "noise", noiseExit = line.exitAnimation == "noise";
        const bool noiseComposition = (noiseEntry || noiseExit) && features.contains("p07_noise_composition");
        const bool singingScale = line.karaokeAnimation == "utopia";
        const bool utopiaEntry = line.entryAnimation == "utopia", utopiaExit = line.exitAnimation == "utopia";
        const bool utopia = singingScale || utopiaEntry || utopiaExit;
        const bool annotated = !source.value("ruby_groups").toArray().isEmpty();
        const bool rtlRuby = style.rightToLeft && features.contains("p05_horizontal_rtl_ruby");
        const bool rtlRubySinging = rtlRuby && singingScale && features.contains("p07_rtl_ruby_singing_scale");
        if (rtlRuby && (style.strokeWidth <= 0 || line.wipeReverse || (line.karaokeAnimation != "none" && !rtlRubySinging)
            || line.entryAnimation != "none" || line.exitAnimation != "none" || line.zoomPulseEnabled
            || line.scanlineEnabled || line.strokeFlashEnabled || !line.bursts.empty()
            || !projected.charStyles.empty()))
            throw BackendError("Skia 右至左上注音当前需要有描边的普通走字或 Utopia 唱字，支持实色光晕");
        const bool vectorGuides = std::ranges::any_of(line.chars, [](const auto& cell) { return bool(cell.vectorGlyph); });
        const bool transformedRuby = annotated
            && !line.zoomPulseEnabled && !line.wipeReverse && !style.vertical && !style.rightToLeft
            && !line.scanlineEnabled && !line.strokeFlashEnabled
            && projected.charStyles.empty()
            && std::ranges::none_of(line.chars, [](const auto& cell) { return bool(cell.vectorGlyph); });
        const bool spinComposition = transformedRuby && features.contains("p07_ruby_character_exit_spin")
            && line.bursts.empty() && line.entryAnimation == "none" && line.exitAnimation == "spin_flip" && line.exitDurationMs > 0;
        const bool translationComposition = transformedRuby && features.contains("p07_ruby_character_translation")
            && (line.entryAnimation == "tracking_in" || line.entryAnimation == "wave_in")
            && line.bursts.empty() && line.exitAnimation == "none" && line.entryDurationMs > 0;
        const bool affineExit = line.entryAnimation == "none"
            && ((features.contains("p07_ruby_character_affine_exit")
                    && (line.exitAnimation == "scatter_out" || line.exitAnimation == "converge_out"))
                || (features.contains("p07_ruby_character_stretch_exit") && line.exitAnimation == "stretch_out"));
        const bool waveScatter = features.contains("p07_ruby_character_wave_scatter")
            && line.entryAnimation == "wave_in" && line.exitAnimation == "scatter_out" && line.entryDurationMs > 0;
        const bool stretchEntry = features.contains("p07_ruby_character_stretch_entry")
            && line.entryAnimation == "stretch_in" && line.exitAnimation == "none" && line.entryDurationMs > 0;
        const bool glowTransition = features.contains("p07_ruby_character_glow_transition")
            && ((line.entryAnimation == "glow_in" && line.exitAnimation == "none" && line.entryDurationMs > 0)
                || (line.entryAnimation == "none" && line.exitAnimation == "glow_out" && line.exitDurationMs > 0));
        const bool affineComposition = transformedRuby && line.bursts.empty()
            && ((line.exitDurationMs > 0 && (affineExit || waveScatter)) || stretchEntry || glowTransition);
        const bool pairedEnergy = features.contains("p07_ruby_energy_pair")
            && line.entryDurationMs > 0 && line.exitDurationMs > 0;
        const bool particleComposition = transformedRuby && !line.bursts.empty()
            && ((line.entryDurationMs > 0 && line.exitAnimation == "none")
                || (line.exitDurationMs > 0 && line.entryAnimation == "none") || pairedEnergy)
            && scene::allowsSkiaRubyParticleTransition(entryParticleKind, exitParticleKind, features);
        if (particleComposition && features.contains("p07_ruby_particle_entry")
            && (style.decorationKindForState(false) != "none" || style.decorationKindForState(true) != "none"
                || style.rubyDecorationKindForState(false) != "none" || style.rubyDecorationKindForState(true) != "none"))
            throw BackendError("Skia 带上注音的粒子入场暂不支持阴影或光晕");
        const bool geometryComposition = translationComposition || affineComposition || particleComposition;
        const bool fadeComposition = features.contains("p07_character_entry_fade_composition")
            && line.entryAnimation == "char_fade" && line.exitAnimation == "none"
            && (line.karaokeAnimation == "none" || (singingScale && !line.zoomPulseEnabled))
            && !line.wipeReverse && !style.vertical && !style.rightToLeft
            && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty()
            && projected.charStyles.empty()
            && std::ranges::none_of(line.chars, [](const auto& cell) { return bool(cell.vectorGlyph); });
        const bool rubyParticles = layout.value("sing_twinkle_schema").toDouble(-1) >= 12;
        const bool lineAnimation = singingScale && !line.zoomPulseEnabled
            && features.contains("p07_utopia_line_animation")
            && (line.entryAnimation == "none" || line.entryAnimation == "fade" || line.entryAnimation == "slide_in"
                || line.entryAnimation == "rise")
            && (line.exitAnimation == "none" || line.exitAnimation == "fade" || line.exitAnimation == "slide_out"
                || line.exitAnimation == "rise");
        const bool roleSinging = features.contains("p07_role_singing_scale") && singingScale && !line.zoomPulseEnabled
            && line.entryAnimation == "none" && line.exitAnimation == "none"
            && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty();
        if (utopia && (style.vertical || (style.rightToLeft && !rtlRubySinging) || prepared.noWipe
            || (vectorGuides && !features.contains("p05_vector_guide_composition"))
            || line.wipeReverse || (line.strokeFlashEnabled && (utopiaEntry || utopiaExit
                || !features.contains("p07_composed_stroke_flash")))
            || (!line.bursts.empty() && (!rubyParticles || utopiaEntry || utopiaExit || line.zoomPulseEnabled))
            || (line.scanlineEnabled && (utopiaEntry || utopiaExit
                || (line.zoomPulseEnabled && !features.contains("p07_composed_scanline"))))
            || (!lineAnimation && !fadeComposition && !noiseComposition && !spinComposition && !geometryComposition
                && ((!utopiaEntry && line.entryAnimation != "none")
                || (!utopiaExit && line.exitAnimation != "none")))
            || (annotated && (!features.contains("p07_ruby_singing_scale")
                || (line.zoomPulseEnabled && !features.contains("p07_ruby_zoom_pulse"))
                || utopiaEntry || utopiaExit))
            || (!projected.charStyles.empty() && !roleSinging)))
            throw BackendError("Skia Utopia 动画当前仅支持正向横排实色正文");
        prepared.singingScale = {singingScale, line.zoomPulseEnabled, style.zoomPulseCurveLevel, style.zoomPulsePeakPct};
        if (singingScale && annotated) {
            const auto origin = source.value("utopia_origin_x");
            if (!origin.isDouble() || !std::isfinite(origin.toDouble()) || std::abs(origin.toDouble()) > 1e6)
                throw BackendError("Skia Utopia 注音缺少有效的行原点");
            prepared.rubySingingOrigin = float(origin.toDouble() * glyphs.dpr);
        }
        prepared.utopiaTransition = {utopiaEntry, utopiaExit, style.tailMs, float(projected.height) / 15.f};
        prepared.noise = {noiseEntry ? line.entryDurationMs : 0, noiseExit ? line.exitDurationMs : 0,
                          line.endMs, style.fontSize};
        prepared.animation = {line.entryAnimation == "none" || charEntry || geoEntry || noiseEntry || utopiaEntry ? 0 : line.entryDurationMs,
                              line.exitAnimation == "none" || charExit || geoExit || noiseExit || utopiaExit ? 0 : line.exitDurationMs,
                              line.entryAnimation == "slide_in" ? slide : 0.f,
                              line.entryAnimation == "rise" ? rise : 0.f,
                              line.exitAnimation == "slide_out" ? slide : 0.f,
                              line.exitAnimation == "rise" ? -rise : 0.f};
        const int delay = cells.size() > 1 ? 350 / (int(cells.size()) - 1) : 0;
        prepared.characterFade = {charEntry && line.entryDurationMs > 0, charExit && line.exitDurationMs > 0,
                                  line.endMs, delay, kindOf(line.entryAnimation), kindOf(line.exitAnimation)};
        prepared.characterGeometry = {geoEntry ? line.entryAnimation : "", geoExit ? line.exitAnimation : "",
                                      geoEntry ? line.entryDurationMs : 0, geoExit ? line.exitDurationMs : 0,
                                      line.endMs, style.fontSize};
        const bool geometry = prepared.characterGeometry.active();
        prepared.particleVisibility = {particleEntry ? effects::fallEntryLeadMs(line.entryAnimation) : 0,
                                       particleExit && exitParticleKind != "dissolve"};
        if (geometry && !geometryComposition && (style.strokeWidth > 0 || style.stroke2Width > 0))
            throw BackendError("Skia 逐字几何过渡当前仅支持无描边正文");
        prepared.rubyGeometryComposition = geometryComposition;
        const bool transforms = prepared.characterFade.transforms() || geometry;
        prepared.transformedWipe = transforms && allowTransformedWipe;
        const bool pivots = transforms || utopia || !line.bursts.empty();
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
                                 std::min(style.scanlineGlowRadius, halfWidth), line.wipeReverse,
                                 features.contains("p07_ruby_scanline")};
            if (features.contains("p07_scanline_shape") && style.scanlineShape != "band") {
                int lastCharEnd = line.endMs;
                for (const auto& cell : line.chars) lastCharEnd = std::max(lastCharEnd, cell.endMs);
                prepared.scanline.shape = ::subtitle::skia::ScanlineShape::make(
                    style.scanlineShape, style.scanlineColor, style.scanlineWidth, style.fontSize,
                    line.displayWindows[0].startMs, line.displayWindows[0].endMs, lastCharEnd);
                if (style.scanlineShape == "image")
                    prepared.scanline.image = imageCache.scanline(style, projected.height);
            }
        }
        // vertical_solid_schema=1: the reverse flag means bottom-to-top on a column.
        prepared.vertical = style.vertical;
        prepared.wipeRightToLeft = style.vertical ? line.wipeReverse : style.rightToLeft != line.wipeReverse;
        if (style.vertical) {
            const auto rubyGroups = source.value("ruby_groups").toArray();
            if (!ir.value("required_features").toArray().contains("p05_vertical_solid")
                || !source.value("text_flow").isString() || source.value("text_flow").toString() != "vertical"
                || (!rubyGroups.isEmpty()
                    && (!ir.value("required_features").toArray().contains("p05_vertical_ruby")
                        || (rubyGroups.size() > 1 && !features.contains("p05_vertical_ruby_groups")))))
                throw BackendError("Skia 竖排缺少能力声明或最终方向无效");
        } else if (style.rightToLeft || source.contains("text_flow")) {
            if (!ir.value("required_features").toArray().contains("p05_horizontal_rtl")
                || layout.value("horizontal_rtl_schema").toDouble(-1) != 1
                || !source.value("text_flow").isString()
                || source.value("text_flow").toString() != (style.rightToLeft ? "rtl" : "ltr")
                || (!source.value("ruby_groups").toArray().isEmpty()
                    && !rtlRuby))
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
            const float strokeWidth = latinPrimaryOutline && isLatinText(line.chars[j].text)
                ? paint.latinStrokeWidth : paint.strokeWidth;
            const auto beforeColor = argb(paint.beforeFillPaint.color);
            const auto afterColor = argb(paint.afterFillPaint.color);
            const double wipePad = std::floor(std::round(strokeWidth / glyphs.dpr) / 2);
            const auto& vector = line.chars[j].vectorGlyph;
            const auto glyphId = sourceChars[j].toObject().value("vector_glyph_id");
            if ((glyphId.isString() && !vector) || (vector && line.chars[j].text != L"\ufffc"))
                throw BackendError("Skia 导唱符缺少有效的来源轮廓引用");
            const auto& bitmap = line.chars[j].bitmapGuide;
            if (bitmap && (!features.contains("p08_bitmap_guides") || vector || line.chars[j].text != L"￼"))
                throw BackendError("Skia 图片导唱符缺少版本化能力或来源无效");
            auto glyph = bitmap
                ? resolvedBitmapGuideCell(cell, *bitmap, imageCache, paint, glyphs.dpr, line.chars[clock].startMs,
                                          line.chars[clock].endMs, beforeColor, afterColor, wipePad,
                                          staticTitle && features.contains("p08_animated_title_guides"))
                : vector
                ? glyphs.vectorCell(cell, *vector, glyphId.toString(), paint.fontSize / glyphs.dpr, line.chars[clock].startMs,
                                    line.chars[clock].endMs, beforeColor, afterColor, wipePad)
                : glyphs.cell(cell, QString::fromStdWString(line.chars[j].text), line.chars[clock].startMs,
                              line.chars[clock].endMs, beforeColor, afterColor, wipePad);
            if (style.vertical) {
                applyVerticalGeometry(glyph, cell, line.chars[j].text, verticalVersion, japaneseGlyphs, glyphs.dpr);
            } else if (cell.contains("wipe_top") || cell.contains("wipe_bottom") || cell.contains("rotation_pivot_x")) {
                throw BackendError("Skia 横排单元不能携带竖排单元框");
            }
            if (outlineLayers > 0 && !glyph.path.isEmpty()) {
                // The existing GPU protects the body from any translucent fill
                // (including a translucent gradient stop), and the inner outline
                // from the outer ring when either is translucent.
                using ::subtitle::skia::paintNeedsBodyProtection;
                const bool bodyAlpha = paintNeedsBodyProtection(paint.beforeFillPaint)
                    || paintNeedsBodyProtection(paint.afterFillPaint);
                const bool strokeAlpha = paintNeedsBodyProtection(paint.beforeStrokePaint)
                    || paintNeedsBodyProtection(paint.afterStrokePaint);
                const auto beforeOutline = argb(paint.beforeStrokePaint.color);
                const auto afterOutline = argb(paint.afterStrokePaint.color);
                if (strokeWidth > 0)
                    glyph.outline = ::subtitle::skia::SolidOutline{
                        ::subtitle::skia::prepareOutlinePath(glyph.path, strokeWidth),
                        beforeOutline, afterOutline, bodyAlpha, strokeWidth};
                if (paint.stroke2Width > 0)
                    glyph.outerOutline = ::subtitle::skia::SolidOutline{
                        ::subtitle::skia::prepareOutlinePath(glyph.path, strokeWidth + paint.stroke2Width),
                        argb(paint.beforeStroke2Paint.color), argb(paint.afterStroke2Paint.color),
                        bodyAlpha || strokeAlpha, strokeWidth + paint.stroke2Width};
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
                if (utopia) glyph.pivot.baseline = pivot("baseline_y");
                if ((transforms || utopia) && (glyph.outline || glyph.outerOutline)) {
                    const auto opaque = [](const ::subtitle::skia::SolidOutline& stroke) {
                        return !stroke.protectBody && (stroke.before >> 24) == 255 && (stroke.after >> 24) == 255;
                    };
                    const bool paintedSinging = singingScale && !transforms && !line.zoomPulseEnabled
                        && (features.contains("p06_utopia_main_paint") || (roleSinging && roleBodyPaints));
                    if (!paintedSinging && ((!utopia && !allowAnimatedOutlines) || (glyph.outline && !opaque(*glyph.outline))
                        || (glyph.outerOutline && !opaque(*glyph.outerOutline))))
                        throw BackendError("Skia 逐字变换当前仅支持不透明实色描边");
                    const auto ink = glyph.path.computeTightBounds();
                    glyph.pivot.inkLeft = ink.fLeft;
                    glyph.pivot.inkRight = ink.fRight;
                    glyph.pivot.wipePad = float(int(paint.strokeWidth) / 2);
                }
            } else if (cell.contains("layout_left") || cell.contains("layout_right") || cell.contains("pivot_y")) {
                throw BackendError("Skia 版面单元轴心缺少逐字特效能力声明");
            }
            if (line.strokeFlashEnabled)
                glyph.strokeFlashWidth = ::subtitle::skia::strokeFlashBaseWidth(paint.fontSize, paint.strokeWidth);
            prepared.cells.push_back(std::move(glyph));
        }
        const int timingOffset = cells.isEmpty() ? 0
            : line.chars.front().startMs - int(intervals[0].toArray()[0].toDouble());
        auto rubyTransition = RubyTransition::None;
        if (fadeComposition || particleComposition) rubyTransition = RubyTransition::Fade;
        if (spinComposition) rubyTransition = RubyTransition::Spin;
        if (translationComposition) rubyTransition = RubyTransition::Translation;
        if (affineComposition || (particleComposition && (entryParticleKind == "flame" || exitParticleKind == "flame")))
            rubyTransition = RubyTransition::Affine;
        std::optional<RolePaintBounds> rolePaintBounds;
        if (roleBodyPaints || gradientGlowWithoutTexturedRoles)
            rolePaintBounds = collectRolePaintBounds(prepared, line, style, projected.charStyles,
                                                     projected.layoutReferenceScale);
        if (roleBodyPaints) {
            if (source.contains("body_gradient_bounds"))
                throw BackendError("Skia 角色画笔不能携带整行画笔范围");
            resolveRoleBodyPaints(prepared, line, style, projected.charStyles, *rolePaintBounds, imageCache,
                                  projected.layoutReferenceScale);
        }
        prepared.rubyGroups = resolvedSkiaRubyGroups(source, ir, glyphs, projected, index, timingOffset,
            rubyTransition, imageCache, glowPaints, rolePaintBounds ? &*rolePaintBounds : nullptr, totalCells, totalGroups);
        if (((prepared.characterFade.active() && !fadeComposition && !spinComposition)
                || (geometry && !geometryComposition)) && !prepared.rubyGroups.empty())
            throw BackendError("Skia 逐字淡化暂不支持注音组合");
        // Each burst anchors at its character pivot; the box is
        // the wipe extent by the line's metric ascent + descent (glyph runs, never ink).
        if (!line.bursts.empty()) {
            const auto box = source.value("particle_box_height");
            if (!box.isDouble() || !std::isfinite(box.toDouble()) || box.toDouble() < 0 || box.toDouble() > 1e5
                || (!prepared.rubyGroups.empty() && !rubyParticles) || style.vertical || style.rightToLeft)
                throw BackendError("Skia 字幕粒子缺少有效的行盒高度");
            for (const auto& burst : line.bursts) {
                const auto sprite = particleSprites.find(burst.sprite);
                if (sprite == particleSprites.end() || sprite->second.isEmpty()
                    || burst.charIndex < 0 || burst.charIndex >= int(prepared.cells.size()))
                    throw BackendError("Skia 字幕粒子缺少有效的轮廓或锚点");
                const auto index = std::size_t(burst.charIndex);
                const auto& anchor = prepared.cells[line.wipeReverse ? prepared.cells.size() - 1 - index : index];
                prepared.particles.push_back({burst, sprite->second, argb(burst.color),
                    (anchor.pivot.layoutLeft + anchor.pivot.layoutRight) * .5f, anchor.pivot.centerY,
                    std::max(anchor.right - anchor.left, 1.f), float(box.toDouble() * glyphs.dpr),
                    // Ripple and shockwave both fill the same ring sprite in the existing GPU.
                    burst.kind == "ripple" || burst.kind == "shockwave"
                        ? ::subtitle::skia::concentricRing(sprite->second) : std::nullopt});
            }
        } else if (source.contains("particle_box_height")) {
            throw BackendError("Skia 粒子行盒高度缺少能力声明");
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
        const bool primaryOutline = style.strokeWidth > 0, secondaryOutline = style.stroke2Width > 0;
        // Drawn layers in before/after pairs: body, primary outline, outer ring.
        const PaintStyle* layers[] = {&style.beforeFillPaint, &style.afterFillPaint,
            primaryOutline ? &style.beforeStrokePaint : nullptr, primaryOutline ? &style.afterStrokePaint : nullptr,
            secondaryOutline ? &style.beforeStroke2Paint : nullptr, secondaryOutline ? &style.afterStroke2Paint : nullptr};
        const auto anyLayer = [&](auto&& predicate) {
            return std::ranges::any_of(layers, [&](const PaintStyle* paint) { return paint && predicate(*paint); });
        };
        const bool gradient = anyLayer([](const PaintStyle& p) { return p.mode != "solid" && p.mode != "image"; });
        const bool image = anyLayer([](const PaintStyle& p) { return p.mode == "image"; });
        prepared.shadow = skiaStaticShadow(style);
        prepared.glow = skiaStaticGlow(style);
        const bool hasInlineRoles = std::ranges::any_of(line.chars, [](const auto& cell) { return cell.styleIndex >= 0; });
        const bool lineGradientGlow = gradientGlow
            && ((prepared.glow.before.radius > 0 && style.beforeDecorPaint.mode != "solid")
                || (prepared.glow.after.radius > 0 && style.afterDecorPaint.mode != "solid"));
        if (lineGradientGlow || ((roleStaticGlow || (latinPrimaryOutline && staticGlow)) && hasInlineRoles)) {
            resolveRoleGlows(prepared, line, style, projected.charStyles,
                             gradientGlow && rolePaintBounds ? &*rolePaintBounds : nullptr);
            prepared.glow = {};
        }
        const bool hasGlow = prepared.glow.before.radius || prepared.glow.after.radius;
        const bool hasShadow = prepared.shadow.before.enabled || prepared.shadow.after.enabled;
        const bool hasDecoration = hasGlow || hasShadow;
        const bool singingGlow = singingScale && features.contains("p07_singing_glow")
            && (!line.zoomPulseEnabled || features.contains("p07_zoom_pulse_decoration"));
        const bool particleGlow = rubyParticles && !prepared.shadow.before.enabled && !prepared.shadow.after.enabled;
        const bool lineGlow = (lineAnimation || fadeComposition || noiseComposition || spinComposition || geometryComposition)
            && !prepared.shadow.before.enabled && !prepared.shadow.after.enabled;
        const bool flashGlow = hasGlow && !prepared.shadow.before.enabled && !prepared.shadow.after.enabled
            && features.contains("p07_composed_stroke_flash");
        // The band repaints the glyph stack above its existing static glow.
        const bool roleScanlineGlow = features.contains("p07_role_scanline_layers")
            && line.karaokeAnimation == "none" && line.entryAnimation == "none" && line.exitAnimation == "none"
            && !line.strokeFlashEnabled && line.bursts.empty();
        const bool scanlineGlow = hasGlow && !prepared.shadow.before.enabled && !prepared.shadow.after.enabled
            && layout.value("scanline_band_schema").toDouble(-1) >= 6
            && (style.scanlineMode != "role" || roleScanlineGlow);
        const bool noWipeGlow = hasGlow && !prepared.shadow.before.enabled && !prepared.shadow.after.enabled
            && features.contains("p07_no_wipe_decoration");
        const bool rubyMainPaint = !prepared.rubyGroups.empty() && features.contains("p06_ruby_main_paint")
            && !features.contains("p05_lower_ruby");
        const bool singingMainPaint = rubyMainPaint && singingScale && !line.zoomPulseEnabled
            && features.contains("p06_utopia_main_paint");
        const bool paintedGlow = rubyMainPaint && !prepared.shadow.before.enabled && !prepared.shadow.after.enabled;
        const bool verticalFade = style.vertical && features.contains("p07_vertical_decoration_fade")
            && (line.entryAnimation == "none" || line.entryAnimation == "fade")
            && (line.exitAnimation == "none" || line.exitAnimation == "fade");
        const bool verticalDecoration = style.vertical && columnDecoration
            && (!annotated || features.contains("p06_vertical_ruby_decoration"))
            && (!hasGlow || features.contains("p06_vertical_glow"))
            && (!hasShadow || features.contains("p06_vertical_shadow"))
            && line.karaokeAnimation == "none"
            && (verticalFade || (line.entryAnimation == "none" && line.exitAnimation == "none"))
            && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty();
        const bool reverseVerticalDecoration = verticalDecoration && !annotated
            && (!hasGlow || features.contains("p06_vertical_reverse_glow"))
            && (!hasShadow || features.contains("p06_vertical_reverse_shadow"));
        if (hasDecoration
            && ((style.vertical && !verticalDecoration) || (style.rightToLeft && (!rtlRuby || hasShadow))
                || (line.wipeReverse && !reverseVerticalDecoration) || (prepared.noWipe && !noWipeGlow)
                || (!(lineGlow || (verticalDecoration && verticalFade))
                    && (line.entryAnimation != "none" || line.exitAnimation != "none"))
                || (line.scanlineEnabled && !scanlineGlow)
                || (!line.bursts.empty() && !particleGlow) || (line.strokeFlashEnabled && !flashGlow)
                || hasInlineRoles || ((gradient || image) && !paintedGlow) || (hasGlow && utopia && !singingGlow)
                || (vectorGuides && !features.contains("p05_vector_guide_composition"))))
            throw BackendError("Skia 静态装饰当前仅支持正向横排实色正文");
        if (!roleBodyPaints && (gradient || image)) {
            if ((gradient && (!bodyGradients || ((primaryOutline || secondaryOutline) && !outlineGradients)))
                || (image && !imageFills)
                || style.vertical || style.rightToLeft || line.wipeReverse || (utopia && !singingMainPaint)
                || line.entryAnimation != "none" || line.exitAnimation != "none" || line.karaokeAnimation == "no_wipe"
                || line.scanlineEnabled || !line.bursts.empty() || line.strokeFlashEnabled
                || (!prepared.rubyGroups.empty() && !rubyMainPaint) || !projected.charStyles.empty())
                throw BackendError("Skia 正文渐变当前仅支持正向横排基础正文");
            // Image paints need no line domain: the existing GPU tiles them on the canvas.
            for (int i = 0; i < 6 && image; ++i)
                if (layers[i] && layers[i]->mode == "image")
                    (i < 2 ? prepared.images.body : i < 4 ? prepared.images.outline : prepared.images.outer)[i % 2]
                        = imageCache.fill(*layers[i], projected.layoutReferenceScale);
            if (gradient && std::ranges::any_of(prepared.cells, [](const auto& glyph) { return !glyph.path.isEmpty(); })) {
                const auto rect = source.value("body_gradient_bounds").toArray();
                if (rect.size() != 4 || std::ranges::any_of(rect, [](const auto& v) {
                        return !v.isDouble() || !std::isfinite(v.toDouble()) || std::abs(v.toDouble()) > 1e6;
                    }))
                    throw BackendError("Skia 正文渐变缺少有效的绘制范围");
                const auto ink = SkRect::MakeLTRB(float(rect[0].toDouble() * glyphs.dpr), float(rect[1].toDouble() * glyphs.dpr),
                                                 float(rect[2].toDouble() * glyphs.dpr), float(rect[3].toDouble() * glyphs.dpr));
                if (ink.isEmpty()) throw BackendError("Skia 正文渐变的绘制范围为空");
                // Every layer shares the line's visual pad at the layout reference scale.
                const float pad = ::subtitle::skia::outlineVisualPad(style.strokeWidth, style.stroke2Width,
                                                                    projected.layoutReferenceScale);
                using ::subtitle::skia::lineGradient;
                prepared.beforeGradient = lineGradient(style.beforeFillPaint, ink, pad);
                prepared.afterGradient = lineGradient(style.afterFillPaint, ink, pad);
                if (primaryOutline) {
                    prepared.beforeOutlineGradient = lineGradient(style.beforeStrokePaint, ink, pad);
                    prepared.afterOutlineGradient = lineGradient(style.afterStrokePaint, ink, pad);
                }
                if (secondaryOutline) {
                    prepared.beforeOuterGradient = lineGradient(style.beforeStroke2Paint, ink, pad);
                    prepared.afterOuterGradient = lineGradient(style.afterStroke2Paint, ink, pad);
                }
            }
        }
        if (!roleBodyPaints && !gradient && source.contains("body_gradient_bounds"))
            throw BackendError("Skia 实色正文携带了未声明的渐变范围");
        if (line.wipeReverse) std::reverse(prepared.cells.begin(), prepared.cells.end());
        out.lines.push_back(std::move(prepared));
    }
    return out;
}
}
