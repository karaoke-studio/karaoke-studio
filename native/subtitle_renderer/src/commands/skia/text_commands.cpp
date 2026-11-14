#include "text_commands.h"
#include "../../protocol/json_protocol.h"
#include "text/shaped_cell.h"
#include <QtCore/QCryptographicHash>
#include <QtCore/QFile>
#include <QtCore/QJsonArray>
#include <cmath>
#include <algorithm>
#include <map>
#include <stdexcept>

namespace krok::subtitle::native::commands {
namespace {
double number(const QJsonObject& object, const char* name, double fallback, double low, double high) {
    const auto v = object.value(QLatin1String(name));
    if (v.isUndefined()) return fallback;
    const double n = v.toDouble();
    if (!v.isDouble() || !std::isfinite(n) || n < low || n > high)
        throw std::runtime_error(std::string("Skia 度量参数无效：") + name);
    return n;
}
using Resource = ::subtitle::skia::text::FontResource;
using Instance = ::subtitle::skia::text::FontInstance;
std::shared_ptr<const Resource> resource(const QJsonObject& object) {
    QFile file(object.value("path").toString());
    if (!file.open(QIODevice::ReadOnly) || file.size() < 1 || file.size() > 64 * 1024 * 1024)
        throw std::runtime_error("Skia 字体文件缺失或大小超限");
    const auto bytes = file.readAll();
    if (bytes.size() != file.size()) throw std::runtime_error("Skia 字体文件读取不完整");
    const auto hash = QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex();
    if (object.contains("sha256") && object.value("sha256").toString().toLatin1() != hash)
        throw std::runtime_error("Skia 字体内容与请求的 SHA-256 不符");
    auto result = std::make_shared<Resource>();
    result->bytes.assign(bytes.begin(), bytes.end()); result->sha256 = hash.toStdString();
    const auto index = number(object, "face_index", 0, 0, 1024);
    if (std::floor(index) != index) throw std::runtime_error("Skia 字体 face index 必须为整数");
    result->faceIndex = int(index);
    if (object.contains("axes") && !object.value("axes").isObject()) throw std::runtime_error("Skia 字体轴必须为对象");
    const auto axes = object.value("axes").toObject();
    for (auto it = axes.begin(); it != axes.end(); ++it) {
        const auto tag = it.key().toLatin1();
        if (tag.size() != 4 || std::any_of(tag.begin(), tag.end(), [](auto c) { return c < 32 || c > 126; }))
            throw std::runtime_error("Skia 字体轴 tag 必须为四个 ASCII 字符");
        std::uint32_t value = 0;
        for (auto c : tag) value = (value << 8) | std::uint8_t(c);
        result->axes.push_back({value, float(number(axes, tag.constData(), 0, -1e6, 1e6))});
    }
    return result;
}
QJsonObject cellJson(const ::subtitle::skia::text::ShapedCell& cell) {
    QJsonArray glyphs;
    for (const auto& g : cell.glyphs) glyphs.append(QJsonObject{{"id", int(g.id)}, {"x", g.position.x()},
        {"y", g.position.y()}, {"cluster_start", int(g.clusterStart)}, {"cluster_end", int(g.clusterEnd)}});
    return {{"font_identity", QString::fromStdString(cell.fontIdentity)}, {"selected_font", int(cell.selectedFont)},
        {"advance", cell.advance}, {"ascent", cell.ascent}, {"descent", cell.descent}, {"leading", cell.leading},
        {"ink", QJsonArray{cell.ink.left(), cell.ink.top(), cell.ink.right(), cell.ink.bottom()}},
        {"glyphs", glyphs}, {"outline_available", cell.outlineAvailable}};
}
}
QJsonObject measureSkiaText(const QJsonObject& request) {
    if (request.value("metrics_schema").toInt() != 1 || !request.value("fonts").isObject() || !request.value("runs").isArray())
        throw std::runtime_error("Skia 批量度量 schema 或必需字段无效");
    const auto table = request.value("fonts").toObject();
    const auto runs = request.value("runs").toArray();
    if (table.isEmpty() || table.size() > 32 || runs.size() > 4096) throw std::runtime_error("Skia 度量批次大小超限");
    std::map<QString, std::shared_ptr<const Resource>> resources;
    std::uint64_t totalBytes = 0;
    for (auto it = table.begin(); it != table.end(); ++it) {
        if (!it.value().isObject()) throw std::runtime_error("Skia 字体描述必须为对象");
        auto loaded = resource(it.value().toObject()); totalBytes += loaded->bytes.size();
        if (totalBytes > 256u * 1024 * 1024) throw std::runtime_error("Skia 字体批次超过 256 MiB");
        resources.emplace(it.key(), std::move(loaded));
        protocol::emitProgress("skia_font_resources", resources.size(), table.size());
    }
    QJsonArray output;
    std::uint64_t totalCells = 0, totalTextBytes = 0;
    for (const auto& value : runs) totalCells += value.toObject().value("cells").toArray().size();
    if (totalCells > 65536) throw std::runtime_error("Skia 度量单元数量超限");
    std::uint64_t processedCells = 0;
    for (const auto& value : runs) {
        if (!value.isObject()) throw std::runtime_error("Skia 度量 run 必须为对象");
        const auto run = value.toObject();
        if (!run.value("cells").isArray() || !run.value("font_ids").isArray()) throw std::runtime_error("Skia 度量缺少单元或字体链");
        std::vector<std::shared_ptr<const Instance>> fonts;
        for (const auto& id : run.value("font_ids").toArray()) {
            const auto found = resources.find(id.toString());
            if (!id.isString() || found == resources.end()) throw std::runtime_error("Skia 度量引用未知字体资源");
            ::subtitle::skia::text::FontSpec spec;
            spec.resource = found->second;
            spec.size = float(number(run, "size", 48, .1, 2048));
            spec.scaleX = float(number(run, "scale_x", 1, .1, 8));
            for (const auto* flag : {"synthetic_bold", "synthetic_italic", "rtl"})
                if (run.contains(flag) && !run.value(flag).isBool()) throw std::runtime_error("Skia 字体/方向开关必须为布尔值");
            spec.syntheticBold = run.value("synthetic_bold").toBool();
            spec.syntheticItalic = run.value("synthetic_italic").toBool();
            fonts.push_back(std::make_shared<Instance>(spec));
        }
        QJsonArray cells;
        const auto inputCells = run.value("cells").toArray();
        for (const auto& cell : inputCells) {
            if (!cell.isString()) throw std::runtime_error("Skia 歌词单元必须为字符串");
            const auto text = cell.toString().toUtf8();
            totalTextBytes += text.size();
            if (totalTextBytes > 4u * 1024 * 1024) throw std::runtime_error("Skia 度量文本批次超过 4 MiB");
            cells.append(cellJson(::subtitle::skia::text::shapeCell(text.toStdString(), fonts,
                run.value("language").toString("und").toStdString(), run.value("rtl").toBool())));
            protocol::emitProgress("skia_text_metrics", ++processedCells, totalCells);
        }
        output.append(QJsonObject{{"cells", cells}});
    }
    auto out = protocol::response(true, "text_measured");
    out.insert("metrics_schema", 1); out.insert("backend", "skia"); out.insert("execution_target", "cpu_metrics");
    out.insert("gpu_initialized", false); out.insert("runs", output);
    return out;
}
}
