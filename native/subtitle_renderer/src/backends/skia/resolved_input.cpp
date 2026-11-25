#include "resolved_input.h"
#include "resolved_wipe.h"
#include "resolved_glyph.h"
#include "resolved_bitmap_guide.h"
#include "resolved_line_band.h"
#include "resolved_particles.h"
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
#include <string_view>

namespace krok::subtitle::native {
namespace {
std::uint32_t argb(const RgbaColor& c) {
    return (std::uint32_t(c.alpha) << 24) | (std::uint32_t(c.red) << 16) | (std::uint32_t(c.green) << 8) | c.blue;
}

void applyVerticalGeometry(::subtitle::skia::ResolvedCell& glyph, const QJsonObject& cell,
                           const std::wstring& text, double version, bool japaneseGlyphs,
                           bool textCells, bool vectorGuide, double dpr) {
    const auto code = text.size() == 1 ? text.front() : L'\0';
    const bool rotated = verticalRotates(text);
    const bool shifted = verticalShiftedScalar(code);
    const bool upright = verticalUprightScalar(code);
    const bool extended = text.empty() || code == L' ' || code == 0x3000
        || (code >= L'0' && code <= L'9') || (code >= L'A' && code <= L'Z') || (code >= L'a' && code <= L'z')
        || (code >= 0x00c0 && code <= 0x00d6) || (code >= 0x00d8 && code <= 0x00f6) || (code >= 0x00f8 && code <= 0x00ff);
    const bool baseGlyph = (verticalRotatedScalar(code) || shifted) ? japaneseGlyphs
        : (upright || (version == 2 && extended));
    if (!vectorGuide && !glyph.bitmap && !baseGlyph && !textCells)
        throw BackendError("Skia 竖排单元包含未验证字符或缺少日文字形能力");
    if (rotated != cell.contains("rotation_pivot_x"))
        throw BackendError("Skia 竖排单元的字形旋转与源字符不符");

    const auto top = cell.value("wipe_top"), bottom = cell.value("wipe_bottom");
    if (!top.isDouble() || !bottom.isDouble() || !std::isfinite(top.toDouble()) || !std::isfinite(bottom.toDouble())
        || bottom.toDouble() <= top.toDouble() || std::abs(top.toDouble()) > 1e6 || std::abs(bottom.toDouble()) > 1e6)
        throw BackendError("Skia 竖排单元框无效");
    glyph.top = float(top.toDouble() * dpr);
    glyph.bottom = float(bottom.toDouble() * dpr);
    if (glyph.bitmap && (std::abs(glyph.top - glyph.bitmap->box.top()) > 1e-3
                         || std::abs(glyph.bottom - glyph.bitmap->box.bottom()) > 1e-3))
        throw BackendError("Skia 竖排图片导唱符的走字范围与图片框不一致");
    if (vectorGuide && !glyph.path.isEmpty()
        && std::abs(glyph.path.computeTightBounds().centerY() - (glyph.top + glyph.bottom) * .5f) > 1e-3)
        throw BackendError("Skia 竖排矢量导唱符未居中于单元框");
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
    const bool roleStaticShadow = features.contains("p06_role_static_shadow");
    const bool roleShadowPaints = features.contains("p06_role_shadow_paints");
    const bool roleVectorGuides = features.contains("p05_role_vector_guides");
    const bool gradientGlowWithoutTexturedRoles = features.contains("p06_gradient_glow");
    const auto glowPaints = (gradientGlowWithoutTexturedRoles || features.contains("p06_role_gradient_glow"))
        ? GlowPaintSupport::LineGradients
        : features.contains("p06_vertical_gradient_glow") ? GlowPaintSupport::VerticalGradient : GlowPaintSupport::Solid;
    const bool gradientGlow = glowPaints != GlowPaintSupport::Solid;
    const bool latinPrimaryOutline = features.contains("p06_latin_primary_outline");
    const bool latinSecondaryOutline = features.contains("p06_latin_secondary_outline");
    const bool bodyGradients = features.contains("p06_body_gradient") || roleBodyPaints;
    const bool roleOutlineGradients = features.contains("p06_role_outline_gradient");
    const bool roleOutlinePaints = features.contains("p06_role_outline_paints");
    const bool outlineGradients = features.contains("p06_outline_gradient") || roleOutlineGradients || roleOutlinePaints;
    const bool inlineAvatars = features.contains("p08_inline_bitmap_guides");
    const bool imageFills = features.contains("p06_image_fill") || roleBodyPaints;
    const bool bodyShadowGradients = features.contains("p06_body_gradient_shadow");
    const bool bodyShadowImages = features.contains("p06_body_image_shadow");
    // Validation only: every paint, decoration and signal kind is implemented.
    validateSkiaScene(projected, true, {.outlineLayers = 2, .bodyGradients = true,
        .outlineGradients = true, .imageFills = true, .staticShadow = true, .staticGlow = true,
        .customShapeSignal = true, .imageShapeSignal = true, .decoratedShapeSignal = true,
        .vectorShapeSignal = true, .volumeSignal = true, .staticLineBand = true,
        .roleOutlines = true, .roleBodyPaints = true, .roleStaticShadow = true, .roleStaticGlow = true,
        .bodyShadowGradients = true, .bodyShadowImages = true, .roleShadowPaints = true,
        .glowPaints = GlowPaintSupport::LineGradients});
    const bool staticGlow = features.contains("p06_static_glow");
    const bool scriptStylesOnly = projected.charStyles.empty()
        && (latinSecondaryOutline || (latinPrimaryOutline && features.contains("p05_vector_guide_composition")
            && features.contains("p07_utopia_line_animation")));
    if ((latinPrimaryOutline || latinSecondaryOutline) && (roleBodyPaints || roleStaticGlow || staticGlow))
        splitLatinOutlineStyles(projected, latinPrimaryOutline, latinSecondaryOutline);
    const auto layout = ir.value("skia_layout").toObject();
    const auto transformVersion = layout.value("character_transform_schema").toDouble();
    const bool allowTransformedWipe = transformVersion >= 2;
    const bool allowAnimatedOutlines = transformVersion >= 3;
    const auto verticalVersion = layout.value("vertical_solid_schema").toDouble(-1);
    const bool japaneseGlyphs = ir.value("required_features").toArray().contains("p05_vertical_japanese_glyphs");
    const bool verticalTextCells = features.contains("p05_vertical_text_cells");
    if (layout.value("version").toInt() != 2 || layout.value("metrics_identity").toString().size() != 64
        || !layout.value("fonts").isObject() || !layout.value("lines").isArray()
        || !layout.value("runs").isArray())
        throw BackendError("Skia 缺少版本化的最终布局与度量快照");
    const auto lines = layout.value("lines").toArray();
    const auto resources = lines.isEmpty() && layout.value("fonts").toObject().isEmpty()
        ? scene::FontResourceTable{} : scene::loadSkiaFontResources(layout.value("fonts").toObject());
    const auto originalTrack = ir.value("track").toObject();
    const auto originalLines = originalTrack.value("lines").toArray();
    const bool anyReadings = !originalTrack.value("rubies").toArray().isEmpty();
    if (lines.size() != int(projected.lines.size()) || lines.size() > 4096)
        throw BackendError("Skia 最终布局行数不一致或超限");
    const auto runs = layout.value("runs").toArray();
    if (runs.size() > 4096) throw BackendError("Skia 字体实例数量超限");
    ResolvedGlyphContext glyphs;
    for (const auto& value : runs) {
        if (!value.isObject()) throw BackendError("Skia 字体实例必须为对象");
        const auto run = value.toObject();
        if (run.value("rtl").toBool()) throw BackendError("Skia 当前规范布局尚不支持 RTL");
        glyphs.fonts.push_back(scene::skiaRunFonts(run, resources));
        glyphs.mixedFallback.push_back(run.value("mixed_fallback").toBool());
        glyphs.languages.push_back(run.value("language").toString("und").toStdString());
    }
    glyphs.dpr = ir.value("screen").toObject().value("dpr").toDouble(1);
    const auto particleSprites = resolveSkiaParticleSprites(projected);
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
        const bool fadedUtopiaScanline = usesFadedUtopiaScanline(line, style);
        const bool fixedFadedScanline = fadedUtopiaScanline && (style.scanlineMode == "default" || style.scanlineMode == "color");
        const bool roleFadedScanline = fixedFadedScanline && line.bursts.empty()
            && features.contains("p07_inline_role_faded_scanline");
        const bool vectorFadedScanline = fixedFadedScanline
            && (line.bursts.empty() || features.contains("p07_faded_scanline_singing_particles"))
            && features.contains("p07_faded_scanline_prefix_guides");
        const bool brightenedScanlineGradient = fadedUtopiaScanline && line.bursts.empty()
            && style.scanlineMode == "brighten" && features.contains("p06_faded_scanline_palette_gradient");
        const bool ordinaryLineAnimation = (line.entryAnimation == "none" || line.entryAnimation == "fade"
                || line.entryAnimation == "slide_in" || line.entryAnimation == "rise")
            && (line.exitAnimation == "none" || line.exitAnimation == "fade" || line.exitAnimation == "slide_out"
                || line.exitAnimation == "rise");
        const bool animatedGuideGradients = features.contains("p05_vector_guide_composition")
            && features.contains("p06_animated_singing_gradient_fill") && line.karaokeAnimation == "utopia"
            && !style.vertical && !style.rightToLeft && !line.wipeReverse && !line.scanlineEnabled
            && !line.strokeFlashEnabled && !line.zoomPulseEnabled && line.bursts.empty()
            && ordinaryLineAnimation;
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
                const auto duration = [](const std::string& kind, bool entry, int value) {
                    if (kind == "none" || kind == "fade" || kind == "rise"
                        || kind == (entry ? "slide_in" : "slide_out")) return 0;
                    return value > 0 ? value : 600;
                };
                prepared.lineBandTransition = {duration(line.entryAnimation, true, line.entryDurationMs),
                                               duration(line.exitAnimation, false, line.exitDurationMs)};
            }
        } else if (source.contains("line_band_rect")) {
            throw BackendError("Skia 行背景带缺少能力声明");
        }
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
        const bool hasInlineRoles = std::ranges::any_of(line.chars, [](const auto& cell) { return cell.styleIndex >= 0; });
        const bool rtlRuby = style.rightToLeft && features.contains("p05_horizontal_rtl_ruby");
        const bool rtlRubySinging = rtlRuby && singingScale && features.contains("p07_rtl_ruby_singing_scale");
        const bool rtlRubyNoWipe = rtlRuby && prepared.noWipe && features.contains("p07_rtl_ruby_no_wipe");
        const bool rtlRubyMotion = rtlRubySinging && features.contains("p07_rtl_ruby_line_animation");
        // Cell order does not change the per-cell singing pose on lines without readings.
        const bool rtlPlainSinging = style.rightToLeft && singingScale && !annotated && !line.wipeReverse
            && !line.zoomPulseEnabled && features.contains("p07_rtl_singing_scale");
        const bool verticalMotion = style.vertical && features.contains("p07_vertical_decoration_line_motion")
            && ordinaryLineAnimation && (line.karaokeAnimation == "none"
                || (prepared.noWipe && features.contains("p07_vertical_decoration_no_wipe_animation")));
        const bool rtlRubyUnscaledMotion = rtlRuby && !singingScale && ordinaryLineAnimation
            && features.contains("p07_rtl_ruby_unscaled_line_animation");
        const bool vectorGuides = std::ranges::any_of(line.chars, [](const auto& cell) { return bool(cell.vectorGlyph); });
        // A bitmap guide's glow takes the line's glow paint on any line.
        const bool bitmapGradientGlow = features.contains("p08_bitmap_guide_gradient_glow") && !staticTitle;
        const bool reverseBitmapLine = line.wipeReverse && features.contains("p08_reverse_bitmap_guides")
            && !line.chars.empty()
            && std::ranges::all_of(line.chars, [](const auto& cell) { return cell.bitmapGuide && cell.bitmapGuide->noDecor; });
        const bool mixedReverseBitmaps = line.wipeReverse && features.contains("p08_mixed_reverse_bitmap_guides")
            && std::ranges::any_of(line.chars, [](const auto& cell) { return bool(cell.bitmapGuide); });
        const bool verticalSingingAnimation = style.vertical && singingScale && ordinaryLineAnimation
            && !line.wipeReverse && features.contains("p07_vertical_singing_line_animation")
            && (!line.scanlineEnabled || features.contains("p07_vertical_singing_scanline_line_animation"));
        const bool verticalScaledParticles = style.vertical && singingScale && !line.bursts.empty()
            && features.contains("p07_vertical_singing_with_particles");
        const bool verticalSinging = style.vertical && singingScale && features.contains("p07_vertical_singing_scale")
            && (!style.dualLineLayout || features.contains("p07_vertical_columns_singing_scale"))
            && (!line.zoomPulseEnabled || features.contains("p07_vertical_zoom_pulse"))
            && (!line.wipeReverse || (features.contains("p07_vertical_reverse_singing")
                && (!line.scanlineEnabled || features.contains("p07_vertical_reverse_scanline_singing")) && !annotated))
            && !style.rightToLeft && !vectorGuides && !hasInlineRoles
            && (verticalSingingAnimation || (line.entryAnimation == "none" && line.exitAnimation == "none"))
            && (!line.scanlineEnabled || features.contains("p07_vertical_scanline_singing"))
            && !line.strokeFlashEnabled && (line.bursts.empty() || verticalScaledParticles);
        const bool unscaledLineMotion = !style.vertical && !style.rightToLeft && !line.wipeReverse
            && (line.karaokeAnimation == "none" || prepared.noWipe) && ordinaryLineAnimation
            && (line.entryAnimation != "none" || line.exitAnimation != "none")
            && !line.zoomPulseEnabled && !line.strokeFlashEnabled && line.bursts.empty()
            && !hasInlineRoles;
        const auto usesDecoration = [&](const char* kind) {
            return style.decorationKindForState(false) == kind || style.decorationKindForState(true) == kind
                || (annotated && (style.rubyDecorationKindForState(false) == kind
                                  || style.rubyDecorationKindForState(true) == kind));
        };
        const bool horizontalDecorationMotion = unscaledLineMotion && !line.scanlineEnabled
            && !features.contains("p05_lower_ruby");
        const bool horizontalShadowMotion = horizontalDecorationMotion
            && features.contains("p07_horizontal_shadow_line_animation") && usesDecoration("shadow");
        const bool horizontalGlowMotion = horizontalDecorationMotion && glowPaints == GlowPaintSupport::Solid
            && features.contains("p07_horizontal_glow_line_animation")
            && (!prepared.noWipe || features.contains("p07_no_wipe_decoration"))
            && usesDecoration("glow") && !usesDecoration("shadow");
        const bool lowerUnscaledMotion = unscaledLineMotion && features.contains("p07_lower_ruby_unscaled_line_animation")
            && (!line.scanlineEnabled || features.contains("p07_lower_ruby_scanline_line_animation"))
            && std::ranges::any_of(source.value("ruby_groups").toArray(), [](const auto& value) {
                const auto group = value.toObject();
                return group.value("lower").toBool() && (group.contains("lower_glow") || group.contains("lower_shadow"));
            });
        const bool lowerUtopiaMotion = singingScale && ordinaryLineAnimation
            && features.contains("p07_lower_ruby_decoration_line_animation");
        const bool noteExit = line.exitAnimation == "note" && features.contains("p07_ruby_petal_scanline_note_exit");
        const bool petalScanlineImage = noteExit && style.scanlineShape == "image"
            && features.contains("p07_ruby_petal_scanline_image");
        const bool petalScanline = line.scanlineEnabled && features.contains("p07_ruby_petal_entry_scanline")
            && line.entryAnimation == "petal" && (line.exitAnimation == "none" || noteExit) && singingScale
            && (style.scanlineShape == "band" || petalScanlineImage) && style.scanlineMode == "color";
        const bool petalNotePair = noteExit && petalScanline;
        // Readings transition with their owner cells on any horizontal forward line.
        const bool transformedRuby = annotated && !line.wipeReverse && !style.vertical;
        const bool rubyUtopiaExit = transformedRuby && utopiaExit
            && features.contains("p07_ruby_utopia_exit");
        const bool rubyUtopiaEntry = transformedRuby && utopiaEntry
            && features.contains("p07_ruby_utopia_entry");
        const bool spinEntry = line.entryAnimation == "spin_flip" && line.entryDurationMs > 0;
        const bool spinExit = line.exitAnimation == "spin_flip" && line.exitDurationMs > 0;
        const bool fadeEntry = features.contains("p07_character_entry_fade_composition")
            && line.entryAnimation == "char_fade";
        const bool fadeExit = features.contains("p07_ruby_character_exit_fade") && annotated
            && line.exitAnimation == "char_fade"
            && style.rubyDecorationKindForState(false) != "shadow"
            && style.rubyDecorationKindForState(true) != "shadow";
        const bool spinComposition = transformedRuby && (spinEntry || spinExit)
            && (!spinEntry || features.contains("p07_ruby_character_entry_spin"))
            && (!spinExit || features.contains("p07_ruby_character_exit_spin"));
        const bool dripEntry = line.entryAnimation == "char_drip" && line.entryDurationMs > 0;
        const bool dripExit = line.exitAnimation == "char_drip" && line.exitDurationMs > 0;
        const bool dripComposition = transformedRuby && features.contains("p07_ruby_character_drip")
            && (dripEntry || dripExit) && (!(dripEntry && dripExit) || features.contains("p07_ruby_character_drip_pair"));
        const bool warpComposition = spinComposition || dripComposition;
        // Readings take each geometry phase of their owner whatever the other
        // phase, karaoke mode or particles do.
        const bool translationComposition = transformedRuby && features.contains("p07_ruby_character_translation")
            && (line.entryAnimation == "tracking_in" || line.entryAnimation == "wave_in")
            && line.entryDurationMs > 0;
        const bool affineExit = ((features.contains("p07_ruby_character_affine_exit")
                    && (line.exitAnimation == "scatter_out" || line.exitAnimation == "converge_out"))
                || (features.contains("p07_ruby_character_stretch_exit") && line.exitAnimation == "stretch_out"));
        const bool waveScatter = features.contains("p07_ruby_character_wave_scatter")
            && line.entryAnimation == "wave_in" && line.exitAnimation == "scatter_out" && line.entryDurationMs > 0;
        const bool stretchEntry = features.contains("p07_ruby_character_stretch_entry")
            && line.entryAnimation == "stretch_in" && line.entryDurationMs > 0;
        const bool glowTransition = features.contains("p07_ruby_character_glow_transition")
            && ((line.entryAnimation == "glow_in" && line.entryDurationMs > 0)
                || (line.exitAnimation == "glow_out" && line.exitDurationMs > 0));
        const bool affineComposition = transformedRuby
            && ((line.exitDurationMs > 0 && (affineExit || waveScatter)) || stretchEntry || glowTransition);
        const bool pairedParticles = (features.contains("p07_ruby_energy_pair")
                || features.contains("p07_ruby_particle_assembly_pair")
                || features.contains("p07_ruby_particle_entry_exit")
                || features.contains("p07_ruby_mixed_particle_entry_exit") || petalNotePair)
            && line.entryDurationMs > 0 && line.exitDurationMs > 0;
        const bool particleComposition = transformedRuby && !line.bursts.empty()
            && ((line.entryDurationMs > 0 && line.exitAnimation == "none")
                || (line.exitDurationMs > 0 && line.entryAnimation == "none") || pairedParticles)
            && scene::allowsSkiaRubyParticleTransition(entryParticleKind, exitParticleKind, features);
        const bool symbolEntry = line.entryAnimation == "sparkle" || line.entryAnimation == "ripple"
            || line.entryAnimation == "note";
        const bool particleGlowAllowed = particleComposition
            && (line.exitAnimation == "none" || petalNotePair)
            && (petalNotePair || !features.contains("p07_ruby_petal_entry_flame_singing"))
            && (symbolEntry || line.entryAnimation == "heart" || scene::isSkiaFallingParticle(entryParticleKind));
        const bool particleEntryGlow = particleGlowAllowed && features.contains("p07_ruby_particle_entry_glow")
            && (!symbolEntry || features.contains("p07_ruby_symbol_entry_glow"))
            && (style.decorationKindForState(false) == "glow" || style.decorationKindForState(true) == "glow");
        const bool readingEntryGlow = particleEntryGlow
            || (particleGlowAllowed && features.contains("p07_ruby_particle_entry_reading_glow"));
        const auto supportsParticleEntryDecoration = [](const auto& kind, bool glow) {
            return kind == "none" || (glow && kind == "glow");
        };
        const bool geometryComposition = translationComposition || affineComposition || particleComposition;
        // Without readings the singing pose and per-character transition are independent layers.
        const bool singingTransform = !annotated && singingScale && features.contains("p07_singing_character_transform")
            && !line.zoomPulseEnabled && !line.wipeReverse && !style.vertical && !style.rightToLeft
            && !line.scanlineEnabled && !line.strokeFlashEnabled && (line.bursts.empty() || particleEntry || particleExit)
            && !usesDecoration("shadow") && !usesDecoration("glow")
            && (charEntry || charExit || geoEntry || geoExit);
        const bool fadeComposition = (fadeEntry || fadeExit)
            && (line.entryAnimation == "none" || fadeEntry
                || (fadeExit && (line.entryAnimation == "fade" || line.entryAnimation == "rise")))
            && (line.exitAnimation == "none" || fadeExit)
            && (line.karaokeAnimation == "none" || (singingScale && !line.zoomPulseEnabled))
            && !line.wipeReverse && !style.vertical && !style.rightToLeft
            && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty()
            && projected.charStyles.empty()
            && std::ranges::none_of(line.chars, [](const auto& cell) { return bool(cell.vectorGlyph); });
        const bool rubyParticles = annotated && layout.value("sing_twinkle_schema").toDouble(-1) >= 12;
        const bool fadedScanlineParticles = features.contains("p07_faded_scanline_singing_particles")
            && !line.bursts.empty() && fadedUtopiaScanline;
        const bool fadedScanlineGradientFill = fixedFadedScanline && (fadedScanlineParticles || vectorFadedScanline)
            && features.contains("p06_faded_scanline_gradient_fill");
        const bool rubyZoomLineAnimation = annotated && features.contains("p07_ruby_zoom_line_animation")
            && !style.vertical && !style.rightToLeft && !line.wipeReverse && !hasInlineRoles && !vectorGuides
            && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty();
        const bool lineAnimation = verticalSingingAnimation || (singingScale && (!line.zoomPulseEnabled || rubyZoomLineAnimation)
            && features.contains("p07_utopia_line_animation") && ordinaryLineAnimation);
        const bool movingSingingGradientFill = lineAnimation && singingScale && !style.vertical
            && (line.entryAnimation != "none" || line.exitAnimation != "none")
            && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty()
            && features.contains("p06_animated_singing_gradient_fill");
        const bool roleSinging = features.contains("p07_role_singing_scale") && singingScale && !line.zoomPulseEnabled
            && ((line.entryAnimation == "none" && line.exitAnimation == "none" && !line.scanlineEnabled)
                || roleFadedScanline || ((inlineAvatars || roleVectorGuides)
                    && lineAnimation && !line.scanlineEnabled
                    && (line.entryAnimation == "none" || line.entryAnimation == "fade")
                    && (line.exitAnimation == "none" || line.exitAnimation == "fade")))
            && !line.strokeFlashEnabled && line.bursts.empty();
        if (line.scanlineEnabled && !features.contains("p07_scanline_static_cells")) {
        }
        const bool reverseRoleSinging = hasInlineRoles && roleSinging && !line.scanlineEnabled
            && features.contains("p07_reverse_role_singing")
            && std::ranges::all_of(line.chars, [&](const auto& cell) {
                if (cell.styleIndex < 0) return true;
                const auto& look = projected.charStyles[cell.styleIndex];
                return look.strokeWidth == 0 && look.stroke2Width == 0
                    && look.beforeFillPaint.mode == "solid" && look.afterFillPaint.mode == "solid"
                    && look.decorationKindForState(false) == "none" && look.decorationKindForState(true) == "none";
            });
        const bool reverseBitmapGlow = mixedReverseBitmaps && features.contains("p08_reverse_bitmap_singing_glow");
        const bool reverseSingingOutline = features.contains("p07_reverse_singing_outline")
            && line.wipeReverse && !hasInlineRoles && !line.scanlineEnabled
            && std::ranges::all_of(line.chars, [&](const auto& cell) {
                return !cell.bitmapGuide || ((cell.bitmapGuide->noDecor || reverseBitmapGlow) && mixedReverseBitmaps
                    && features.contains("p08_reverse_bitmap_singing_outline"));
            });
        const bool reverseSingingParticles = features.contains("p07_plain_scaled_singing_particles")
            && features.contains("p07_reverse_singing_particles") && !line.scanlineEnabled
            && !hasInlineRoles && !reverseSingingOutline;
        const bool reverseHorizontalSinging = features.contains("p07_horizontal_reverse_singing")
            && !style.vertical && !style.rightToLeft && !annotated && (!hasInlineRoles || reverseRoleSinging)
            && line.karaokeAnimation == "utopia" && line.entryAnimation == "none" && line.exitAnimation == "none"
            && !line.strokeFlashEnabled && !line.zoomPulseEnabled && (line.bursts.empty() || reverseSingingParticles)
            && ((style.strokeWidth == 0 && style.stroke2Width == 0) || reverseSingingOutline);
        const bool unannotatedSingingPaint = features.contains("p06_unannotated_singing_paints")
            && singingScale && !annotated && !style.vertical && !style.rightToLeft
            && (!line.wipeReverse || reverseHorizontalSinging)
            && !hasInlineRoles && !vectorGuides && line.entryAnimation == "none" && line.exitAnimation == "none"
            && (!line.zoomPulseEnabled || (!line.wipeReverse && features.contains("p06_unannotated_zoom_paints")))
            && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty()
            && style.decorationKindForState(false) == "none" && style.decorationKindForState(true) == "none"
            && std::ranges::none_of(line.chars, [](const auto& cell) { return bool(cell.bitmapGuide); });
        prepared.singingScale = {singingScale, line.zoomPulseEnabled, style.zoomPulseCurveLevel, style.zoomPulsePeakPct};
        if ((singingScale || ((rubyUtopiaEntry || rubyUtopiaExit) && source.contains("utopia_origin_x")))
            && annotated && !verticalSinging) {
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
                              line.exitAnimation == "rise" ? -rise : 0.f,
                              horizontalGlowMotion};
        const int delay = cells.size() > 1 ? 350 / (int(cells.size()) - 1) : 0;
        prepared.characterFade = {charEntry && line.entryDurationMs > 0, charExit && line.exitDurationMs > 0,
                                  line.endMs, delay, kindOf(line.entryAnimation), kindOf(line.exitAnimation)};
        prepared.characterGeometry = {geoEntry ? line.entryAnimation : "", geoExit ? line.exitAnimation : "",
                                      geoEntry ? line.entryDurationMs : 0, geoExit ? line.exitDurationMs : 0,
                                      line.endMs, style.fontSize};
        const bool geometry = prepared.characterGeometry.active();
        prepared.particleVisibility = {particleEntry ? effects::fallEntryLeadMs(line.entryAnimation) : 0,
                                       particleExit && exitParticleKind != "dissolve"};
        const auto energyOrNone = [](std::string_view kind) {
            return kind == "none" || kind == "flame" || kind == "lightning";
        };
        const bool outlinedEnergy = features.contains("p07_energy_transition_outlines") && !annotated
            && energyOrNone(line.entryAnimation) && energyOrNone(line.exitAnimation)
            && (particleEntry || particleExit);
        const bool outlinedGeometry = !annotated && line.exitAnimation != "glow_out"
            && features.contains("p07_outlined_character_geometry");
        prepared.rubyGeometryComposition = geometryComposition || (annotated && (geoEntry || geoExit));
        // Inside its window a geometry transition owns the cell pose, whatever else the line sings.
        prepared.geometryOwnsPose = geoEntry || geoExit;
        if (particleEntryGlow) {
            using Glow = ::subtitle::skia::ParticleEntryGlow;
            prepared.particleEntryGlow = symbolEntry ? Glow::SampledSymbols : Glow::Standard;
        }
        const bool transforms = prepared.characterFade.transforms() || geometry;
        prepared.transformedWipe = transforms && allowTransformedWipe;
        const bool pivots = transforms || utopia || (!style.vertical && !line.bursts.empty());
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
                                 std::min(style.scanlineGlowRadius, halfWidth),
                                 style.vertical ? line.wipeReverse : style.rightToLeft != line.wipeReverse,
                                 annotated && features.contains("p07_ruby_scanline") && !(style.vertical && line.wipeReverse),
                                 style.vertical};
            if (features.contains("p07_scanline_shape") && style.scanlineShape != "band") {
                int lastCharEnd = line.endMs;
                for (const auto& cell : line.chars) lastCharEnd = std::max(lastCharEnd, cell.endMs);
                const auto color = prepared.scanline.paint.shade(argb(style.beforeFillPaint.color),
                                                                argb(style.afterFillPaint.color), true);
                const RgbaColor base{std::uint8_t(color >> 16), std::uint8_t(color >> 8),
                                     std::uint8_t(color), std::uint8_t(color >> 24)};
                prepared.scanline.shape = ::subtitle::skia::ScanlineShape::make(
                    style.scanlineShape, base, style.scanlineWidth, style.fontSize,
                    line.displayWindows[0].startMs, line.displayWindows[0].endMs, lastCharEnd);
                if (style.scanlineMode == "default") {
                    const bool flame = style.scanlineShape == "flame";
                    prepared.scanline.shape.outer = flame ? RgbaColor{232, 58, 18, 255} : RgbaColor{143, 211, 255, 255};
                    prepared.scanline.shape.inner = flame ? RgbaColor{255, 176, 46, 255} : RgbaColor{242, 248, 255, 255};
                    prepared.scanline.shape.core = flame ? RgbaColor{255, 241, 184, 255} : RgbaColor{255, 255, 255, 255};
                }
                if (style.scanlineShape == "image")
                    prepared.scanline.image = imageCache.scanline(style, projected.height);
            }
        }
        // vertical_solid_schema=1: the reverse flag means bottom-to-top on a column.
        prepared.vertical = style.vertical;
        prepared.wipeRightToLeft = style.vertical ? line.wipeReverse : style.rightToLeft != line.wipeReverse;
        if (style.vertical) {
            const auto rubyGroups = source.value("ruby_groups").toArray();
            if (!source.value("text_flow").isString() || source.value("text_flow").toString() != "vertical")
                throw BackendError("Skia 竖排最终方向无效");
        } else if (style.rightToLeft || source.contains("text_flow")) {
            if (!source.value("text_flow").isString()
                || source.value("text_flow").toString() != (style.rightToLeft ? "rtl" : "ltr"))
                throw BackendError("Skia 右至左单元排列的最终方向无效");
        }
        // The existing GPU places vertical columns from margins only; page offsets do not apply.
        if (!style.vertical)
            for (const auto& window : line.placementWindows)
                prepared.translations.push_back({window.startMs, window.endMs, window.offsetX, window.offsetY});
        const TextStyle* firstVisiblePaint = nullptr;
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
            // A script-slot override has no effect on non-Latin cells or guides.
            const bool latin = isLatinText(line.chars[j].text);
            if (latin) {
            }
            if (roleFadedScanline) {
                const auto supported = [](const PaintStyle& fill) {
                    return fill.mode == "solid" || fill.mode == "gradient_vertical";
                };
                if (!supported(paint.beforeFillPaint) || !supported(paint.afterFillPaint))
                    throw BackendError("Skia 行内角色扫字线支持实色和纵向渐变填充");
            }
            const float strokeWidth = latin ? paint.latinStrokeWidth : paint.strokeWidth;
            const float stroke2Width = latin ? paint.latinStroke2Width : paint.stroke2Width;
            const auto beforeColor = argb(paint.beforeFillPaint.color);
            const auto afterColor = argb(paint.afterFillPaint.color);
            const double wipePad = std::floor(std::round(strokeWidth / glyphs.dpr) / 2);
            const auto& vector = line.chars[j].vectorGlyph;
            const auto glyphId = sourceChars[j].toObject().value("vector_glyph_id");
            if ((glyphId.isString() && !vector) || (vector && line.chars[j].text != L"\ufffc"))
                throw BackendError("Skia 导唱符缺少有效的来源轮廓引用");
            const auto& bitmap = line.chars[j].bitmapGuide;
            if (bitmap && (vector || line.chars[j].text != L"￼"))
                throw BackendError("Skia 图片导唱符来源无效");
            auto glyph = bitmap
                ? resolvedBitmapGuideCell(cell, *bitmap, imageCache, paint, glyphs.dpr, line.chars[clock].startMs,
                                          line.chars[clock].endMs, beforeColor, afterColor, wipePad,
                                          /*allowAnimation=*/true,
                                          features.contains("p08_bitmap_guide_shadow"), bitmapGradientGlow)
                : vector
                ? glyphs.vectorCell(cell, *vector, glyphId.toString(), paint.fontSize / glyphs.dpr, line.chars[clock].startMs,
                                    line.chars[clock].endMs, beforeColor, afterColor, wipePad)
                : glyphs.cell(cell, QString::fromStdWString(line.chars[j].text), line.chars[clock].startMs,
                              line.chars[clock].endMs, beforeColor, afterColor, wipePad);
            if (style.vertical) {
                applyVerticalGeometry(glyph, cell, line.chars[j].text, verticalVersion, japaneseGlyphs,
                                      verticalTextCells, bool(vector), glyphs.dpr);
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
                const bool expandedOuter = vector && !bodyAlpha && !strokeAlpha;
                if (strokeWidth > 0)
                    glyph.outline = ::subtitle::skia::SolidOutline{
                        ::subtitle::skia::prepareOutlinePath(glyph.path, strokeWidth, staticTitle && vector && !bodyAlpha),
                        beforeOutline, afterOutline, bodyAlpha, strokeWidth};
                if (stroke2Width > 0)
                    glyph.outerOutline = ::subtitle::skia::SolidOutline{
                        ::subtitle::skia::prepareOutlinePath(glyph.path, strokeWidth + stroke2Width, expandedOuter),
                        argb(paint.beforeStroke2Paint.color), argb(paint.afterStroke2Paint.color),
                        bodyAlpha || strokeAlpha, strokeWidth + stroke2Width, expandedOuter};
            }
            glyph.wipePoints = resolvedSkiaWipePoints(cell, ir, line.chars[clock].startMs - int(interval[0].toDouble()), totalPoints);
            if (const auto end = resolvedSkiaWipeEnd(cell, ir)) {
                if (glyph.wipePoints.empty()) glyph.wipePoints = {{glyph.startMs, 0.f}, {glyph.endMs, 1.f}};
                glyph.wipePoints.back().position = *end;
                glyph.adjustedWipeEnd = true;
            }
            // Canonical layout cell and glyph-run box center.
            if (pivots || cell.contains("layout_left")) {
                const auto pivot = [&](const char* key) {
                    const auto value = cell.value(QLatin1String(key));
                    if (!value.isDouble() || !std::isfinite(value.toDouble()) || std::abs(value.toDouble()) > 1e6)
                        throw BackendError("Skia 逐字变换缺少有效的版面单元轴心");
                    return float(value.toDouble() * glyphs.dpr);
                };
                glyph.pivot = {pivot("layout_left"), pivot("layout_right"), pivot("pivot_y"), glyph.left, glyph.right};
                if (glyph.pivot.layoutRight < glyph.pivot.layoutLeft)
                    throw BackendError("Skia 逐字变换的版面单元轴心无效");
                if (utopia) glyph.pivot.baseline = verticalSinging ? glyph.pivot.centerY : pivot("baseline_y");
                if ((transforms || utopia) && (glyph.outline || glyph.outerOutline)) {
                    const auto opaque = [](const ::subtitle::skia::SolidOutline& stroke) {
                        return !stroke.protectBody && (stroke.before >> 24) == 255 && (stroke.after >> 24) == 255;
                    };
                    const bool paintedSinging = singingScale && !transforms
                        && (unannotatedSingingPaint || (!line.zoomPulseEnabled
                            && (features.contains("p06_utopia_main_paint") || fadedScanlineGradientFill
                                || (roleSinging && roleBodyPaints))));
                    const bool translucentOutline = !utopia && !geometry && !annotated && !hasInlineRoles
                        && features.contains("p07_translucent_character_outline");
                    const bool reverseProtectedOutline = reverseHorizontalSinging && reverseSingingOutline
                        && singingScale && !transforms;
                    const auto ink = glyph.path.computeTightBounds();
                    glyph.pivot.inkLeft = ink.fLeft;
                    glyph.pivot.inkRight = ink.fRight;
                    glyph.pivot.wipePad = float(int(paint.strokeWidth) / 2);
                }
            } else if (cell.contains("layout_right") || cell.contains("pivot_y")) {
                throw BackendError("Skia 版面单元轴心缺少逐字特效能力声明");
            }
            if (line.strokeFlashEnabled && !glyph.path.isEmpty())
                glyph.strokeFlashWidth = ::subtitle::skia::strokeFlashBaseWidth(paint.fontSize, paint.strokeWidth);
            if (!firstVisiblePaint && (!glyph.path.isEmpty() || glyph.bitmap))
                firstVisiblePaint = &paint;
            prepared.cells.push_back(std::move(glyph));
        }
        const auto* lampPaint = style.litAppearanceMode == "role" && projected.litDecorStyle
            ? &*projected.litDecorStyle : firstVisiblePaint;
        const auto* volumePaint = style.volumeAppearanceMode == "role" && projected.volumeDecorStyle
            ? &*projected.volumeDecorStyle : firstVisiblePaint;
        const SignalPaintContext signalPaintContext{imageCache, glyphs.dpr, projected.layoutReferenceScale};
        prepared.shapeSignal = resolveShapeSignal(source, line, style, signalPaintContext, lampPaint);
        SkRect volumeTextInk = SkRect::MakeEmpty();
        std::optional<int> leftmostVolumeRole;
        if (style.volumeEnabled && line.volumeHead && style.volumeAppearanceMode != "custom") {
            const auto& palette = volumePaint ? *volumePaint : style;
            const auto paints = {&palette.beforeFillPaint, &palette.afterFillPaint, &palette.beforeStrokePaint,
                                 &palette.afterStrokePaint, &palette.beforeStroke2Paint, &palette.afterStroke2Paint,
                                 &palette.beforeDecorPaint, &palette.afterDecorPaint};
            if (line.signalBandJoin
                || std::ranges::any_of(paints, [](const auto* paint) { return paint->mode == "gradient_horizontal"; })) {
                const auto domains = collectRolePaintBounds(prepared, line, style, projected.charStyles,
                                                           projected.layoutReferenceScale, true);
                leftmostVolumeRole = domains.leftmostRole();
                if (leftmostVolumeRole) volumeTextInk = domains.forRole(*leftmostVolumeRole);
            }
        }
        prepared.volumeSignal = resolveVolumeSignal(source, line, style, signalPaintContext, volumePaint, volumeTextInk);
        const auto volumeBandLeft = line.signalBandJoin && prepared.volumeSignal && leftmostVolumeRole
            ? std::optional(prepared.volumeSignal->bars.front().rect.rect().left()) : std::nullopt;
        const int timingOffset = cells.isEmpty() ? 0
            : line.chars.front().startMs - int(intervals[0].toArray()[0].toDouble());
        auto rubyTransition = RubyTransition::None;
        if (fadeComposition || particleComposition || rubyUtopiaEntry) rubyTransition = RubyTransition::Fade;
        if (spinComposition) rubyTransition = RubyTransition::Spin;
        if (dripComposition) rubyTransition = RubyTransition::Drip;
        if (translationComposition) rubyTransition = RubyTransition::Translation;
        if (affineComposition || (particleComposition && (entryParticleKind == "flame" || exitParticleKind == "flame")))
            rubyTransition = RubyTransition::Affine;
        if (rubyUtopiaExit) rubyTransition = RubyTransition::UtopiaExit;
        // Any other per-character transition: readings follow their owner cell.
        if (rubyTransition == RubyTransition::None && annotated
            && (charEntry || charExit || geoEntry || geoExit || utopiaEntry || utopiaExit))
            rubyTransition = RubyTransition::Fade;
        std::optional<RolePaintBounds> rolePaintBounds;
        const bool gradientScanlineRole = line.scanlineEnabled && style.scanlineMode == "role"
            && style.scanlineRolePaint.mode != "solid" && features.contains("p07_scanline_role_gradient");
        if (roleBodyPaints || roleShadowPaints || gradientGlowWithoutTexturedRoles || gradientScanlineRole || bodyShadowGradients
            || features.contains("p06_ruby_horizontal_outline") || features.contains("p06_lower_ruby_static_gradient")
            || features.contains("p06_lower_ruby_outline_gradient") || features.contains("p06_lower_ruby_glow_gradient")
            || features.contains("p06_independent_ruby_fill") || features.contains("p06_lower_ruby_shadow_gradient")
            || features.contains("p06_ruby_gradient_shadow"))
            rolePaintBounds = collectRolePaintBounds(prepared, line, style, projected.charStyles,
                                                     projected.layoutReferenceScale, staticTitle && roleBodyPaints);
        if (rolePaintBounds && volumeBandLeft) {
            if (auto first = rolePaintBounds->roles.find(*leftmostVolumeRole); first != rolePaintBounds->roles.end())
                first->second.ink.fLeft = std::min(first->second.ink.left(), *volumeBandLeft);
        }
        if (gradientScanlineRole && !rolePaintBounds->line.isEmpty()) {
            const auto& ink = style.scanlineRolePaint.mode == "gradient_horizontal"
                ? rolePaintBounds->forRole(-1) : rolePaintBounds->line;
            auto gradient = ::subtitle::skia::lineGradient(style.scanlineRolePaint, ink, rolePaintBounds->outlinePad);
            for (auto& layers : prepared.scanline.gradients) layers = {gradient, gradient};
        }
        if (roleBodyPaints) {
            if (source.contains("body_gradient_bounds"))
                throw BackendError("Skia 角色画笔不能携带整行画笔范围");
            resolveRoleBodyPaints(prepared, line, style, projected.charStyles, *rolePaintBounds, imageCache,
                                  projected.layoutReferenceScale, roleOutlineGradients, roleOutlinePaints);
        }
        prepared.rubyGroups = resolvedSkiaRubyGroups(source, ir, glyphs, projected, index, timingOffset,
            rubyTransition, imageCache, glowPaints, rolePaintBounds ? &*rolePaintBounds : nullptr,
            {rtlRubyUnscaledMotion || horizontalShadowMotion || horizontalGlowMotion || verticalMotion,
             lowerUnscaledMotion || lowerUtopiaMotion, movingSingingGradientFill},
            totalCells, totalGroups);
        resolveSkiaParticleAnchors(source.value("particle_box_height"), features, line, style,
                                   rubyParticles, particleSprites, imageCache, glyphs.dpr, prepared);
        if (transforms) {
            // Transformed cells are never wiping: entry finishes before each cell starts,
            // exit begins after every cell ends. Overlap uses a per-glyph front (unverified).
            const auto& fade = prepared.characterFade;
            for (std::size_t i = 0; !allowTransformedWipe && i < prepared.cells.size(); ++i) {
                const auto& c = prepared.cells[i];
                const int start = c.wipePoints.empty() ? c.startMs : c.wipePoints.front().timeMs;
                const int end = c.wipePoints.empty() ? c.endMs : c.wipePoints.back().timeMs;
            }
            const auto simpleTransform = [](std::string_view animation) {
                return animation == "none" || animation == "char_drip" || animation == "spin_flip";
            };
            const bool simpleBodyTransform = !geometry && !annotated && !hasInlineRoles && !vectorGuides
                && simpleTransform(line.entryAnimation) && simpleTransform(line.exitAnimation)
                && !line.strokeFlashEnabled && !line.zoomPulseEnabled && line.bursts.empty();
            const bool transformBand = features.contains("p07_scanline_character_transform")
                && simpleBodyTransform && line.karaokeAnimation == "none"
                && style.scanlineShape == "band" && (style.scanlineMode == "default" || style.scanlineMode == "color");
            const bool wholeCellTransform = simpleBodyTransform && features.contains("p07_no_wipe_character_transform");
            const bool directionalTransform = simpleBodyTransform && features.contains("p07_directional_character_transform")
                && line.karaokeAnimation == "none" && !line.scanlineEnabled
                && style.strokeWidth == 0 && style.stroke2Width == 0
                && style.decorationKindForState(false) == "none" && style.decorationKindForState(true) == "none";
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
        const bool movingGradient = (fadedScanlineGradientFill || movingSingingGradientFill || brightenedScanlineGradient)
            && !anyLayer([](const PaintStyle& paint) { return paint.mode != "solid" && paint.mode != "gradient_vertical"; });
        const bool bitmapGuideGradientShadow = features.contains("p08_bitmap_guide_gradient_shadow");
        const bool bitmapGuideImageShadow = features.contains("p08_bitmap_guide_image_shadow");
        const bool hasBitmapGuide = std::ranges::any_of(prepared.cells, [](const auto& cell) { return bool(cell.bitmap); });
        const bool staticShadowLine = line.entryAnimation == "none" && line.exitAnimation == "none";
        const bool roleShadowMotion = !staticShadowLine && ordinaryLineAnimation
            && features.contains("p07_role_shadow_line_animation");
        const bool roleShadowRuby = staticShadowLine && features.contains("p06_role_shadow_ruby");
        const auto solidRoleShadows = [&] {
            return style.beforeDecorPaint.mode == "solid" && style.afterDecorPaint.mode == "solid"
                && std::ranges::all_of(line.chars, [&](const auto& cell) {
                    if (cell.styleIndex < 0) return true;
                    const auto& look = projected.charStyles[cell.styleIndex];
                    return look.beforeDecorPaint.mode == "solid" && look.afterDecorPaint.mode == "solid";
                });
        };
        const bool roleSingingShadow = staticShadowLine && roleSinging && !anyReadings
            && features.contains("p07_role_singing_shadow") && solidRoleShadows();
        // Each role keeps its own shadow (or none) on every line, as in Direct2D.
        const bool inlineShadow = roleStaticShadow && hasInlineRoles;
        prepared.shadow = skiaStaticShadow(style);
        const auto prepareShadow = [&](auto& state, const PaintStyle& paint, bool after) {
            if (!state.enabled || paint.mode == "solid") return;
            const bool imagePaint = paint.mode == "image";
            // Painted shadows compose with every layout and animation; posed
            // cells paint them in line space like the static shadow.
            if (!imagePaint && !rolePaintBounds)
                throw BackendError("Skia 正文渐变阴影缺少绘制范围");
            if (imagePaint) {
                state.image = imageCache.fill(paint, projected.layoutReferenceScale);
                state.color = argb(paint.color);
            } else {
                if (!hasVisibleGradientStops(paint)) return;
                auto ink = inlineShadow && paint.mode == "gradient_horizontal"
                    ? rolePaintBounds->forRole(-1) : rolePaintBounds->line;
                if (hasBitmapGuide && ink.isEmpty()) throw BackendError("Skia 图片导唱符渐变阴影需要同一行存在正文");
                if (paint.mode == "gradient_horizontal") {
                    // Bitmap artwork extends the horizontal paint span only.
                    for (const auto& cell : prepared.cells)
                        if (cell.bitmap) {
                            ink.fLeft = std::min(ink.fLeft, cell.bitmap->box.left());
                            ink.fRight = std::max(ink.fRight, cell.bitmap->box.right());
                        }
                }
                if (ink.isEmpty()) return;
                state.shader = ::subtitle::skia::lineGradient(paint, ink, rolePaintBounds->outlinePad);
            }
            if (hasBitmapGuide)
                for (auto& cell : prepared.cells)
                    if (cell.bitmap && cell.bitmap->shadow[after].enabled) cell.bitmap->shadow[after] = state;
        };
        prepareShadow(prepared.shadow.before, style.beforeDecorPaint, false);
        prepareShadow(prepared.shadow.after, style.afterDecorPaint, true);
        prepared.glow = skiaStaticGlow(style);
        const bool baseOwnsText = std::ranges::any_of(line.chars, [](const auto& cell) {
            return cell.styleIndex < 0 && !cell.bitmapGuide;
        });
        if (reverseBitmapLine || (inlineAvatars && !baseOwnsText)) {
            prepared.shadow = {};
            prepared.glow = {};
        }
        if (rubyUtopiaExit && (prepared.glow.before.radius > 0 || prepared.glow.after.radius > 0)) {
            // The animated halo uses a fixed line rectangle, including the
            // pen extent and the vertical paint pad used by the fill range.
            auto ink = SkRect::MakeEmpty();
            for (const auto& glyph : prepared.cells)
                if (!glyph.path.isEmpty()) ink.join(glyph.path.computeTightBounds());
            if (!ink.isEmpty()) {
                const float pad = ::subtitle::skia::outlineVisualPad(
                    style.strokeWidth, style.stroke2Width, projected.layoutReferenceScale);
                auto fill = ink.makeOutset(0.f, pad);
                fill.fLeft = std::min(fill.left(), prepared.rubySingingOrigin);
                for (const auto& glyph : prepared.cells)
                    fill.fRight = std::max(fill.right(), glyph.pivot.layoutRight);
                prepared.bodyGlowTransitionBounds = fill;
            }
        }
        const bool lineGradientGlow = gradientGlow
            && ((prepared.glow.before.radius > 0 && style.beforeDecorPaint.mode != "solid")
                || (prepared.glow.after.radius > 0 && style.afterDecorPaint.mode != "solid"));
        if (lineGradientGlow || ((roleStaticGlow || ((latinPrimaryOutline || latinSecondaryOutline) && staticGlow))
            && (hasInlineRoles || roleFadedScanline))) {
            resolveRoleGlows(prepared, line, style, projected.charStyles,
                             gradientGlow && rolePaintBounds ? &*rolePaintBounds : nullptr, bitmapGradientGlow);
            prepared.glow = {};
        }
        const bool hasGlow = prepared.glow.before.radius || prepared.glow.after.radius;
        const bool roleGuideShadow = lineAnimation && (!hasInlineRoles || roleSinging)
            && roleVectorGuides && features.contains("p06_static_shadow") && !line.scanlineEnabled
            && !line.strokeFlashEnabled && line.bursts.empty();
        if ((roleGuideShadow && hasInlineRoles) || inlineShadow)
            resolveRoleShadows(prepared, line, projected.charStyles,
                               roleShadowPaints && inlineShadow ? &*rolePaintBounds : nullptr,
                               imageCache, projected.layoutReferenceScale);
        const bool hasShadow = prepared.shadow.before.enabled || prepared.shadow.after.enabled || !prepared.cellShadows.empty();
        const bool hasDecoration = hasGlow || hasShadow;
        const bool singingGlow = singingScale && features.contains("p07_singing_glow")
            && (!line.zoomPulseEnabled || features.contains("p07_zoom_pulse_decoration"));
        const bool particleGlow = (rubyParticles || fadedScanlineParticles) && !hasShadow;
        const bool unscaledScanlineMotion = line.scanlineEnabled && line.karaokeAnimation == "none" && ordinaryLineAnimation
            && features.contains("p07_scanline_unscaled_line_animation");
        const bool lineGlow = (lineAnimation || lowerUnscaledMotion || unscaledScanlineMotion || horizontalGlowMotion
            || fadeComposition || noiseComposition || warpComposition
            || geometryComposition || rubyUtopiaEntry || rubyUtopiaExit)
            && !prepared.shadow.before.enabled && !prepared.shadow.after.enabled;
        const bool flashGlow = hasGlow && !prepared.shadow.before.enabled && !prepared.shadow.after.enabled
            && features.contains("p07_composed_stroke_flash");
        // The band repaints the glyph stack above its existing static glow.
        const bool roleScanlineGlow = features.contains("p07_role_scanline_layers")
            && line.karaokeAnimation == "none"
            && ((line.entryAnimation == "none" && line.exitAnimation == "none")
                || (unscaledScanlineMotion && features.contains("p07_role_scanline_line_animation")))
            && !line.strokeFlashEnabled && (line.bursts.empty() || features.contains("p07_role_scanline_particles"));
        const bool scanlineGlow = hasGlow && !prepared.shadow.before.enabled && !prepared.shadow.after.enabled
            && layout.value("scanline_band_schema").toDouble(-1) >= 6
            && (style.scanlineMode != "role" || roleScanlineGlow);
        const bool scanlineShadow = features.contains("p07_scanline_shadow") && hasShadow && !hasGlow
            && !style.vertical && !style.rightToLeft && !line.wipeReverse
            && style.scanlineShape == "band" && (style.scanlineMode == "color" || style.scanlineMode == "default")
            && line.karaokeAnimation == "none" && line.entryAnimation == "none" && line.exitAnimation == "none"
            && !line.zoomPulseEnabled && !line.strokeFlashEnabled && line.bursts.empty()
            && !hasInlineRoles && !features.contains("p05_lower_ruby");
        const bool noWipeGlow = hasGlow && !prepared.shadow.before.enabled && !prepared.shadow.after.enabled
            && features.contains("p07_no_wipe_decoration");
        const bool rubyMainPaint = !prepared.rubyGroups.empty() && features.contains("p06_ruby_main_paint")
            && (!features.contains("p05_lower_ruby")
                || (fadedScanlineParticles && features.contains("p07_lower_ruby_scanline_singing_particles")));
        const bool singingMainPaint = unannotatedSingingPaint || movingGradient
            || (rubyMainPaint && singingScale && !line.zoomPulseEnabled
            && features.contains("p06_utopia_main_paint"));
        const bool movingGradientShadow = movingSingingGradientFill && movingGradient && hasShadow && !hasGlow;
        const bool stationaryPaint = staticShadowLine && line.karaokeAnimation == "none"
            && !line.zoomPulseEnabled && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty();
        const bool paintedDecoration = (stationaryPaint && !style.vertical && !style.rightToLeft && !line.wipeReverse)
            || ((rubyMainPaint || movingGradient) && (!hasShadow || movingGradientShadow));
        const bool verticalFade = style.vertical && features.contains("p07_vertical_decoration_fade")
            && (line.entryAnimation == "none" || line.entryAnimation == "fade")
            && (line.exitAnimation == "none" || line.exitAnimation == "fade");
        const bool verticalGlowNoWipe = noWipeGlow && features.contains("p07_vertical_glow_no_wipe");
        const bool verticalShadowNoWipe = hasShadow && prepared.noWipe && !hasGlow
            && features.contains("p07_vertical_shadow_no_wipe");
        const bool verticalMixedNoWipe = style.vertical && prepared.noWipe
            && features.contains("p07_vertical_mixed_decoration_no_wipe");
        const bool verticalScanlineDecoration = style.vertical && line.scanlineEnabled
            && features.contains("p07_vertical_scanline_decoration");
        const bool verticalSingingShadow = verticalSinging && features.contains("p07_vertical_singing_shadow");
        const bool verticalDecorationAnimation = verticalFade || verticalMotion || verticalSingingAnimation;
        const bool verticalParticleDecoration = style.vertical && features.contains("p07_vertical_decorated_singing_particles");
        const bool verticalDecoration = style.vertical && columnDecoration
            && (!annotated || features.contains("p06_vertical_ruby_decoration"))
            && (!hasGlow || features.contains("p06_vertical_glow"))
            && (!hasShadow || features.contains("p06_vertical_shadow"))
            && (line.karaokeAnimation == "none" || (verticalSinging && (!hasShadow || verticalSingingShadow))
                || verticalGlowNoWipe || verticalShadowNoWipe || verticalMixedNoWipe)
            && (verticalDecorationAnimation || (line.entryAnimation == "none" && line.exitAnimation == "none"))
            && (!line.scanlineEnabled || verticalScanlineDecoration) && !line.strokeFlashEnabled
            && (line.bursts.empty() || verticalParticleDecoration);
        const bool reverseVerticalDecoration = verticalDecoration && !annotated
            && (!hasGlow || features.contains("p06_vertical_reverse_glow"))
            && (!hasShadow || features.contains("p06_vertical_reverse_shadow"));
        const bool reverseHorizontalNoWipe = line.wipeReverse && prepared.noWipe
            && features.contains("p07_reverse_decoration_no_wipe");
        const bool reverseHorizontalMotion = line.wipeReverse && ordinaryLineAnimation
            && features.contains("p07_reverse_decoration_line_animation");
        const bool reverseHorizontalDecoration = features.contains("p06_reverse_decoration")
            && !style.vertical && !style.rightToLeft && !annotated
            && (line.karaokeAnimation == "none" || reverseHorizontalNoWipe)
            && (reverseHorizontalMotion || (line.entryAnimation == "none" && line.exitAnimation == "none"))
            && !line.scanlineEnabled && !line.strokeFlashEnabled && line.bursts.empty();
        const bool staticPaintStack = stationaryPaint && !hasDecoration && !hasBitmapGuide && !annotated && !hasInlineRoles;
        const bool staticDirectional = staticPaintStack && !style.vertical && style.rightToLeft != line.wipeReverse;
        const bool verticalPaints = staticPaintStack && style.vertical && !vectorGuides
            && features.contains("p06_vertical_body_paints");
        const bool directionalPaints = staticDirectional && features.contains("p06_directional_body_paints");
        const bool directionalOutlines = directionalPaints
            || (staticDirectional && !gradient && !image && features.contains("p06_directional_solid_outline"));
        const bool rtlShadow = rtlRuby && !utopia && hasShadow && features.contains("p06_rtl_ruby_shadow");
        const bool rtlBitmapDecoration = style.rightToLeft && hasBitmapGuide
            && features.contains("p08_rtl_bitmap_guide_decoration") && !annotated && !hasInlineRoles
            && !vectorGuides && !line.wipeReverse && line.karaokeAnimation == "none"
            && line.entryAnimation == "none" && line.exitAnimation == "none" && !line.scanlineEnabled
            && !line.zoomPulseEnabled && !line.strokeFlashEnabled && line.bursts.empty();
        const bool reverseSingingGlow = singingGlow && reverseHorizontalSinging && hasGlow && !hasShadow
            && !gradientGlow && !line.scanlineEnabled && !hasInlineRoles && (!hasBitmapGuide || reverseBitmapGlow)
            && features.contains("p07_reverse_singing_glow");
        if (!roleBodyPaints && (gradient || image)) {
            const bool unscaledPaintFade = features.contains("p06_unscaled_paint_fade")
                && (line.entryAnimation == "none" || line.entryAnimation == "fade")
                && (line.exitAnimation == "none" || line.exitAnimation == "fade")
                && line.karaokeAnimation == "none" && !line.scanlineEnabled && !line.zoomPulseEnabled
                && line.bursts.empty() && prepared.rubyGroups.empty();
            const bool unscaledPaintScanline = features.contains("p06_unscaled_paint_scanline")
                && line.entryAnimation == "none" && line.exitAnimation == "none" && line.karaokeAnimation == "none"
                && line.scanlineEnabled && style.scanlineShape == "band"
                && (style.scanlineMode == "default" || style.scanlineMode == "color")
                && !line.zoomPulseEnabled && line.bursts.empty() && prepared.rubyGroups.empty();
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
                auto ink = SkRect::MakeLTRB(float(rect[0].toDouble() * glyphs.dpr), float(rect[1].toDouble() * glyphs.dpr),
                                           float(rect[2].toDouble() * glyphs.dpr), float(rect[3].toDouble() * glyphs.dpr));
                if (ink.isEmpty()) throw BackendError("Skia 正文渐变的绘制范围为空");
                if (verticalPaints && (std::abs(ink.top() - prepared.cells.front().top) > 1e-3f
                    || std::abs(ink.bottom() - prepared.cells.back().bottom) > 1e-3f))
                    throw BackendError("Skia 竖排画笔范围与列框不一致");
                if (verticalPaints && anyLayer([](const PaintStyle& paint) { return paint.mode == "gradient_horizontal"; })) {
                    // The column's y range is the cell grid. The horizontal gradient uses placed glyphs, including rotation.
                    auto placedInk = SkRect::MakeEmpty();
                    for (const auto& glyph : prepared.cells)
                        if (!glyph.path.isEmpty()) placedInk.join(glyph.path.computeTightBounds());
                    ink.fLeft = placedInk.left();
                    ink.fRight = placedInk.right();
                }
                // Every layer shares the line's visual pad at the layout reference scale.
                const float pad = verticalPaints ? 0.f : ::subtitle::skia::outlineVisualPad(
                    style.strokeWidth, style.stroke2Width, projected.layoutReferenceScale);
                auto horizontalInk = ink;
                if (volumeBandLeft) horizontalInk.fLeft = std::min(ink.left(), *volumeBandLeft);
                const auto gradientFor = [&](const PaintStyle& paint) {
                    return ::subtitle::skia::lineGradient(paint,
                        paint.mode == "gradient_horizontal" ? horizontalInk : ink, pad);
                };
                prepared.beforeGradient = gradientFor(style.beforeFillPaint);
                prepared.afterGradient = gradientFor(style.afterFillPaint);
                if (primaryOutline) {
                    prepared.beforeOutlineGradient = gradientFor(style.beforeStrokePaint);
                    prepared.afterOutlineGradient = gradientFor(style.afterStrokePaint);
                }
                if (secondaryOutline) {
                    prepared.beforeOuterGradient = gradientFor(style.beforeStroke2Paint);
                    prepared.afterOuterGradient = gradientFor(style.afterStroke2Paint);
                }
                if (brightenedScanlineGradient)
                    for (int i = 0; i < 6; ++i)
                        if (layers[i])
                            prepared.scanline.gradients[2 - i / 2][i % 2] =
                                ::subtitle::skia::scanlineGradient(*layers[i], ink, pad, prepared.scanline.paint);
            }
        }
        if (line.wipeReverse) {
            std::reverse(prepared.cells.begin(), prepared.cells.end());
            // Per-cell role glows and shadows were resolved in source order.
            const auto last = prepared.cells.size() - 1;
            for (auto& group : prepared.roleGlows)
                for (auto& index : group.cells) index = last - index;
            std::reverse(prepared.cellShadows.begin(), prepared.cellShadows.end());
        }
        out.lines.push_back(std::move(prepared));
    }
    return out;
}
}
