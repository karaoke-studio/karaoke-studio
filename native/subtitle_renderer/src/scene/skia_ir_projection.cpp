#include "skia_ir_projection.h"
#include "skia_ir_contract.h"
#include "skia_lower_projection.h"
#include "style_projection.h"
#include "line_layout_projection.h"
#include "skia_particle_projection.h"
#include "../protocol/render_config_parser.h"
#include "../model/backend_caps.h"
#include <QtCore/QJsonArray>
#include <map>
#include <algorithm>

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
        || cfg.dualLineLayout || std::any_of(cfg.lineOffsetsX.begin(), cfg.lineOffsetsX.end(), [](int v) { return v != 0; })))
        throw BackendError("Skia 竖排仅支持已验证的单列实色正文");
    if (features.contains("p06_primary_solid_outline")
        && (cfg.vertical || cfg.rightToLeft || !cfg.rubies.empty() || cfg.dualLineLayout
            || features.contains("p05_resolved_ruby") || features.contains("p05_reverse_wipe")))
        throw BackendError("Skia 实色描边当前仅支持从左至右的基础横排正文");
    if (cfg.lineBand.enabled
        || cfg.viewportScalePct != 100 || cfg.viewportRotationDeg || cfg.viewportOffsetX || cfg.viewportOffsetY)
        throw BackendError("Skia P04: annotations, line bands and viewport transforms require later phases");
    RenderScene out;
    out.width = cfg.physicalWidth();
    out.height = cfg.physicalHeight();
    out.layoutReferenceScale = float(cfg.dpr);
    // Pointers refer to the immutable parser cache for this projection only.
    // Materialize a role once, rather than copying a full style into each cell.
    std::map<const protocol::ResolvedStyle*, int> roleStyles;
    const bool allowFade = features.contains("p07_line_fade");
    const bool allowMotion = features.contains("p07_line_motion");
    const bool allowCharacterFade = features.contains("p07_character_fade");
    const bool allowCharacterTransform = features.contains("p07_character_transform");
    const int transformVersion = ir.value("skia_layout").toObject().value("character_transform_schema").toInt();
    const bool allowGeometry = allowCharacterTransform && transformVersion >= 4;
    const bool allowHalo = allowGeometry && transformVersion >= 5;
    const int twinkleVersion = ir.value("skia_layout").toObject().value("sing_twinkle_schema").toInt(1);
    const auto scanlineVersion = ir.value("skia_layout").toObject().value("scanline_band_schema").toDouble();
    const auto transformAnimation = [=](const QString& animation, bool entry) {
        return animation == "char_drip" || animation == "spin_flip"
            || (allowGeometry && (entry ? animation == "tracking_in" || animation == "wave_in"
                                       : animation == "scatter_out" || animation == "converge_out"))
            || (allowHalo && (entry ? animation == "stretch_in" || animation == "glow_in"
                                    : animation == "stretch_out" || animation == "glow_out"));
    };
    const auto supportedAnimation = [=](const QString& animation, bool entry) {
        return animation == "none" || (allowFade && animation == "fade")
            || (allowMotion && (animation == (entry ? "slide_in" : "slide_out") || animation == "rise"))
            || (allowCharacterFade && animation == "char_fade")
            || (allowCharacterTransform && transformAnimation(animation, entry));
    };
    for (const auto& source : cfg.lines) {
        if (source.annotationBaselineY
            || !supportedAnimation(source.entryAnimation, true)
            || !supportedAnimation(source.exitAnimation, false)
            || (source.karaokeAnimation != "none" && source.karaokeAnimation != "inherit"
                && source.karaokeAnimation != "no_wipe")
            || source.zoomPulseEnabled
            || (!source.fxBursts.empty() && !features.contains("p07_sing_twinkle")))
            throw BackendError("Skia P04: unsupported line feature");
        TextStyle style;
        const auto& resolved = protocol::resolvedStyleForLine(cfg, source);
        if (source.strokeFlashEnabled
            && (!features.contains("p07_stroke_flash") || cfg.vertical || cfg.rightToLeft || source.wipeReverse
                || !cfg.rubies.empty() || features.contains("p05_resolved_ruby") || source.scanlineEnabled
                || source.entryAnimation != "none" || source.exitAnimation != "none"
                || source.karaokeAnimation == "no_wipe" || !source.fxBursts.empty()))
            throw BackendError("Skia 唱字描边闪光当前仅支持正向横排实色正文");
        // Horizontal solid-color band, without image, ruby or roles.
        if (source.scanlineEnabled
            && (!features.contains("p07_scanline_band")
                || (resolved.scanlineMode != "color" && scanlineVersion < 3)
                || resolved.scanlineShape != "band" || (resolved.scanlineGlowPx != 0 && scanlineVersion < 2)
                || cfg.vertical || cfg.rightToLeft || (source.wipeReverse && scanlineVersion < 4)
                || source.karaokeAnimation == "no_wipe" || !cfg.rubies.empty() || features.contains("p05_resolved_ruby")))
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
        line.wipeReverse = source.wipeReverse;
        line.entryAnimation = source.entryAnimation.toStdString();
        line.entryDurationMs = source.entryDurationMs;
        line.exitAnimation = source.exitAnimation.toStdString();
        line.exitDurationMs = source.exitDurationMs;
        line.karaokeAnimation = source.karaokeAnimation.toStdString();
        line.scanlineEnabled = source.scanlineEnabled;
        line.strokeFlashEnabled = source.strokeFlashEnabled;
        line.lane = source.lane;
        const int offset = cfg.timingOffsetMs + source.sourceOffsetMs;
        line.startMs = source.chars.empty() ? 0 : source.chars.front().startMs + offset;
        line.endMs = source.endMs + offset;
        projectSkiaParticles(cfg, source, resolved, offset, twinkleVersion, out, line);
        if ((!source.displayStartMs || !source.displayEndMs) && !source.chars.empty())
            throw BackendError("Skia P04: Python-resolved display window is required");
        line.displayWindows.push_back({source.displayStartMs.value_or(0) + offset, source.displayEndMs.value_or(0) + offset, 0, 0});
        for (const auto& window : source.placementWindows)
            line.placementWindows.push_back({window.startMs + offset, window.endMs + offset,
                window.offsetX * float(cfg.dpr), window.offsetY * float(cfg.dpr)});
        for (const auto& cell : source.chars) {
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
            if (source.scanlineEnabled && (cell.vectorGlyph || &cellStyle != &resolved))
                throw BackendError("Skia 扫字线暂不支持导唱符或行内角色");
            if (source.strokeFlashEnabled && (cell.vectorGlyph || &cellStyle != &resolved))
                throw BackendError("Skia 唱字描边闪光暂不支持导唱符或行内角色");
            if (!source.fxBursts.empty() && (cell.vectorGlyph || &cellStyle != &resolved))
                throw BackendError("Skia 唱字星光暂不支持导唱符或行内角色");
            if ((transformAnimation(source.entryAnimation, true) || transformAnimation(source.exitAnimation, false))
                && (cell.vectorGlyph || &cellStyle != &resolved))
                throw BackendError("Skia 逐字变换暂不支持导唱符或行内角色");
            // vector_guide_schema=1: solid horizontal LTR main text, no roles, ruby or outlines.
            if (cell.vectorGlyph && (!features.contains("p05_vector_guides") || cfg.vertical || cfg.rightToLeft
                || !cfg.rubies.empty() || &cellStyle != &resolved || features.contains("p06_primary_solid_outline")))
                throw BackendError("Skia 导唱符仅支持已验证的横排实色正文子集");
            if (cell.bitmapGuide
                || (!cell.fallbackFamily.isEmpty() && !features.contains("p05_font_fallback"))
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
