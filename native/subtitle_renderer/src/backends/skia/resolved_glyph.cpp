#include "resolved_glyph.h"
#include "../render_backend.h"
#include "../../scene/skia_text_projection.h"
#include "include/core/SkMatrix.h"
#include <QtCore/QJsonArray>
#include <cmath>

namespace krok::subtitle::native {
namespace {
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
}
