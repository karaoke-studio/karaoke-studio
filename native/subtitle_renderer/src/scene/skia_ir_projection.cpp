#include "skia_ir_projection.h"
#include "skia_lower_projection.h"
#include "style_projection.h"
#include "line_layout_projection.h"
#include "../protocol/render_config_parser.h"
#include "../model/backend_caps.h"
#include <QtCore/QJsonArray>
#include <cmath>
#include <limits>
#include <map>
#include <algorithm>

namespace krok::subtitle::native::scene {
namespace {
int integer(const QJsonObject& o, const char* key, int minimum, int maximum) {
    const auto v = o.value(QLatin1String(key));
    const double number = v.toDouble(std::numeric_limits<double>::quiet_NaN());
    if (!v.isDouble() || !std::isfinite(number) || std::floor(number) != number || number < minimum || number > maximum)
        throw BackendError(std::string("Skia IR: invalid ") + key);
    return int(number);
}
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
void validateEnvelope(const QJsonObject& ir) {
    if (integer(ir, "schema", 3, 3) != 3) throw BackendError("Skia requires IR schema 3");
    for (const auto& feature : ir.value("required_features").toArray()) {
        if (feature.toString() != "p04_ascii_solid_wipe" && feature.toString() != "p05_resolved_horizontal"
            && feature.toString() != "p05_single_line_placement" && feature.toString() != "p05_inline_roles"
            && feature.toString() != "p05_fixed_row_grid" && feature.toString() != "p05_page_collisions"
            && feature.toString() != "p05_smart_horizontal" && feature.toString() != "p05_segmented_wipe"
            && feature.toString() != "p05_resolved_ruby" && feature.toString() != "p05_upper_ruby"
            && feature.toString() != "p05_overlap_wipe" && feature.toString() != "p05_lower_ruby"
            && feature.toString() != "p05_ruby_markers" && feature.toString() != "p05_ruby_units"
            && feature.toString() != "p05_font_fallback" && feature.toString() != "p05_mixed_fonts"
            && feature.toString() != "p05_standalone_marks" && feature.toString() != "p07_no_wipe"
            && feature.toString() != "p07_zero_ruby_no_wipe"
            && feature.toString() != "p05_horizontal_rtl" && feature.toString() != "p05_horizontal_rtl_cells"
            && feature.toString() != "p05_horizontal_rtl_kana_cells" && feature.toString() != "p05_vertical_solid"
            && feature.toString() != "p05_vertical_japanese_glyphs"
            && feature.toString() != "p06_primary_solid_outline"
            && feature.toString() != "p05_horizontal_rtl_kana_marks"
            && feature.toString() != "p05_reverse_wipe" && feature.toString() != "p05_reverse_overlap")
            throw BackendError("Skia IR: unknown required feature");
    }
    if (ir.contains("required_features") && !ir.value("required_features").isArray())
        throw BackendError("Skia IR: required_features must be an array");
    if (ir.value("required_features").toArray().contains("p05_overlap_wipe")
        && ir.value("skia_layout").toObject().value("overlap_schema").toDouble(-1) != 1)
        throw BackendError("Skia 重叠走字布局版本无效");
    if (ir.value("required_features").toArray().contains("p05_ruby_units")
        && ir.value("skia_layout").toObject().value("ruby_unit_schema").toDouble(-1) != 1)
        throw BackendError("Skia 注音计时单元布局版本无效");
    if (ir.value("required_features").toArray().contains("p05_font_fallback")
        && ir.value("skia_layout").toObject().value("font_chain_schema").toDouble(-1) != 1)
        throw BackendError("Skia 字体链布局版本无效");
    if (ir.value("required_features").toArray().contains("p05_mixed_fonts")
        && ir.value("skia_layout").toObject().value("mixed_font_schema").toDouble(-1) != 1)
        throw BackendError("Skia 混合字体布局版本无效");
    if (ir.value("required_features").toArray().contains("p05_standalone_marks")
        && ir.value("skia_layout").toObject().value("standalone_mark_schema").toDouble(-1) != 1)
        throw BackendError("Skia 独立组合标记布局版本无效");
    if (ir.value("required_features").toArray().contains("p07_no_wipe")
        && ir.value("skia_layout").toObject().value("no_wipe_schema").toDouble(-1) != 1)
        throw BackendError("Skia 整字变色布局版本无效");
    if (ir.value("required_features").toArray().contains("p07_zero_ruby_no_wipe")
        && (ir.value("skia_layout").toObject().value("zero_ruby_no_wipe_schema").toDouble(-1) != 1
            || !ir.value("required_features").toArray().contains("p07_no_wipe")))
        throw BackendError("Skia 零时长注音整字变色缺少版本化能力声明");
    if (ir.value("required_features").toArray().contains("p05_horizontal_rtl")
        && ir.value("skia_layout").toObject().value("horizontal_rtl_schema").toDouble(-1) != 1)
        throw BackendError("Skia 右至左单元排列布局版本无效");
    if (ir.value("required_features").toArray().contains("p05_horizontal_rtl_cells")
        && (ir.value("skia_layout").toObject().value("horizontal_rtl_cell_schema").toDouble(-1) != 1
            || !ir.value("required_features").toArray().contains("p05_horizontal_rtl")))
        throw BackendError("Skia 右至左多码点单元布局版本无效");
    if (ir.value("required_features").toArray().contains("p05_horizontal_rtl_kana_cells")
        && (ir.value("skia_layout").toObject().value("horizontal_rtl_kana_schema").toDouble(-1) != 1
            || !ir.value("required_features").toArray().contains("p05_horizontal_rtl_cells")))
        throw BackendError("Skia 右至左假名单元布局版本无效");
    if (ir.value("required_features").toArray().contains("p05_horizontal_rtl_kana_marks")
        && (ir.value("skia_layout").toObject().value("kana_mark_schema").toDouble(-1) != 1
            || !ir.value("required_features").toArray().contains("p05_horizontal_rtl_kana_cells")))
        throw BackendError("Skia 右至左假名浊音布局版本无效");
    if (ir.value("required_features").toArray().contains("p05_vertical_solid")) {
        const auto version = ir.value("skia_layout").toObject().value("vertical_solid_schema").toDouble(-1);
        if (version != 1 && version != 2)
            throw BackendError("Skia 竖排实色正文布局版本无效");
    }
    if (ir.value("required_features").toArray().contains("p05_vertical_japanese_glyphs")
        && (ir.value("skia_layout").toObject().value("vertical_japanese_glyph_schema").toDouble(-1) != 1
            || !ir.value("required_features").toArray().contains("p05_vertical_solid")
            || !ir.value("style").toObject().value("vertical").toBool()))
        throw BackendError("Skia 竖排日文字形布局版本无效");
    if (ir.value("required_features").toArray().contains("p06_primary_solid_outline")
        && ir.value("skia_layout").toObject().value("primary_solid_outline_schema").toDouble(-1) != 1)
        throw BackendError("Skia 实色描边布局版本无效");
    if (ir.value("style").toObject().contains("right_to_left")
        && !ir.value("style").toObject().value("right_to_left").isBool())
        throw BackendError("Skia 右至左单元排列标记必须是布尔值");
    if (ir.value("required_features").toArray().contains("p05_reverse_wipe")
        && ir.value("skia_layout").toObject().value("reverse_wipe_schema").toDouble(-1) != 1)
        throw BackendError("Skia 反向走字布局版本无效");
    if (ir.value("required_features").toArray().contains("p05_reverse_overlap")
        && (ir.value("skia_layout").toObject().value("reverse_overlap_schema").toDouble(-1) != 1
            || !ir.value("required_features").toArray().contains("p05_reverse_wipe")))
        throw BackendError("Skia 反向走字重叠终点布局版本无效");
    if (!ir.value("screen").isObject() || !ir.value("style").isObject() || !ir.value("track").isObject())
        throw BackendError("Skia IR: screen/style/track objects are required");
    const auto screen = ir.value("screen").toObject();
    integer(screen, "width", 1, 8192);
    integer(screen, "height", 1, 8192);
    integer(screen, "fps", 1, 240);
    const double dpr = screen.value("dpr").toDouble(1);
    if (!std::isfinite(dpr) || dpr < 0.1 || dpr > 8
        || screen.value("width").toDouble() * dpr > 8192 || screen.value("height").toDouble() * dpr > 8192)
        throw BackendError("Skia IR: invalid DPR/physical size");
    if (!ir.value("extra_tracks").toArray().isEmpty() || !ir.value("titles").toArray().isEmpty())
        throw BackendError("Skia P04: extra tracks and titles require later phases");
    const auto track = ir.value("track").toObject();
    if (!track.value("lines").isArray()) throw BackendError("Skia IR: track.lines is required");
    for (const auto& value : track.value("lines").toArray()) {
        if (!value.isObject()) throw BackendError("Skia IR: invalid line");
        const auto line = value.toObject();
        if (line.contains("lane")) integer(line, "lane", 0, 31);
        if (line.contains("wipe_reverse") && !line.value("wipe_reverse").isBool())
            throw BackendError("Skia 反向走字标记必须是布尔值");
        if (line.contains("layout_offset_windows")) {
            const auto windows = line.value("layout_offset_windows");
            if (!windows.isArray() || windows.toArray().size() > 4096)
                throw BackendError("Skia 页面位移窗口数量或类型无效");
            int previousEnd = -86400000;
            for (const auto& window : windows.toArray()) {
                if (!window.isObject()) throw BackendError("Skia 页面位移窗口必须是对象");
                const auto object = window.toObject();
                const auto start = integer(object, "start_ms", -86400000, 86400000);
                const auto end = integer(object, "end_ms", -86400000, 86400000);
                if (end <= start || start < previousEnd) throw BackendError("Skia 页面位移窗口无效或重叠");
                previousEnd = end;
                for (const auto key : {"offset_x", "offset_y"}) {
                    const auto value = object.value(QLatin1String(key));
                    if (!value.isDouble() || !std::isfinite(value.toDouble()) || std::abs(value.toDouble()) > 1e6)
                        throw BackendError("Skia 页面位移包含无效或非有限值");
                }
            }
        }
        if (!line.value("chars").isArray()) throw BackendError("Skia IR: line.chars is required");
        for (const auto& cv : line.value("chars").toArray()) {
            if (!cv.isObject() || !cv.toObject().value("text").isString()) throw BackendError("Skia IR: invalid character");
            integer(cv.toObject(), "start_ms", -86400000, 86400000);
            for (const auto* key : {"vector_glyph_id", "vector_glyph", "bitmap_guide", "wipe_points"})
                if (cv.toObject().contains(QLatin1String(key)))
                    throw BackendError("Skia P04: guide symbols and custom wipe curves require later phases");
        }
    }
}
}
RenderScene skiaSceneFromIr(const QJsonObject& ir) {
    validateEnvelope(ir);
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
    // vertical_solid_schema=1: fixed-cell columns without annotations or role geometry.
    if (cfg.vertical && (!features.contains("p05_vertical_solid") || cfg.rightToLeft || !cfg.rubies.empty()
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
    for (const auto& source : cfg.lines) {
        if (source.annotationBaselineY
            || source.entryAnimation != "none" || source.exitAnimation != "none"
            || (source.karaokeAnimation != "none" && source.karaokeAnimation != "inherit"
                && source.karaokeAnimation != "no_wipe")
            || source.scanlineEnabled || source.zoomPulseEnabled || source.strokeFlashEnabled
            || !source.fxBursts.empty())
            throw BackendError("Skia P04: unsupported line feature");
        TextStyle style;
        const auto& resolved = protocol::resolvedStyleForLine(cfg, source);
        applyResolvedStyle(style, resolved, cfg.dpr);
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
        line.karaokeAnimation = source.karaokeAnimation.toStdString();
        line.lane = source.lane;
        const int offset = cfg.timingOffsetMs + source.sourceOffsetMs;
        line.startMs = source.chars.empty() ? 0 : source.chars.front().startMs + offset;
        line.endMs = source.endMs + offset;
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
            if (cell.vectorGlyph || cell.bitmapGuide
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
