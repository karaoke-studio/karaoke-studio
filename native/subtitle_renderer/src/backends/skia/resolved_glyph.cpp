#include "resolved_glyph.h"
#include "../render_backend.h"
#include "../../scene/skia_text_projection.h"
#include "include/core/SkMatrix.h"
#include "include/core/SkPathBuilder.h"
#include <QtCore/QJsonArray>
#include <cmath>

namespace krok::subtitle::native {
namespace {
bool near(const QJsonValue& value, double expected) {
    return value.isDouble() && std::isfinite(value.toDouble()) && std::abs(value.toDouble() - expected) <= 1e-3;
}
double finite(const QJsonObject& o, const char* key) {
    const auto v = o.value(QLatin1String(key));
    const double n = v.toDouble();
    if (!v.isDouble() || !std::isfinite(n) || std::abs(n) > 1e6)
        throw BackendError(std::string("Skia 最终几何无效：") + key);
    return n;
}
void same(const QJsonValue& expected, const QJsonValue& actual, const QString& key = {}) {
    bool valid = expected.type() == actual.type();
    if (valid && actual.isObject()) {
        const auto a = actual.toObject(), e = expected.toObject();
        if (a.keys() != e.keys()) valid = false;
        else for (auto it = a.begin(); it != a.end(); ++it) same(e.value(it.key()), it.value(), it.key());
    } else if (valid && actual.isArray()) {
        const auto a = actual.toArray(), e = expected.toArray();
        if (a.size() != e.size()) valid = false;
        else for (int i = 0; i < a.size(); ++i) same(e[i], a[i], key);
    } else if (valid && actual.isDouble()) {
        const bool discrete = key == "id" || key == "cluster_start" || key == "cluster_end" || key == "selected_font"
            || key == "font_index" || key == "byte_start" || key == "byte_end";
        valid = std::isfinite(expected.toDouble()) && std::isfinite(actual.toDouble())
            && std::abs(expected.toDouble() - actual.toDouble()) <= (discrete ? 0 : 1e-4);
    } else if (valid) valid = expected == actual;
    if (!valid) throw BackendError("Skia 最终 IR 的字体度量或 cluster 快照已失配");
}
}
::subtitle::skia::ResolvedCell ResolvedGlyphContext::cell(const QJsonObject& cell, const QString& sourceText,
    int start, int end, std::uint32_t before, std::uint32_t after, double wipePad) const {
    const auto runValue = cell.value("run_index");
    const auto runIndex = runValue.toDouble(-1);
    if (!runValue.isDouble() || !std::isfinite(runIndex) || std::floor(runIndex) != runIndex
        || runIndex < 0 || runIndex >= fonts.size())
        throw BackendError("Skia 最终单元引用了无效字体实例");
    const auto text = sourceText.toUtf8();
    auto shaped = ::subtitle::skia::text::shapeCell(text.toStdString(), fonts[int(runIndex)], languages[int(runIndex)],
                                                  false, mixedFallback[int(runIndex)]);
    same(cell.value("metrics"), scene::skiaCellMetricsJson(shaped));
    if (!shaped.outlineAvailable) throw BackendError("Skia 当前绘制仅支持有矢量轮廓的字体单元");
    const auto x = finite(cell, "origin_x"), y = finite(cell, "baseline_y");
    const auto left = finite(cell, "wipe_left"), right = finite(cell, "wipe_right");
    if (right < left || (!shaped.ink.isEmpty()
        && (std::abs(left - x - shaped.ink.left() + wipePad) > 1e-4
            || std::abs(right - x - shaped.ink.right() - wipePad) > 1e-4)))
        throw BackendError("Skia 最终走字边界与字形墨迹不一致");
    auto path = shaped.outline.makeTransform(SkMatrix::Translate(float(x), float(y)));
    path = path.makeTransform(SkMatrix::Scale(float(dpr), float(dpr)));
    return {path, float(left*dpr), float(right*dpr), start, end, before, after};
}
SkPath vectorGlyphPath(const VectorGlyph& glyph, double size) {
    const float scale = std::max(float(size), 1.0f) / std::max(glyph.unitsPerEm, 1.0f);
    SkPathBuilder builder(SkPathFillType::kWinding);
    bool open = false;
    for (const auto& command : glyph.commands) {
        const auto point = [&](std::size_t i) { return SkPoint::Make(command.values[i] * scale, command.values[i + 1] * scale); };
        if (command.kind == 'M' && command.values.size() == 2) { builder.moveTo(point(0)); open = true; }
        else if (command.kind == 'L' && command.values.size() == 2 && open) builder.lineTo(point(0));
        else if (command.kind == 'C' && command.values.size() == 6 && open) builder.cubicTo(point(0), point(2), point(4));
        else if (command.kind == 'Q' && command.values.size() == 4 && open) builder.quadTo(point(0), point(2));
        else if (command.kind == 'Z' && open) { builder.close(); open = false; }
    }
    SkPath path = builder.detach();
    if (!path.isFinite()) throw BackendError("Skia 导唱符轮廓包含非有限坐标");
    return path;
}
// The existing GPU builds the guide from M/L/C/Q/Z commands scaled by
// max(size, 1) / unitsPerEm (y-down, baseline 0, winding fill, open figures
// filled), translates it to the cell's left edge and advances by
// max(size * advanceWidth / unitsPerEm, 1). Advance and ink are re-derived here.
::subtitle::skia::ResolvedCell ResolvedGlyphContext::vectorCell(const QJsonObject& cell, const VectorGlyph& glyph,
                                                              const QString& glyphId, double size,
                                                              int start, int end, std::uint32_t before, std::uint32_t after) const {
    if (!std::isfinite(size) || size <= 0 || size > 4096 || glyph.commands.empty() || glyph.commands.size() > 65536)
        throw BackendError("Skia 导唱符尺寸或轮廓无效");
    const SkPath path = vectorGlyphPath(glyph, size);
    const SkRect ink = path.isEmpty() ? SkRect::MakeEmpty() : path.computeTightBounds();
    const double advance = std::max(size * std::max(glyph.advanceWidth, 0.0f) / std::max(glyph.unitsPerEm, 1.0f), 1.0);
    const auto metrics = cell.value("metrics").toObject();
    const auto inkValue = metrics.value("ink").toArray();
    if (glyphId.isEmpty() || metrics.value("vector_glyph_id").toString() != glyphId
        || metrics.value("font_identity").toString() != QStringLiteral("vector:") + glyphId
        || !metrics.value("glyphs").isArray()
        || !metrics.value("glyphs").toArray().isEmpty() || !near(metrics.value("advance"), advance) || inkValue.size() != 4
        || !near(inkValue[0], ink.left()) || !near(inkValue[1], ink.top())
        || !near(inkValue[2], ink.right()) || !near(inkValue[3], ink.bottom()))
        throw BackendError("Skia 导唱符度量快照与轮廓不一致");
    const auto x = finite(cell, "origin_x"), y = finite(cell, "baseline_y");
    const auto left = finite(cell, "wipe_left"), right = finite(cell, "wipe_right");
    if (right < left || (!ink.isEmpty() && (std::abs(left - x - ink.left()) > 1e-3 || std::abs(right - x - ink.right()) > 1e-3)))
        throw BackendError("Skia 导唱符走字边界与轮廓墨迹不一致");
    auto positioned = path.makeTransform(SkMatrix::Translate(float(x), float(y)));
    positioned = positioned.makeTransform(SkMatrix::Scale(float(dpr), float(dpr)));
    return {positioned, float(left * dpr), float(right * dpr), start, end, before, after};
}
}
