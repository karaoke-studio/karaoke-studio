#include "text_commands.h"
#include "../../scene/skia_text_projection.h"
#include "../../protocol/json_protocol.h"
#include <QtCore/QJsonArray>
#include <stdexcept>

namespace krok::subtitle::native::commands {
QJsonObject measureSkiaText(const QJsonObject& request) {
    if (request.value("metrics_schema").toInt() != 1 || !request.value("fonts").isObject() || !request.value("runs").isArray())
        throw std::runtime_error("Skia 批量度量 schema 或必需字段无效");
    const auto table = request.value("fonts").toObject();
    const auto runs = request.value("runs").toArray();
    if (table.isEmpty() || table.size() > 32 || runs.size() > 4096) throw std::runtime_error("Skia 度量批次大小超限");
    const auto resources = scene::loadSkiaFontResources(table);
    protocol::emitProgress("skia_font_resources", resources.size(), table.size());
    QJsonArray output;
    std::uint64_t totalCells = 0, totalTextBytes = 0;
    for (const auto& value : runs) totalCells += value.toObject().value("cells").toArray().size();
    if (totalCells > 65536) throw std::runtime_error("Skia 度量单元数量超限");
    std::uint64_t processedCells = 0;
    for (const auto& value : runs) {
        if (!value.isObject()) throw std::runtime_error("Skia 度量 run 必须为对象");
        const auto run = value.toObject();
        if (!run.value("cells").isArray() || !run.value("font_ids").isArray()) throw std::runtime_error("Skia 度量缺少单元或字体链");
        const auto fonts = scene::skiaRunFonts(run, resources);
        QJsonArray cells;
        const auto inputCells = run.value("cells").toArray();
        for (const auto& cell : inputCells) {
            if (!cell.isString()) throw std::runtime_error("Skia 歌词单元必须为字符串");
            const auto text = cell.toString().toUtf8();
            totalTextBytes += text.size();
            if (totalTextBytes > 4u * 1024 * 1024) throw std::runtime_error("Skia 度量文本批次超过 4 MiB");
            cells.append(scene::skiaCellMetricsJson(::subtitle::skia::text::shapeCell(text.toStdString(), fonts,
                run.value("language").toString("und").toStdString(), run.value("rtl").toBool())));
            protocol::emitProgress("skia_text_metrics", ++processedCells, totalCells);
        }
        const auto& primary = *fonts.front();
        output.append(QJsonObject{{"cells", cells}, {"font_metrics", QJsonObject{
            {"font_identity", QString::fromStdString(primary.identity())},
            {"ascent", primary.ascent()}, {"descent", primary.descent()}, {"leading", primary.leading()}}}});
    }
    auto out = protocol::response(true, "text_measured");
    out.insert("metrics_schema", 1); out.insert("backend", "skia"); out.insert("execution_target", "cpu_metrics");
    out.insert("gpu_initialized", false); out.insert("runs", output);
    return out;
}
}
