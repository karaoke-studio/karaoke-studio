#include "skia_ir_projection.h"
#include "skia_ir_contract.h"
#include "skia_lower_projection.h"
#include "style_projection.h"
#include "line_layout_projection.h"
#include "skia_particle_projection.h"
#include "../protocol/render_config_parser.h"
#include "../model/backend_caps.h"
#include <QtCore/QJsonArray>
#include <QtGui/QColor>
#include <map>
#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::scene {
namespace {
// horizontal_rtl_cell_schema=1: multi-codepoint cells whose whole-cell RTL
// placement is GPU-validated. Mirrors engine/render/skia_ir.py exactly.
bool validatedRightToLeftScalar(char32_t c) {
    // Latin incl. extensions and double diacritics, soft hyphen, ZWNJ/ZWJ/CGJ/
    // VS16, punctuation, CJK incl. extension/compatibility/supplementary
    // ideographs, Hangul, fullwidth/halfwidth forms and unpointed Hebrew/Arabic
    // letters. Hebrew points, harakat and emoji keep different GPU placement.
    return (c >= 0x20 && c <= 0x7e) || (c >= 0xa0 && c <= 0x24f) || (c >= 0x300 && c <= 0x36f)
        || (c >= 0x5d0 && c <= 0x5ea) || (c >= 0x621 && c <= 0x64a) || (c >= 0x1e00 && c <= 0x1eff)
        || c == 0x200c || c == 0x200d || (c >= 0x2010 && c <= 0x2027) || (c >= 0x2030 && c <= 0x205e)
        || (c >= 0x3400 && c <= 0x4dbf) || (c >= 0x4e00 && c <= 0x9fff) || (c >= 0xac00 && c <= 0xd7a3)
        || (c >= 0xf900 && c <= 0xfaff) || c == 0xfe0f || (c >= 0xff01 && c <= 0xffee)
        || (c >= 0x20000 && c <= 0x323af);
}
// horizontal_rtl_kana_schema=1: kana, iteration marks and Japanese punctuation
// drawn from nominal glyphs. Combining U+3099/U+309A use kana_mark_schema=1.
bool validatedRightToLeftKana(char32_t c) {
    return (c >= 0x3000 && c <= 0x3029) || (c >= 0x3030 && c <= 0x303f) || (c >= 0x3041 && c <= 0x3096)
        || (c >= 0x309b && c <= 0x309f) || (c >= 0x30a0 && c <= 0x30ff);
}

}
RenderScene skiaSceneFromIr(const QJsonObject& ir, bool staticTitle) {
    validateSkiaIrEnvelope(ir);
    validateSkiaLowerLayout(ir);
    QString error;
    const auto parsed = protocol::parseRenderConfig(ir, &error);
    if (!parsed) throw BackendError(error.toStdString());
    const auto& cfg = *parsed;
    const auto features = ir.value("required_features").toArray();
    const bool mixedGuides = features.contains("p08_mixed_guide_kinds");
    const bool markerFeature = features.contains("p05_ruby_markers");
    for (const auto& ruby : cfg.rubies) {
        const auto reading = ruby.reading.trimmed();
        const bool marker = (reading.isEmpty() || reading == QStringLiteral("^"))
            && std::all_of(ruby.readingParts.begin(), ruby.readingParts.end(), [](const QString& part) {
                return part.trimmed().isEmpty() || part.trimmed() == QStringLiteral("^");
            });
    }
    if (!cfg.rubies.empty()) {
        const auto features = ir.value("required_features").toArray();
        const auto layout = ir.value("skia_layout").toObject();
        const auto lines = layout.value("lines");
        if (!lines.isArray()
            || lines.toArray().size() != int(cfg.lines.size()))
            throw BackendError("Skia 源注音缺少逐行最终布局");
        for (const auto& value : lines.toArray())
            if (!value.isObject() || !value.toObject().value("ruby_groups").isArray())
                throw BackendError("Skia 源注音缺少逐行最终注音组");
        // Source annotations remain provenance only. Their layout and clocks
        // were materialized by Python; never invoke legacy Qt ruby projection.
    }
    const bool rtlRuby = cfg.rightToLeft && features.contains("p05_horizontal_rtl_ruby");
    // Fixed-cell columns. Readings require a separately versioned feature.
    if (cfg.vertical && cfg.rightToLeft)
        throw BackendError("Skia 竖排与右至左排列不能同时启用");
    // Upper and lower annotations over solid main outlines need their own capability.
    const bool outlineRuby = features.contains("p06_outline_ruby");
    const bool multiLaneOutline = features.contains("p06_multi_lane_outline");
    const bool reverseVerticalOutline = cfg.vertical && features.contains("p06_vertical_reverse_outline");
    const bool reverseHorizontalOutline = !cfg.vertical && !cfg.rightToLeft
        && features.contains("p06_reverse_decoration");
    const bool reverseBitmapGuides = features.contains("p08_reverse_bitmap_guides")
        || features.contains("p08_mixed_reverse_bitmap_guides");
    const bool directionalOutlines = features.contains("p06_directional_body_paints")
        || features.contains("p06_directional_solid_outline") || features.contains("p07_reverse_singing_outline");
    const auto& band = cfg.lineBand;
    const bool staticBand = features.contains("p08_static_line_band");
    const bool fittedBand = features.contains("p08_line_band_fit");
    const bool slicedBand = features.contains("p08_line_band_slices");
    const bool rubyBand = features.contains("p08_ruby_line_band");
    const bool staffBand = features.contains("p08_staff_line_band");
    const bool animatedBand = features.contains("p08_animated_staff_line_band") || features.contains("p08_animated_line_band");
    const bool customBandSequence = features.contains("p08_line_band_image_sequence");
    const bool tintedBand = features.contains("p08_tinted_line_band");
    const bool bandWithEffects = features.contains("p08_line_band_with_effects");
    const bool bandTransition = features.contains("p08_line_band_transition");
    const bool positionedStrokeBand = features.contains("p08_line_band_stroke_position");
    const bool strokeBand = features.contains("p08_line_band_outer_stroke") || positionedStrokeBand;
    const bool hasStrokeBand = band.stroke.enabled && band.stroke.sizePx > 0;
    const bool blurBand = features.contains("p08_line_band_blur");
    const bool hasBlurBand = (band.shadow.enabled && band.shadow.sizePx > 0) || (band.glow.enabled && band.glow.sizePx > 0);
    const auto isPng = [](const QString& path) { return path.endsWith(".png", Qt::CaseInsensitive); };
    const bool bandLayersSupported = staffBand ? isPng(band.linesPath) && isPng(band.clefPath)
        : band.linesPath.isEmpty() && band.clefPath.isEmpty();
    const bool webp = band.imagePath.endsWith(".webp", Qt::CaseInsensitive);
    const bool bandImageSupported = customBandSequence ? webp || band.imagePath.endsWith(".gif", Qt::CaseInsensitive)
        : animatedBand ? webp : !staffBand || isPng(band.imagePath);
    const bool bandFitSupported = slicedBand ? band.fillMode == "three_slice"
        : fittedBand ? band.fillMode == "repeat" || band.fillMode == "cover" : band.fillMode == "stretch";
    const bool bandEnabled = band.enabled && !cfg.vertical && !band.imagePath.isEmpty();
    const bool staffLayers = !band.linesPath.isEmpty() || !band.clefPath.isEmpty();
    if (band.enabled && !cfg.vertical && !band.imagePath.isEmpty()
        && ((staffLayers && !(isPng(band.linesPath) && isPng(band.clefPath)))
            || (band.fillMode != "stretch" && band.fillMode != "repeat" && band.fillMode != "cover"
                && band.fillMode != "three_slice")
            || band.stroke.sizePx > 512))
        throw BackendError("Skia 行背景带的素材或填充模式无效");
    const bool viewport = cfg.viewportScalePct != 100 || cfg.viewportRotationDeg
        || cfg.viewportOffsetX || cfg.viewportOffsetY;
    RenderScene out;
    if (staticBand) {
        out.lineBand.enabled = true;
        out.lineBand.imagePath = band.imagePath.toStdWString();
        out.lineBand.fillMode = band.fillMode.toStdString();
        out.lineBand.midMode = band.midMode.toStdString();
        out.lineBand.capLeft = float(band.capLeftPx);
        out.lineBand.capRight = float(band.capRightPx);
        out.lineBand.srcCapLeft = band.srcCapLeftPx;
        out.lineBand.srcCapRight = band.srcCapRightPx;
        out.lineBand.opacity = float(std::clamp(band.opacityPct, 0, 100)) / 100.f;
        const auto color = [](const QColor& value) {
            return RgbaColor{std::uint8_t(value.red()), std::uint8_t(value.green()), std::uint8_t(value.blue()), 255};
        };
        if (strokeBand) {
            auto& stroke = out.lineBand.stroke;
            stroke.enabled = true;
            stroke.size = float(band.stroke.sizePx * cfg.dpr);
            stroke.position = band.stroke.position.toStdString();
            const QColor paint(band.stroke.color);
            if (paint.isValid()) stroke.color = color(paint);
        }
        if (blurBand) {
            const auto projectBlur = [&](const auto& config, LineBandStyle::Effect& effect) {
                if (!config.enabled || config.sizePx <= 0) return;
                effect.enabled = true;
                effect.size = float(config.sizePx * cfg.dpr);
                effect.spreadPx = effect.size * config.spreadPct / 100.f;
                effect.range = float(config.rangePct) / 100.f;
                const QColor paint(config.color);
                if (paint.isValid()) effect.color = color(paint);
                const double radians = config.angleDeg * 3.14159265358979323846 / 180.;
                const double x = config.distancePx > 0 ? std::cos(radians) * config.distancePx : config.offsetXPx;
                const double y = config.distancePx > 0 ? std::sin(radians) * config.distancePx : config.offsetYPx;
                effect.offsetX = float(x * cfg.dpr);
                effect.offsetY = float(y * cfg.dpr);
            };
            projectBlur(band.shadow, out.lineBand.shadow);
            projectBlur(band.glow, out.lineBand.glow);
        }
        if (tintedBand) {
            const QColor tint(band.tint);
            out.lineBand.hasTint = tint.isValid();
            if (out.lineBand.hasTint) out.lineBand.tint = color(tint);
            out.lineBand.tintStrengthPct = std::clamp(band.tintStrengthPct, 0, 100);
        }
        if (staffBand) {
            out.lineBand.linesPath = band.linesPath.toStdWString();
            out.lineBand.clefPath = band.clefPath.toStdWString();
            const QColor lines(band.lineColor), clef(band.clefColor);
            out.lineBand.hasLineColor = lines.isValid();
            out.lineBand.hasClefColor = clef.isValid();
            if (out.lineBand.hasLineColor) out.lineBand.lineColor = color(lines);
            if (out.lineBand.hasClefColor) out.lineBand.clefColor = color(clef);
        }
    }
    out.width = cfg.physicalWidth();
    out.height = cfg.physicalHeight();
    out.layoutReferenceScale = float(cfg.dpr);
    const auto projectSignalRole = [&](std::optional<TextStyle>& output, const QString& roleName) {
        const auto role = roleName.trimmed();
        if (role == "__global__" || !cfg.customStyleSchemes.value(role).toObject().isEmpty()) {
            auto& decoration = output.emplace();
            if (role == "__global__") applyResolvedStyle(decoration, cfg.baseStyle, cfg.dpr);
            else applyResolvedStyle(decoration, protocol::resolvedStyleForRole(cfg, cfg.baseStyle, role), cfg.dpr);
        }
    };
    if (features.contains("p08_decorated_shape_signal") && cfg.baseStyle.litAppearanceMode == "role")
        projectSignalRole(out.litDecorStyle, cfg.baseStyle.litRoleName);
    if (features.contains("p08_volume_signal") && cfg.baseStyle.volumeAppearanceMode == "role")
        projectSignalRole(out.volumeDecorStyle, cfg.baseStyle.volumeRoleName);
    if (viewport) {
        out.viewportScale = float(cfg.viewportScalePct) / 100.f;
        out.viewportRotation = float(cfg.viewportRotationDeg);
        out.viewportOffsetX = float(cfg.viewportOffsetX * cfg.dpr);
        out.viewportOffsetY = float(cfg.viewportOffsetY * cfg.dpr);
        out.viewportAlign = cfg.viewportAlign.toStdString();
    }
    // Pointers refer to the immutable parser cache for this projection only.
    // Materialize a role once, rather than copying a full style into each cell.
    std::map<const protocol::ResolvedStyle*, int> roleStyles;
    const bool allowFade = features.contains("p07_line_fade");
    const bool allowMotion = features.contains("p07_line_motion");
    const bool allowCharacterFade = features.contains("p07_character_fade");
    const bool allowCharacterTransform = features.contains("p07_character_transform");
    const bool allowNoise = features.contains("p07_noise_transition");
    const int transformVersion = ir.value("skia_layout").toObject().value("character_transform_schema").toInt();
    const bool allowGeometry = allowCharacterTransform && transformVersion >= 4;
    const bool allowHalo = allowGeometry && transformVersion >= 5;
    const bool allowFalling = allowHalo && transformVersion >= 6;
    const bool allowParticleTransition = allowFalling && transformVersion >= 7;
    const int twinkleVersion = ir.value("skia_layout").toObject().value("sing_twinkle_schema").toInt(1);
    const auto scanlineVersion = ir.value("skia_layout").toObject().value("scanline_band_schema").toDouble();
    const auto transformAnimation = [=](const QString& animation, bool entry) {
        return animation == "char_drip" || animation == "spin_flip"
            || (allowGeometry && (entry ? animation == "tracking_in" || animation == "wave_in"
                                       : animation == "scatter_out" || animation == "converge_out"))
            || (allowHalo && (entry ? animation == "stretch_in" || animation == "glow_in"
                                    : animation == "stretch_out" || animation == "glow_out"))
            || (allowFalling && isSkiaFallingParticle(animation))
            || (allowParticleTransition && !skiaParticleKindForTransition(animation, entry).isEmpty()
                && (!isSkiaEnergyTransition(animation) || transformVersion >= 8));
    };
    const auto plainLineAnimation = [=](const QString& animation, bool entry) {
        return animation == "none" || (allowFade && animation == "fade")
            || (allowMotion && (animation == (entry ? "slide_in" : "slide_out") || animation == "rise"));
    };
    const auto supportedAnimation = [&](const QString& animation, bool entry) {
        return plainLineAnimation(animation, entry)
            || (features.contains("p07_utopia_transition") && animation == "utopia")
            || (allowNoise && animation == "noise")
            || (allowCharacterFade && animation == "char_fade")
            || (allowCharacterTransform && transformAnimation(animation, entry));
    };
    const auto layoutLines = ir.value("skia_layout").toObject().value("lines").toArray();
    for (std::size_t lineIndex = 0; lineIndex < cfg.lines.size(); ++lineIndex) {
        const auto& source = cfg.lines[lineIndex];
        const auto rubyGroups = layoutLines.at(qsizetype(lineIndex)).toObject().value("ruby_groups").toArray();
        const bool hasLowerReading = std::ranges::any_of(rubyGroups, [](const auto& group) {
            return group.toObject().value("lower").toBool();
        });
        const bool singingScale = source.karaokeAnimation == "utopia" && features.contains("p07_singing_scale");
        if (source.annotationBaselineY
            || (source.karaokeAnimation != "none" && source.karaokeAnimation != "inherit"
                && source.karaokeAnimation != "no_wipe" && source.karaokeAnimation != "utopia")
            || (source.zoomPulseEnabled && source.karaokeAnimation != "utopia"))
            throw BackendError("Skia 行的唱字动画无效");
        TextStyle style;
        const auto& resolved = protocol::resolvedStyleForLine(cfg, source);
        const bool noise = source.entryAnimation == "noise" || source.exitAnimation == "noise";
        const bool composedNoise = features.contains("p07_noise_composition");
        const bool composedFlash = features.contains("p07_composed_stroke_flash");
        const bool composedScanline = features.contains(
            cfg.vertical ? "p07_vertical_zoom_scanline" : "p07_composed_scanline");
        const bool rubyScanline = !rubyGroups.isEmpty() && features.contains("p07_ruby_scanline");
        const bool roleScanlineParticles = features.contains("p07_role_scanline_particles") && rubyScanline
            && !cfg.vertical && !cfg.rightToLeft && resolved.scanlineMode == "role" && resolved.scanlineShape == "band"
            && resolved.scanlineRolePaint.mode == "solid" && !source.zoomPulseEnabled
            && source.entryAnimation == "none" && source.exitAnimation == "none"
            && std::ranges::all_of(source.fxBursts, [](const auto& burst) { return burst.kind == "scanline"; });
        const bool movingRoleScanline = features.contains("p07_role_scanline_line_animation")
            && !cfg.vertical && !cfg.rightToLeft && !source.wipeReverse
            && resolved.scanlineMode == "role" && resolved.scanlineShape == "band"
            && source.karaokeAnimation == "none" && !source.zoomPulseEnabled
            && !source.strokeFlashEnabled && source.fxBursts.empty();
        const bool roleLayers = features.contains("p07_role_scanline_layers")
            && !source.wipeReverse && source.karaokeAnimation == "none"
            && ((source.entryAnimation == "none" && source.exitAnimation == "none") || movingRoleScanline)
            && !source.strokeFlashEnabled && (source.fxBursts.empty() || roleScanlineParticles);
        // The band repaints the posed unit during Utopia singing.
        const bool utopiaSinging = source.karaokeAnimation == "utopia";
        const bool staticScanline = source.entryAnimation == "none" && source.exitAnimation == "none";
        const bool roleSingingScanline = features.contains("p07_role_scanline_singing") && utopiaSinging
            && staticScanline && !cfg.vertical && !cfg.rightToLeft && !source.wipeReverse
            && !hasLowerReading && !source.strokeFlashEnabled && source.fxBursts.empty()
            && resolved.scanlineMode == "role" && resolved.scanlineShape == "band";
        const bool noteExit = source.exitAnimation == "note" && features.contains("p07_ruby_petal_scanline_note_exit");
        const bool particleScanlineImage = noteExit && source.entryAnimation == "petal"
            && resolved.scanlineShape == "image" && features.contains("p07_ruby_petal_scanline_image");
        // Like the GPU, every horizontal line draws its chosen shape in place of the band.
        const bool solidShape = features.contains("p07_scanline_shape") && resolved.scanlineShape != "band";
        const bool movingUtopiaScanline = features.contains("p07_scanline_line_animation") && utopiaSinging;
        const bool movingVerticalSingingScanline = cfg.vertical && utopiaSinging
            && features.contains("p07_vertical_singing_scanline_line_animation");
        const bool movingUnscaledScanline = features.contains("p07_scanline_unscaled_line_animation")
            && source.karaokeAnimation == "none" && !source.wipeReverse;
        const bool movingVerticalBandParticles = cfg.vertical && resolved.scanlineShape == "band"
            && features.contains("p07_vertical_scanline_particle_line_animation");
        const bool fadedBandParticles = hasFadedScanlineSingingParticles(cfg, source, resolved, features);
        const bool movingScanline = (movingUtopiaScanline || movingUnscaledScanline || movingVerticalSingingScanline)
            && (resolved.scanlineShape == "band" || (solidShape && features.contains("p07_scanline_shape_line_animation")))
            && (resolved.scanlineMode != "role" || movingRoleScanline) && plainLineAnimation(source.entryAnimation, true)
            && plainLineAnimation(source.exitAnimation, false)
            && (!source.zoomPulseEnabled || (movingVerticalSingingScanline && composedScanline))
            && !source.wipeReverse && !source.strokeFlashEnabled
            && (source.fxBursts.empty() || movingVerticalBandParticles || fadedBandParticles);
        const bool particleScanline = features.contains("p07_ruby_petal_entry_scanline") && utopiaSinging
            && source.entryAnimation == "petal" && (source.exitAnimation == "none" || noteExit)
            && (resolved.scanlineShape == "band" || particleScanlineImage) && resolved.scanlineMode == "color"
            && !source.zoomPulseEnabled && !source.strokeFlashEnabled && !source.fxBursts.empty();
        const bool utopiaEntryScanline = features.contains("p07_scanline_utopia_entry") && utopiaSinging
            && !cfg.vertical && !cfg.rightToLeft && !source.wipeReverse && rubyGroups.isEmpty()
            && source.entryAnimation == "utopia" && source.exitAnimation == "none"
            && !source.zoomPulseEnabled && !source.strokeFlashEnabled && source.fxBursts.empty()
            && resolved.scanlineShape == "band" && (resolved.scanlineMode == "default" || resolved.scanlineMode == "color");
        const bool reverseSingingScanline = features.contains("p07_horizontal_reverse_singing") && utopiaSinging
            && source.wipeReverse && !cfg.vertical && !cfg.rightToLeft && rubyGroups.isEmpty() && staticScanline
            && !source.strokeFlashEnabled && !source.zoomPulseEnabled && source.fxBursts.empty()
            && resolved.scanlineShape == "band" && (resolved.scanlineMode == "default" || resolved.scanlineMode == "color");
        const bool utopiaScanline = utopiaSinging && scanlineVersion >= 5
            && (!source.zoomPulseEnabled || composedScanline)
            && (!source.wipeReverse || reverseSingingScanline
                || (cfg.vertical && features.contains("p07_vertical_reverse_scanline_singing")))
            && (staticScanline || movingScanline || particleScanline || utopiaEntryScanline);
        // Shaped scanlines use a solid palette and the line's own front.
        // Like the GPU, a shaped scanline draws through any entry, exit or singing particles.
        const bool scanlineShape = solidShape;
        const bool lowerScanline = features.contains("p07_lower_ruby_scanline")
            && (staticScanline || (movingScanline && features.contains("p07_lower_ruby_scanline_line_animation")))
            && resolved.scanlineShape == "band" && resolved.scanlineMode != "role"
            && !source.wipeReverse && !source.zoomPulseEnabled && !source.strokeFlashEnabled
            && (source.fxBursts.empty() || (fadedBandParticles && features.contains("p07_lower_ruby_scanline_singing_particles")));
        const bool verticalScanlineMotion = movingScanline && (movingVerticalSingingScanline
            || (movingUnscaledScanline && features.contains("p07_vertical_scanline_line_animation")));
        const bool mixedVerticalScanline = cfg.vertical && features.contains("p07_vertical_mixed_scanline");
        const bool verticalPaletteBand = features.contains("p07_vertical_scanline_palette")
            && (staticScanline || (movingVerticalSingingScanline && movingScanline))
            && (source.karaokeAnimation == "none"
                || (utopiaScanline && features.contains("p07_vertical_singing_scanline_palette")))
            && (!source.wipeReverse || features.contains("p07_vertical_reverse_scanline_palette"))
            && (resolved.scanlineMode == "brighten" || resolved.scanlineMode == "follow_before"
                || resolved.scanlineMode == "follow_after");
        const bool verticalBand = features.contains("p07_vertical_scanline_band") && (staticScanline || verticalScanlineMotion)
            && resolved.scanlineShape == "band" && (resolved.scanlineMode == "color" || verticalPaletteBand)
            && (source.karaokeAnimation == "none"
                || (utopiaScanline && features.contains("p07_vertical_scanline_singing")))
            && (!source.wipeReverse || features.contains("p07_vertical_reverse_scanline"))
            && !source.strokeFlashEnabled && (source.fxBursts.empty() || ((staticScanline || movingVerticalBandParticles)
                && (!source.wipeReverse || features.contains("p07_vertical_reverse_scanline_particles"))
                && (features.contains("p07_vertical_scanline_singing_particles")
                    || features.contains("p07_vertical_scanline_particles"))));
        const bool rtlBand = cfg.rightToLeft && features.contains("p07_rtl_scanline_band")
            && !cfg.vertical && staticScanline && rubyGroups.isEmpty()
            && resolved.scanlineShape == "band" && resolved.scanlineMode != "role"
            && source.karaokeAnimation == "none" && !source.strokeFlashEnabled && !source.zoomPulseEnabled
            && source.fxBursts.empty();
        applyResolvedStyle(style, resolved, cfg.dpr);
        if (source.scanlineEnabled && style.scanlineMode == "role" && style.scanlineRolePaint.mode != "solid") {
            const auto& paint = style.scanlineRolePaint.mode;
            const bool gradient = paint == "gradient_horizontal" || paint == "gradient_vertical" || paint == "split_vertical";
        }
        style.rightToLeft = cfg.rightToLeft;
        style.vertical = cfg.vertical;
        if (style.vertical) {
            style.litEnabled = false;
            style.volumeEnabled = false;
        }
        style.layoutSemantics = cfg.layoutSemantics.toStdString();
        style.laneCount = cfg.dualLineLayout ? std::max(int(cfg.lineAlignments.size()), 1) : 1;
        style.dualLineLayout = cfg.dualLineLayout;
        style.alignment = (cfg.lineHorizontalLayout == "center" || source.centerOverride)
            ? "center" : (cfg.lineAlignments.empty() ? "left" : cfg.lineAlignments.front().toStdString());
        style.horizontalMargin = float(cfg.horizontalMarginPx * cfg.dpr);
        style.smartHorizontal = cfg.lineHorizontalLayout == "asymmetric" ? cfg.smartHorizontal.toStdString() : "none";
        style.verticalPosition = cfg.lineYPosition.toStdString();
        style.bottomMargin = float(cfg.lineYMarginPx * cfg.dpr);
        style.leadInMs = cfg.lineLeadInMs;
        style.tailMs = cfg.lineTailMs;
        applyLineLayout(style, source.layout, source.lane, source.centerOverride, source.pageLineCount, cfg.dpr);
        style.layoutOffsetX += float(source.layoutOffsetX * cfg.dpr);
        style.layoutOffsetY += float(source.layoutOffsetY * cfg.dpr);
        // The existing GPU omits every upper/lower reading on a reverse line;
        // sources stay provenance, so a final group there is a forged layout.
        // A missing final line is rejected by the resolved-layout line count.
        const auto finalLines = ir.value("skia_layout").toObject().value("lines").toArray();
        const auto finalIndex = qsizetype(out.lines.size());
        if (cfg.vertical && finalIndex < finalLines.size()) {
            const auto lane = finalLines.at(finalIndex).toObject().value("vertical_lane");
            if (cfg.dualLineLayout ? (!lane.isDouble() || lane.toDouble() != source.lane
                    || source.lane < 0 || source.lane >= style.laneCount) : !lane.isUndefined())
                throw BackendError("Skia 竖排列号与最终布局不一致");
        }
        if (source.wipeReverse && finalIndex < finalLines.size()) {
            const auto resolvedLine = finalLines.at(finalIndex).toObject();
            if (!resolvedLine.value("ruby_groups").toArray().isEmpty())
                throw BackendError("Skia 反向走字行不能携带注音组");
            const auto cells = resolvedLine.value("cells").toArray();
            const bool endpoints = std::any_of(cells.begin(), cells.end(), [](const QJsonValue& cell) {
                return cell.toObject().contains("wipe_end_position");
            });
        }
        TextLine line;
        line.litHead = source.litHead;
        line.volumeHead = source.volumeHead;
        line.signalBandJoin = source.signalBandJoin;
        line.wipeReverse = source.wipeReverse;
        line.entryAnimation = source.entryAnimation.toStdString();
        line.entryDurationMs = source.entryDurationMs;
        line.exitAnimation = source.exitAnimation.toStdString();
        line.exitDurationMs = source.exitDurationMs;
        line.karaokeAnimation = source.karaokeAnimation.toStdString();
        line.zoomPulseEnabled = source.zoomPulseEnabled;
        line.scanlineEnabled = source.scanlineEnabled;
        line.strokeFlashEnabled = source.strokeFlashEnabled;
        line.lane = source.lane;
        const int offset = cfg.timingOffsetMs + source.sourceOffsetMs;
        line.startMs = source.chars.empty() ? 0 : source.chars.front().startMs + offset;
        line.endMs = source.endMs + offset;
        projectSkiaParticles(cfg, source, resolved, offset, twinkleVersion, !rubyGroups.isEmpty(), features, out, line);
        if ((!source.displayStartMs || !source.displayEndMs) && !source.chars.empty())
            throw BackendError("Skia P04: Python-resolved display window is required");
        line.displayWindows.push_back({source.displayStartMs.value_or(0) + offset, source.displayEndMs.value_or(0) + offset, 0, 0});
        for (const auto& window : source.placementWindows)
            line.placementWindows.push_back({window.startMs + offset, window.endMs + offset,
                window.offsetX * float(cfg.dpr), window.offsetY * float(cfg.dpr)});
        const bool composedGuide = features.contains("p05_vector_guide_composition");
        const bool guideWholeLineAnimation = plainLineAnimation(source.entryAnimation, true)
            && plainLineAnimation(source.exitAnimation, false);
        const bool guideRubyAnimation = staticScanline
            || (features.contains("p07_ruby_line_animation") && guideWholeLineAnimation);
        const bool vectorGuideShadow = features.contains("p06_vector_guide_shadow") && rubyGroups.isEmpty()
            && !cfg.vertical && !cfg.rightToLeft && !source.wipeReverse && !source.scanlineEnabled
            && (protocol::decorationKindForState(resolved, false) == "shadow"
                || protocol::decorationKindForState(resolved, true) == "shadow");
        const bool wholeLineGuideShadow = vectorGuideShadow && (staticScanline
            || (features.contains("p07_horizontal_shadow_line_animation")
                && guideWholeLineAnimation))
            && !source.scanlineEnabled
            && source.karaokeAnimation == "none" && !source.strokeFlashEnabled
            && !source.zoomPulseEnabled && source.fxBursts.empty();
        const auto styledTransition = [&](const QString& animation, bool entry) {
            return plainLineAnimation(animation, entry) || animation == "char_drip" || animation == "spin_flip";
        };
        const bool styledCellTransform = features.contains("p07_character_transform_cells")
            && !cfg.vertical && !cfg.rightToLeft && !source.wipeReverse && rubyGroups.isEmpty()
            && styledTransition(source.entryAnimation, true) && styledTransition(source.exitAnimation, false)
            && source.karaokeAnimation == "none" && !source.strokeFlashEnabled && !source.zoomPulseEnabled
            && !source.scanlineEnabled && source.fxBursts.empty();
        const bool rtlVectorGuide = cfg.rightToLeft && features.contains("p05_rtl_vector_guides");
        const bool verticalVectorGuide = cfg.vertical && features.contains("p05_vertical_vector_guides")
            && guideWholeLineAnimation && !source.scanlineEnabled
            && (source.karaokeAnimation == "none" || source.karaokeAnimation == "no_wipe")
            && !source.strokeFlashEnabled && !source.zoomPulseEnabled && source.fxBursts.empty()
            && ((!features.contains("p06_static_shadow") && !features.contains("p06_static_glow"))
                || features.contains("p06_vertical_vector_guide_decoration"));
        const bool singingGuide = composedGuide && !cfg.vertical && !cfg.rightToLeft
            && (!source.wipeReverse || features.contains("p07_reverse_role_singing"))
            && !source.scanlineEnabled && !source.zoomPulseEnabled && !source.strokeFlashEnabled
            && source.fxBursts.empty() && source.karaokeAnimation == "utopia"
            && (source.entryAnimation == "none" || source.entryAnimation == "fade")
            && (source.exitAnimation == "none" || source.exitAnimation == "fade");
        const bool roleVectorGuides = singingGuide && features.contains("p05_role_vector_guides");
        const bool unscaledRoleVectorGuide = features.contains("p05_inline_roles")
            && !cfg.vertical
            && (source.karaokeAnimation == "none" || source.karaokeAnimation == "no_wipe")
            && (guideWholeLineAnimation || styledCellTransform);
        const bool roleFadedScanline = features.contains("p07_inline_role_faded_scanline");
        const bool vectorFadedScanline = features.contains("p07_faded_scanline_prefix_guides");
        const bool fixedShape = scanlineShape && resolved.scanlineMode == "color"
            && (resolved.scanlineShape == "image" || resolved.scanlineShape == "flame");
        const bool staticScanlineCells = features.contains("p07_scanline_static_cells") && staticScanline
            && !cfg.vertical && (!cfg.rightToLeft || rtlBand) && !source.wipeReverse && rubyGroups.isEmpty()
            && source.karaokeAnimation == "none"
            && (resolved.scanlineShape == "band" || fixedShape) && resolved.scanlineMode != "role"
            && !source.strokeFlashEnabled && !source.zoomPulseEnabled && source.fxBursts.empty();
        const bool fadedGuideParticles = vectorFadedScanline
            && (resolved.scanlineMode == "default" || resolved.scanlineMode == "color")
            && hasFadedScanlineSingingParticles(cfg, source, resolved, features);
        const bool staticForwardParticles = staticScanline && !cfg.vertical && !cfg.rightToLeft && !source.wipeReverse
            && rubyGroups.isEmpty() && !source.scanlineEnabled && !source.strokeFlashEnabled;
        const bool staticCellParticles = staticForwardParticles
            && source.karaokeAnimation == "none" && !source.zoomPulseEnabled;
        const bool scaledGuideParticles = staticForwardParticles && singingScale
            && features.contains("p07_plain_scaled_singing_particles");
        const bool inlineBitmapGuides = features.contains("p08_inline_bitmap_guides") && !staticTitle;
        const bool decoratedInlineBitmap = features.contains("p08_inline_bitmap_decoration");
        const bool rtlBitmapSinging = features.contains("p08_rtl_bitmap_singing") && utopiaSinging
            && protocol::decorationKindForState(resolved, false) != "shadow"
            && protocol::decorationKindForState(resolved, true) != "shadow";
        const bool rtlBitmapUpperRuby = features.contains("p08_rtl_bitmap_upper_ruby")
            && guideRubyAnimation && !source.scanlineEnabled && !source.wipeReverse
            && (source.karaokeAnimation == "none" || rtlBitmapSinging)
            && !source.strokeFlashEnabled && !source.zoomPulseEnabled && source.fxBursts.empty();
        const bool rtlBitmap = cfg.rightToLeft && features.contains("p08_rtl_bitmap_guides")
            && (rubyGroups.isEmpty() || rtlBitmapUpperRuby);
        const bool rtlBitmapDecoration = rtlBitmap && features.contains("p08_rtl_bitmap_guide_decoration")
            && guideWholeLineAnimation && !source.wipeReverse && !source.scanlineEnabled
            && (source.karaokeAnimation == "none" || rtlBitmapSinging)
            && !source.strokeFlashEnabled && !source.zoomPulseEnabled && source.fxBursts.empty();
        const bool verticalBitmap = cfg.vertical && features.contains("p08_vertical_bitmap_guides")
            && guideWholeLineAnimation && !source.scanlineEnabled
            && source.karaokeAnimation == "none" && !source.strokeFlashEnabled && !source.zoomPulseEnabled
            && source.fxBursts.empty() && (features.contains("p08_vertical_bitmap_decoration")
                || (!features.contains("p06_static_shadow") && !features.contains("p06_static_glow")));
        const bool reverseBitmapLine = source.wipeReverse && inlineBitmapGuides
            && features.contains("p08_reverse_bitmap_guides") && source.karaokeAnimation == "none"
            && guideWholeLineAnimation
            && !source.scanlineEnabled && !source.strokeFlashEnabled && !source.zoomPulseEnabled && source.fxBursts.empty()
            && std::ranges::all_of(source.chars, [](const auto& cell) { return cell.bitmapGuide && cell.bitmapGuide->noDecor; });
        const bool reverseBitmapSingingGlow = features.contains("p08_reverse_bitmap_singing_glow")
            && protocol::decorationKindForState(resolved, false) != "shadow"
            && protocol::decorationKindForState(resolved, true) != "shadow";
        const bool reverseBitmapSinging = source.karaokeAnimation == "utopia" && features.contains("p08_reverse_bitmap_singing")
            && std::ranges::all_of(source.chars, [&](const auto& cell) {
                return !cell.vectorGlyph && (!cell.bitmapGuide || cell.bitmapGuide->noDecor || reverseBitmapSingingGlow);
            });
        const bool reverseMixedBitmaps = source.wipeReverse && features.contains("p08_mixed_reverse_bitmap_guides")
            && !cfg.vertical && !cfg.rightToLeft
            && ((source.karaokeAnimation == "none" && guideWholeLineAnimation) || (reverseBitmapSinging && staticScanline))
            && !source.scanlineEnabled
            && !source.strokeFlashEnabled && !source.zoomPulseEnabled && source.fxBursts.empty();
        bool sawBitmapText = false;
        for (const auto& cell : source.chars) {
            if (cfg.vertical && std::ranges::any_of(cell.text.toUcs4(), [](char32_t scalar) {
                    const auto category = QChar::category(scalar);
                    return category == QChar::Other_Control || category == QChar::Other_Surrogate;
                }))
                throw BackendError("Skia 竖排单元包含控制字符或无效码点");
            if (cfg.vertical && &protocol::resolvedStyleForCharacter(cfg, source, cell) != &resolved)
                throw BackendError("Skia 竖排单元包含尚未支持的行内角色");
            if (cfg.rightToLeft && cell.text.toUcs4().size() > 1) {
                bool valid = features.contains("p05_horizontal_rtl_cells");
                for (const auto c : cell.text.toUcs4()) {
                    // Isolated, repeated and non-kana-base marks also keep GPU placement.
                    valid = valid && ((c == 0x3099 || c == 0x309a)
                        ? features.contains("p05_horizontal_rtl_kana_marks")
                        : (validatedRightToLeftScalar(c)
                            || (validatedRightToLeftKana(c) && features.contains("p05_horizontal_rtl_kana_cells"))));
                }
                if (!valid) throw BackendError("Skia 右至左多码点单元缺少版本化能力或包含未验证字符");
            }
            const auto& cellStyle = protocol::resolvedStyleForCharacter(cfg, source, cell);
            // Ruby and outlines use their own composition profile.
            const bool lowerVectorClock = source.karaokeAnimation == "none"
                || (!cfg.rightToLeft && (singingScale || source.karaokeAnimation == "no_wipe"));
            if (cell.bitmapGuide) {
                const bool needsInline = !staticTitle && (sawBitmapText || &cellStyle != &resolved);
                // Bitmap guides compose with every layout, animation and reading;
                // only their versioned capability is required.
                if (cell.vectorGlyph)
                    throw BackendError("Skia 图片导唱符与矢量导唱符冲突");
                // An inline guide outside a title still needs its declared capability.
            }
            sawBitmapText = sawBitmapText || !cell.bitmapGuide;
            if (!cell.resolvedEndMs)
                throw BackendError("Skia 单元缺少已解析的计时");
            TextChar ch;
            if (&cellStyle != &resolved) {
                auto [entry, inserted] = roleStyles.try_emplace(&cellStyle, int(out.charStyles.size()));
                if (inserted) {
                    TextStyle roleStyle = style;
                    applyResolvedStyle(roleStyle, cellStyle, cfg.dpr);
                    // Only the line hosts signals. Cells supply geometry and paint.
                    roleStyle.litEnabled = false;
                    roleStyle.volumeEnabled = false;
                    out.charStyles.push_back(std::move(roleStyle));
                }
                ch.styleIndex = entry->second;
            }
            ch.text = cell.text.toStdWString();
            ch.vectorGlyph = cell.vectorGlyph;
            ch.bitmapGuide = cell.bitmapGuide;
            // Legacy family hints are provenance only. The resolved font
            // resource chain is verified by resolvedSkiaInput, never rematched.
            ch.startMs = cell.startMs + offset;
            ch.endMs = *cell.resolvedEndMs + offset;
            line.chars.push_back(std::move(ch));
        }
        out.lines.push_back(std::move(line));
        out.lineStyles.push_back(std::move(style));
    }
    return out;
}
}
