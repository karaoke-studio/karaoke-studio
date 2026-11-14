#include "skia_ir_projection.h"
#include "style_projection.h"
#include "line_layout_projection.h"
#include "../protocol/render_config_parser.h"
#include "../model/backend_caps.h"
#include <QtCore/QJsonArray>
#include <cmath>
#include <limits>
#include <map>

namespace krok::subtitle::native::scene {
namespace {
int integer(const QJsonObject& o, const char* key, int minimum, int maximum) {
    const auto v = o.value(QLatin1String(key));
    const double number = v.toDouble(std::numeric_limits<double>::quiet_NaN());
    if (!v.isDouble() || !std::isfinite(number) || std::floor(number) != number || number < minimum || number > maximum)
        throw BackendError(std::string("Skia IR: invalid ") + key);
    return int(number);
}
void validateEnvelope(const QJsonObject& ir) {
    if (integer(ir, "schema", 3, 3) != 3) throw BackendError("Skia requires IR schema 3");
    for (const auto& feature : ir.value("required_features").toArray()) {
        if (feature.toString() != "p04_ascii_solid_wipe" && feature.toString() != "p05_resolved_horizontal"
            && feature.toString() != "p05_single_line_placement" && feature.toString() != "p05_inline_roles"
            && feature.toString() != "p05_fixed_row_grid")
            throw BackendError("Skia IR: unknown required feature");
    }
    if (ir.contains("required_features") && !ir.value("required_features").isArray())
        throw BackendError("Skia IR: required_features must be an array");
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
    QString error;
    const auto parsed = protocol::parseRenderConfig(ir, &error);
    if (!parsed) throw BackendError(error.toStdString());
    const auto& cfg = *parsed;
    if (!cfg.rubies.empty() || cfg.lineBand.enabled || cfg.vertical || cfg.rightToLeft
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
        if (source.annotationBaselineY || source.wipeReverse
            || source.entryAnimation != "none" || source.exitAnimation != "none"
            || (source.karaokeAnimation != "none" && source.karaokeAnimation != "inherit")
            || source.scanlineEnabled || source.zoomPulseEnabled || source.strokeFlashEnabled
            || !source.fxBursts.empty() || !source.placementWindows.empty())
            throw BackendError("Skia P04: unsupported line feature");
        TextStyle style;
        const auto& resolved = protocol::resolvedStyleForLine(cfg, source);
        applyResolvedStyle(style, resolved, cfg.dpr);
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
        TextLine line;
        line.lane = source.lane;
        const int offset = cfg.timingOffsetMs + source.sourceOffsetMs;
        line.startMs = source.chars.empty() ? 0 : source.chars.front().startMs + offset;
        line.endMs = source.endMs + offset;
        if ((!source.displayStartMs || !source.displayEndMs) && !source.chars.empty())
            throw BackendError("Skia P04: Python-resolved display window is required");
        line.displayWindows.push_back({source.displayStartMs.value_or(0) + offset, source.displayEndMs.value_or(0) + offset, 0, 0});
        for (const auto& cell : source.chars) {
            const auto& cellStyle = protocol::resolvedStyleForCharacter(cfg, source, cell);
            if (cell.vectorGlyph || cell.bitmapGuide || !cell.fallbackFamily.isEmpty()
                || !cell.resolvedEndMs)
                throw BackendError("Skia 当前尚不支持导唱符、缺字回退或未解析计时");
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
