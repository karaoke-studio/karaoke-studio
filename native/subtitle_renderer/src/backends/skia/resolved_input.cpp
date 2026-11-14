#include "resolved_input.h"
#include "scene_input.h"
#include "../render_backend.h"
#include "../../scene/skia_ir_projection.h"
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
        const bool discrete = key == "id" || key == "cluster_start" || key == "cluster_end" || key == "selected_font";
        valid = std::isfinite(expected.toDouble()) && std::isfinite(actual.toDouble())
            && std::abs(expected.toDouble() - actual.toDouble()) <= (discrete ? 0 : 1e-4);
    } else if (valid) valid = expected == actual;
    if (!valid) throw BackendError("Skia 最终 IR 的字体度量或 cluster 快照已失配");
}
std::uint32_t argb(const RgbaColor& c) {
    return (std::uint32_t(c.alpha) << 24) | (std::uint32_t(c.red) << 16) | (std::uint32_t(c.green) << 8) | c.blue;
}
}
::subtitle::skia::ResolvedSubtitleInput resolvedSkiaInput(const QJsonObject& ir) {
    auto projected = scene::skiaSceneFromIr(ir);
    validateSkiaScene(projected, true);
    const auto layout = ir.value("skia_layout").toObject();
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
    using Fonts = std::vector<std::shared_ptr<const ::subtitle::skia::text::FontInstance>>;
    std::vector<Fonts> runFonts;
    std::vector<std::string> languages;
    for (const auto& value : runs) {
        if (!value.isObject()) throw BackendError("Skia 字体实例必须为对象");
        const auto run = value.toObject();
        if (run.value("rtl").toBool()) throw BackendError("Skia 当前规范布局尚不支持 RTL");
        runFonts.push_back(scene::skiaRunFonts(run, resources));
        languages.push_back(run.value("language").toString("und").toStdString());
    }
    const double dpr = ir.value("screen").toObject().value("dpr").toDouble(1);
    ::subtitle::skia::ResolvedSubtitleInput out{projected.width, projected.height, {}};
    std::size_t totalCells = 0;
    for (int index = 0; index < lines.size(); ++index) {
        const auto source = lines[index].toObject();
        const auto& line = projected.lines[index];
        const auto& style = projected.lineStyles[index];
        if (!source.value("cells").isArray()) throw BackendError("Skia 最终布局缺少单元");
        const auto cells = source.value("cells").toArray();
        const auto intervals = originalLines[index].toObject().value("resolved_intervals").toArray();
        if (cells.size() != int(line.chars.size()) || intervals.size() != cells.size() || (totalCells += cells.size()) > 65536)
            throw BackendError("Skia 最终布局与解析计时单元不一致或超限");
        ::subtitle::skia::ResolvedLine prepared{{}, line.displayWindows[0].startMs, line.displayWindows[0].endMs};
        for (const auto& window : line.placementWindows)
            prepared.translations.push_back({window.startMs, window.endMs, window.offsetX, window.offsetY});
        for (int j = 0; j < cells.size(); ++j) {
            const auto cell = cells[j].toObject();
            const auto runValue = cell.value("run_index");
            const auto runIndex = runValue.toDouble(-1);
            if (!runValue.isDouble() || !std::isfinite(runIndex) || std::floor(runIndex) != runIndex
                || runIndex < 0 || runIndex >= runs.size())
                throw BackendError("Skia 最终单元引用了无效字体实例");
            const auto interval = intervals[j].toArray();
            if (interval.size() != 2 || !interval[0].isDouble() || !interval[1].isDouble()
                || !std::isfinite(interval[0].toDouble()) || !std::isfinite(interval[1].toDouble())
                || std::floor(interval[0].toDouble()) != interval[0].toDouble()
                || std::floor(interval[1].toDouble()) != interval[1].toDouble()
                || interval[0].toDouble() < -86400000 || interval[1].toDouble() > 86400000
                || interval[1].toDouble() < interval[0].toDouble())
                throw BackendError("Skia 最终 IR 必须提供有效且完整的解析计时区间");
            const auto text = QString::fromStdWString(line.chars[j].text).toUtf8();
            auto shaped = ::subtitle::skia::text::shapeCell(text.toStdString(), runFonts[int(runIndex)], languages[int(runIndex)]);
            same(cell.value("metrics"), scene::skiaCellMetricsJson(shaped));
            if (!shaped.outlineAvailable) throw BackendError("Skia 当前绘制仅支持有矢量轮廓的字体单元");
            const auto x = finite(cell, "origin_x"), y = finite(cell, "baseline_y");
            const auto left = finite(cell, "wipe_left"), right = finite(cell, "wipe_right");
            if (right < left || (!shaped.ink.isEmpty()
                && (std::abs(left - x - shaped.ink.left()) > 1e-4 || std::abs(right - x - shaped.ink.right()) > 1e-4)))
                throw BackendError("Skia 最终走字边界与字形墨迹不一致");
            auto path = shaped.outline.makeTransform(SkMatrix::Translate(float(x), float(y)));
            path = path.makeTransform(SkMatrix::Scale(float(dpr), float(dpr)));
            const auto& paint = line.chars[j].styleIndex >= 0 ? projected.charStyles[line.chars[j].styleIndex] : style;
            prepared.cells.push_back({path, float(left * dpr), float(right * dpr), line.chars[j].startMs, line.chars[j].endMs,
                argb(paint.beforeFillPaint.color), argb(paint.afterFillPaint.color)});
        }
        out.lines.push_back(std::move(prepared));
    }
    return out;
}
}
