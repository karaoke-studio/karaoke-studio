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
    return (c >= 0x20 && c <= 0x7e) || (c >= 0xa0 && c <= 0xff && c != 0xad)
        || (c >= 0x300 && c <= 0x36f && c != 0x34f && (c < 0x35c || c > 0x362))
        || (c >= 0x4e00 && c <= 0x9fff) || (c >= 0xac00 && c <= 0xd7a3);
}
// horizontal_rtl_kana_schema=1: kana and Japanese punctuation drawn from
// nominal glyphs. Combining U+3099/U+309A are excluded (the GPU neither
// composes them nor applies fallback mark positioning).
bool validatedRightToLeftKana(char32_t c) {
    return c == 0x3000 || c == 0x3001 || c == 0x3002 || (c >= 0x300c && c <= 0x300f) || c == 0x301c
        || (c >= 0x3041 && c <= 0x3096) || c == 0x309b || c == 0x309c || (c >= 0x30a1 && c <= 0x30fc)
        || c == 0xff01 || c == 0xff1f;
}

}
RenderScene skiaSceneFromIr(const QJsonObject& ir) {
    validateSkiaIrEnvelope(ir);
    validateSkiaLowerLayout(ir);
    QString error;
    const auto parsed = protocol::parseRenderConfig(ir, &error);
    if (!parsed) throw BackendError(error.toStdString());
    const auto& cfg = *parsed;
    const auto features = ir.value("required_features").toArray();
    const bool markerFeature = features.contains("p05_ruby_markers");
    if (markerFeature && ir.value("skia_layout").toObject().value("ruby_marker_schema").toDouble(-1) != 1)
        throw BackendError("Skia 短语标记布局版本无效");
    for (const auto& ruby : cfg.rubies) {
        const auto reading = ruby.reading.trimmed();
        const bool marker = (reading.isEmpty() || reading == QStringLiteral("^"))
            && std::all_of(ruby.readingParts.begin(), ruby.readingParts.end(), [](const QString& part) {
                return part.trimmed().isEmpty() || part.trimmed() == QStringLiteral("^");
            });
        if (marker && !markerFeature) throw BackendError("Skia 空读音或短语标记缺少能力声明");
    }
    if (!cfg.rubies.empty()) {
        const auto features = ir.value("required_features").toArray();
        const auto layout = ir.value("skia_layout").toObject();
        const auto lines = layout.value("lines");
        if (!features.contains("p05_upper_ruby") || !features.contains("p05_resolved_ruby")
            || layout.value("ruby_schema").toDouble(-1) != 1 || !lines.isArray()
            || lines.toArray().size() != int(cfg.lines.size()))
            throw BackendError("Skia 源注音需要版本化的最终上注音布局");
        for (const auto& value : lines.toArray())
            if (!value.isObject() || !value.toObject().value("ruby_groups").isArray())
                throw BackendError("Skia 源注音缺少逐行最终注音组");
        // Source annotations remain provenance only. Their layout and clocks
        // were materialized by Python; never invoke legacy Qt ruby projection.
    }
    if (cfg.rightToLeft && (!features.contains("p05_horizontal_rtl")
        || !cfg.rubies.empty()))
        throw BackendError("Skia 右至左单元排列缺少能力声明或携带未支持注音");
    // Fixed-cell columns. Readings require a separately versioned feature.
    if (cfg.vertical && (!features.contains("p05_vertical_solid") || cfg.rightToLeft
        || (!cfg.rubies.empty() && !features.contains("p05_vertical_ruby"))
        || (cfg.dualLineLayout && !features.contains("p05_vertical_columns"))
        || std::any_of(cfg.lineOffsetsX.begin(), cfg.lineOffsetsX.end(), [](int v) { return v != 0; })))
        throw BackendError("Skia 竖排列布局或能力声明无效");
    // Upper and lower annotations over solid main outlines need their own capability.
    const bool outlineRuby = features.contains("p06_outline_ruby");
    const bool multiLaneOutline = features.contains("p06_multi_lane_outline");
    const bool reverseVerticalOutline = cfg.vertical && features.contains("p06_vertical_reverse_outline");
    if (features.contains("p06_primary_solid_outline")
        && ((cfg.vertical && !features.contains("p06_vertical_outline"))
            || cfg.rightToLeft || (cfg.dualLineLayout && !multiLaneOutline)
            || (features.contains("p05_reverse_wipe") && !reverseVerticalOutline)
            || (!outlineRuby && (!cfg.rubies.empty() || features.contains("p05_resolved_ruby")))))
        throw BackendError("Skia 描边缺少对应布局能力");
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
        : animatedBand ? webp : isPng(band.imagePath);
    const bool bandFitSupported = slicedBand ? band.fillMode == "three_slice"
        : fittedBand ? band.fillMode == "repeat" || band.fillMode == "cover" : band.fillMode == "stretch";
    if ((band.enabled && !band.imagePath.isEmpty()) != staticBand
        || (staticBand && (!bandFitSupported
            || !bandImageSupported
            || cfg.vertical || cfg.rightToLeft || (tintedBand == band.tint.isEmpty())
            || (rubyBand != features.contains("p05_resolved_ruby")) || (!rubyBand && !cfg.rubies.empty())
            || !bandLayersSupported || band.edgeKind != "none" || blurBand != hasBlurBand
            || strokeBand != hasStrokeBand || (strokeBand && (positionedStrokeBand == (band.stroke.position == "outside")
                || band.stroke.sizePx > 512)))))
        throw BackendError("Skia 行背景带当前支持横排 PNG、GIF、WebP 和内置预设");
    const bool viewport = cfg.viewportScalePct != 100 || cfg.viewportRotationDeg
        || cfg.viewportOffsetX || cfg.viewportOffsetY;
    if (viewport && (!features.contains("p08_viewport") || cfg.vertical || cfg.rightToLeft))
        throw BackendError("Skia 字幕视口变换缺少横排布局能力声明");
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
    const auto bandAnimation = [&](const QString& animation, bool entry) {
        return plainLineAnimation(animation, entry) || (bandTransition
            && (animation == "char_fade" || isSkiaEnergyTransition(animation)));
    };
    const auto supportedAnimation = [&](const QString& animation, bool entry) {
        return plainLineAnimation(animation, entry)
            || (features.contains("p07_utopia_transition") && animation == "utopia")
            || (allowNoise && animation == "noise")
            || (allowCharacterFade && animation == "char_fade")
            || (allowCharacterTransform && transformAnimation(animation, entry));
    };
    for (const auto& source : cfg.lines) {
        const bool singingScale = source.karaokeAnimation == "utopia" && features.contains("p07_singing_scale");
        if (source.annotationBaselineY
            || !supportedAnimation(source.entryAnimation, true)
            || !supportedAnimation(source.exitAnimation, false)
            || (source.karaokeAnimation != "none" && source.karaokeAnimation != "inherit"
                && source.karaokeAnimation != "no_wipe" && !singingScale)
            || (source.zoomPulseEnabled && !singingScale)
            || (!source.fxBursts.empty() && !features.contains("p07_sing_twinkle")))
            throw BackendError("Skia P04: unsupported line feature");
        TextStyle style;
        const auto& resolved = protocol::resolvedStyleForLine(cfg, source);
        if (staticBand && (!bandAnimation(source.entryAnimation, true)
                || !bandAnimation(source.exitAnimation, false)
                || ((source.scanlineEnabled || !source.fxBursts.empty()) && !bandWithEffects)
                || resolved.litEnabled || resolved.volumeEnabled))
            throw BackendError("Skia 行背景带缺少对应行出入场能力");
        const bool noise = source.entryAnimation == "noise" || source.exitAnimation == "noise";
        const bool composedNoise = features.contains("p07_noise_composition");
        if (noise && (cfg.vertical || cfg.rightToLeft || source.wipeReverse
                || ((!cfg.rubies.empty() || features.contains("p05_resolved_ruby") || singingScale) && !composedNoise)
                || source.scanlineEnabled || source.zoomPulseEnabled
                || (source.entryAnimation != "none" && source.entryAnimation != "noise")
                || (source.exitAnimation != "none" && source.exitAnimation != "noise")
                || source.karaokeAnimation == "no_wipe" || !source.fxBursts.empty() || source.strokeFlashEnabled))
            throw BackendError("Skia 噪声出入场当前仅支持正向横排实色正文");
        const bool composedFlash = features.contains("p07_composed_stroke_flash");
        const bool composedScanline = features.contains("p07_composed_scanline");
        if (source.strokeFlashEnabled
            && (!features.contains("p07_stroke_flash") || cfg.vertical || cfg.rightToLeft || source.wipeReverse
                || (!composedFlash && (!cfg.rubies.empty() || features.contains("p05_resolved_ruby")
                    || source.karaokeAnimation == "utopia"))
                || (source.scanlineEnabled && !composedScanline)
                || (source.zoomPulseEnabled && !features.contains("p07_zoom_pulse_decoration"))
                || source.entryAnimation != "none" || source.exitAnimation != "none"
                || (source.karaokeAnimation == "no_wipe" && !features.contains("p07_no_wipe_decoration"))
                || !source.fxBursts.empty()))
            throw BackendError("Skia 唱字描边闪光当前仅支持正向横排实色正文");
        const bool rubyScanline = features.contains("p07_ruby_scanline");
        const bool roleLayers = features.contains("p07_role_scanline_layers")
            && !source.wipeReverse && source.karaokeAnimation == "none"
            && source.entryAnimation == "none" && source.exitAnimation == "none"
            && !source.strokeFlashEnabled && source.fxBursts.empty();
        // The band follows the posed unit during forward Utopia singing.
        const bool utopiaSinging = source.karaokeAnimation == "utopia";
        const bool staticScanline = source.entryAnimation == "none" && source.exitAnimation == "none";
        const bool shapedSinging = features.contains("p07_shaped_scanline_singing_particles") && staticScanline
            && std::ranges::none_of(source.fxBursts, [](const auto& burst) { return burst.kind == "scanline"; });
        const bool solidShape = features.contains("p07_scanline_shape")
            && (resolved.scanlineShape == "flame" || resolved.scanlineShape == "bubble"
                || (resolved.scanlineShape == "image" && features.contains("p07_scanline_image")))
            && resolved.scanlineMode == "color" && !source.wipeReverse
            && !source.zoomPulseEnabled && !source.strokeFlashEnabled && (source.fxBursts.empty() || shapedSinging);
        const bool movingScanline = features.contains("p07_scanline_line_animation") && utopiaSinging
            && (resolved.scanlineShape == "band" || (solidShape && features.contains("p07_scanline_shape_line_animation")))
            && resolved.scanlineMode != "role" && plainLineAnimation(source.entryAnimation, true)
            && plainLineAnimation(source.exitAnimation, false)
            && !source.zoomPulseEnabled && !source.strokeFlashEnabled && source.fxBursts.empty();
        const bool utopiaScanline = utopiaSinging && scanlineVersion >= 5
            && (!source.zoomPulseEnabled || composedScanline)
            && !source.wipeReverse && (staticScanline || movingScanline);
        // Shaped scanlines use a solid palette and the forward line's own front.
        const bool scanlineShape = solidShape && (staticScanline || movingScanline);
        const bool lowerScanline = features.contains("p07_lower_ruby_scanline") && staticScanline
            && resolved.scanlineShape == "band" && resolved.scanlineMode != "role"
            && !source.wipeReverse && !source.zoomPulseEnabled && !source.strokeFlashEnabled && source.fxBursts.empty();
        // Horizontal band with a separately negotiated upper-annotation pass.
        if (source.scanlineEnabled
            && (!features.contains("p07_scanline_band")
                || (resolved.scanlineMode != "color" && scanlineVersion < 3)
                || (resolved.scanlineShape != "band" && !scanlineShape)
                || (resolved.scanlineGlowPx != 0 && scanlineVersion < 2)
                || cfg.vertical || cfg.rightToLeft || (source.wipeReverse && scanlineVersion < 4)
                || ((source.strokeFlashEnabled || source.zoomPulseEnabled)
                    && (!composedScanline || source.wipeReverse || resolved.scanlineMode == "role"))
                || source.karaokeAnimation == "no_wipe" || (utopiaSinging && !utopiaScanline)
                || (features.contains("p05_lower_ruby") && !lowerScanline)
                || (!rubyScanline && (!cfg.rubies.empty() || features.contains("p05_resolved_ruby")))
                || (rubyScanline && (source.wipeReverse || (resolved.scanlineMode == "role" && !roleLayers)
                    || !(staticScanline || movingScanline)
                    || (source.karaokeAnimation != "none" && !utopiaScanline)))))
            throw BackendError("Skia 扫字线当前仅支持横排实色高亮带");
        applyResolvedStyle(style, resolved, cfg.dpr);
        if (source.scanlineEnabled && style.scanlineMode == "role" && style.scanlineRolePaint.mode != "solid")
            throw BackendError("Skia 扫字线的角色取色暂仅支持实色");
        style.rightToLeft = cfg.rightToLeft;
        style.vertical = cfg.vertical;
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
        if (source.karaokeAnimation == "no_wipe"
            && (!features.contains("p07_no_wipe")
                || ir.value("skia_layout").toObject().value("no_wipe_schema").toDouble(-1) != 1))
            throw BackendError("Skia 整字变色缺少版本化能力声明");
        if (source.wipeReverse && (!features.contains("p05_reverse_wipe")
            || ir.value("skia_layout").toObject().value("reverse_wipe_schema").toDouble(-1) != 1))
            throw BackendError("Skia 反向走字缺少能力声明");
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
            if ((features.contains("p05_resolved_ruby") || !cfg.rubies.empty() || endpoints)
                && (!features.contains("p05_reverse_overlap")
                    || ir.value("skia_layout").toObject().value("reverse_overlap_schema").toDouble(-1) != 1))
                throw BackendError("Skia 反向走字的重叠终点或注音轨道缺少能力声明");
        }
        TextLine line;
        line.litHead = source.litHead;
        line.volumeHead = source.volumeHead;
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
        projectSkiaParticles(cfg, source, resolved, offset, twinkleVersion, features, out, line);
        if ((!source.displayStartMs || !source.displayEndMs) && !source.chars.empty())
            throw BackendError("Skia P04: Python-resolved display window is required");
        line.displayWindows.push_back({source.displayStartMs.value_or(0) + offset, source.displayEndMs.value_or(0) + offset, 0, 0});
        for (const auto& window : source.placementWindows)
            line.placementWindows.push_back({window.startMs + offset, window.endMs + offset,
                window.offsetX * float(cfg.dpr), window.offsetY * float(cfg.dpr)});
        const bool composedGuide = features.contains("p05_vector_guide_composition");
        bool sawLyrics = false;
        bool sawBitmapText = false;
        for (const auto& cell : source.chars) {
            if (composedGuide && sawLyrics && cell.vectorGlyph)
                throw BackendError("Skia 导唱符组合仅支持整行前置导唱符");
            sawLyrics = sawLyrics || !cell.vectorGlyph;
            if (cfg.vertical && &protocol::resolvedStyleForCharacter(cfg, source, cell) != &resolved)
                throw BackendError("Skia 竖排单元包含尚未支持的行内角色");
            if (cfg.rightToLeft && cell.text.toUcs4().size() > 1) {
                bool valid = features.contains("p05_horizontal_rtl_cells");
                char32_t previous = 0;
                for (const auto c : cell.text.toUcs4()) {
                    const bool voice = c == 0x3099 || c == 0x309a;
                    const bool base = (previous >= 0x3041 && previous <= 0x3096)
                        || (previous >= 0x30a1 && previous <= 0x30fa);
                    valid = valid && (voice
                        ? (base && features.contains("p05_horizontal_rtl_kana_marks"))
                        : (validatedRightToLeftScalar(c)
                            || (validatedRightToLeftKana(c) && features.contains("p05_horizontal_rtl_kana_cells"))));
                    previous = c;
                }
                if (!valid) throw BackendError("Skia 右至左多码点单元缺少版本化能力或包含未验证字符");
            }
            const auto& cellStyle = protocol::resolvedStyleForCharacter(cfg, source, cell);
            if (staticBand && (cell.vectorGlyph || cell.bitmapGuide || &cellStyle != &resolved))
                throw BackendError("Skia 行背景带暂不支持导唱符或行内角色");
            if (source.scanlineEnabled && (cell.vectorGlyph || &cellStyle != &resolved))
                throw BackendError("Skia 扫字线暂不支持导唱符或行内角色");
            if (source.strokeFlashEnabled && (cell.vectorGlyph || &cellStyle != &resolved))
                throw BackendError("Skia 唱字描边闪光暂不支持导唱符或行内角色");
            if (noise && (cell.vectorGlyph || &cellStyle != &resolved))
                throw BackendError("Skia 噪声出入场暂不支持导唱符或行内角色");
            if (!source.fxBursts.empty() && (cell.vectorGlyph || &cellStyle != &resolved))
                throw BackendError("Skia 唱字星光暂不支持导唱符或行内角色");
            if ((transformAnimation(source.entryAnimation, true) || transformAnimation(source.exitAnimation, false))
                && (cell.vectorGlyph || &cellStyle != &resolved))
                throw BackendError("Skia 逐字变换暂不支持导唱符或行内角色");
            // vector_guide_schema=1: solid horizontal LTR main text without roles. Ruby and
            // outlines need the verified composition profile.
            if (cell.vectorGlyph && (!features.contains("p05_vector_guides") || cfg.vertical || cfg.rightToLeft
                || &cellStyle != &resolved
                || ((!cfg.rubies.empty() || features.contains("p06_primary_solid_outline"))
                    && !composedGuide)))
                throw BackendError("Skia 导唱符仅支持已验证的横排实色正文子集");
            // bitmap_guide_schema=1: line-prefix image guides on horizontal forward main text.
            if (cell.bitmapGuide && (!features.contains("p08_bitmap_guides") || cell.vectorGlyph || cfg.vertical
                || cfg.rightToLeft || source.wipeReverse || &cellStyle != &resolved
                || sawBitmapText))
                throw BackendError("Skia 图片导唱符仅支持已验证的横排整行前置子集");
            sawBitmapText = sawBitmapText || !cell.bitmapGuide;
            if ((!cell.fallbackFamily.isEmpty() && !features.contains("p05_font_fallback"))
                || !cell.resolvedEndMs)
                throw BackendError("Skia 导唱符、未声明的缺字回退或未解析计时尚未支持");
            TextChar ch;
            if (&cellStyle != &resolved) {
                auto [entry, inserted] = roleStyles.try_emplace(&cellStyle, int(out.charStyles.size()));
                if (inserted) {
                    TextStyle roleStyle = style;
                    applyResolvedStyle(roleStyle, cellStyle, cfg.dpr);
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
