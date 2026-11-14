#include "skia_text_projection.h"
#include <QtCore/QCryptographicHash>
#include <QtCore/QFile>
#include <QtCore/QJsonArray>
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace krok::subtitle::native::scene {
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
std::shared_ptr<const Resource> loadFont(const QJsonObject& object) {
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
}
QJsonObject skiaCellMetricsJson(const ::subtitle::skia::text::ShapedCell& cell) {
    QJsonArray glyphs;
    for (const auto& g : cell.glyphs) glyphs.append(QJsonObject{{"id", int(g.id)}, {"x", g.position.x()},
        {"y", g.position.y()}, {"cluster_start", int(g.clusterStart)}, {"cluster_end", int(g.clusterEnd)}});
    return {{"font_identity", QString::fromStdString(cell.fontIdentity)}, {"selected_font", int(cell.selectedFont)},
        {"design_advance", cell.designAdvance}, {"design_left_bearing", cell.designLeftBearing},
        {"design_right_bearing", cell.designRightBearing}, {"advance", cell.advance}, {"ascent", cell.ascent}, {"descent", cell.descent}, {"leading", cell.leading},
        {"ink", QJsonArray{cell.ink.left(), cell.ink.top(), cell.ink.right(), cell.ink.bottom()}},
        {"glyphs", glyphs}, {"outline_available", cell.outlineAvailable}};
}
FontResourceTable loadSkiaFontResources(const QJsonObject& table) {
    FontResourceTable out;
    std::uint64_t total = 0;
    if (table.isEmpty() || table.size() > 32) throw std::runtime_error("Skia 字体表大小无效");
    for (auto it = table.begin(); it != table.end(); ++it) {
        if (!it.value().isObject()) throw std::runtime_error("Skia 字体资源必须为对象");
        auto loaded = loadFont(it.value().toObject());
        total += loaded->bytes.size();
        if (total > 256u * 1024 * 1024) throw std::runtime_error("Skia 字体表超过 256 MiB");
        out.emplace(it.key(), std::move(loaded));
    }
    return out;
}

std::vector<std::shared_ptr<const ::subtitle::skia::text::FontInstance>> skiaRunFonts(const QJsonObject& run, const FontResourceTable& resources) {
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
    if (fonts.empty() || fonts.size() > 32) throw std::runtime_error("Skia 显式字体链大小无效");
    return fonts;
}

}
