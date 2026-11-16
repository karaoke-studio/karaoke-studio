#include "resolved_wipe.h"
#include "../render_backend.h"
#include <QtCore/QJsonArray>
#include <cmath>

namespace krok::subtitle::native {
std::optional<float> resolvedSkiaWipeEnd(const QJsonObject& cell, const QJsonObject& ir) {
    if (!cell.contains("wipe_end_position")) return {};
    const auto value = cell.value("wipe_end_position");
    const double end = value.toDouble(-1);
    if (!ir.value("required_features").toArray().contains("p05_overlap_wipe")
        || ir.value("skia_layout").toObject().value("overlap_schema").toDouble(-1) != 1
        || !value.isDouble() || !std::isfinite(end) || end < 0 || end > 1)
        throw BackendError("Skia 重叠走字终点缺少能力声明或包含无效值");
    return float(end);
}
std::vector<WipePoint> resolvedSkiaWipePoints(const QJsonObject& cell, const QJsonObject& ir,
                                            int timingOffset, std::size_t& totalPoints) {
    if (!cell.contains("wipe_points")) return {};
    if (!ir.value("required_features").toArray().contains("p05_segmented_wipe")
        || ir.value("skia_layout").toObject().value("wipe_schema").toDouble(-1) != 1)
        throw BackendError("Skia 分段走字缺少版本或能力声明");
    const auto value = cell.value("wipe_points");
    if (!value.isArray() || value.toArray().size() < 2 || value.toArray().size() > 4096
        || (totalPoints += value.toArray().size()) > 1048576)
        throw BackendError("Skia 分段走字数量或类型无效");
    std::vector<WipePoint> out;
    for (const auto& point : value.toArray()) {
        if (!point.isObject()) throw BackendError("Skia 分段走字节点必须为对象");
        const auto object = point.toObject();
        const auto time = object.value("time_ms"), position = object.value("position");
        const double t = time.toDouble(), p = position.toDouble();
        const double shifted = t + timingOffset;
        if (!time.isDouble() || !std::isfinite(t) || std::floor(t) != t || std::abs(t) > 86400000
            || std::abs(shifted) > 172800000 || !position.isDouble() || !std::isfinite(p) || p < 0 || p > 1)
            throw BackendError("Skia 分段走字节点包含无效或非有限值");
        out.push_back({int(shifted), float(p)});
    }
    // An IR curve always spans [0, 1]. The GPU overlap endpoint is a separate
    // field applied afterwards, so it must never launder an invalid curve end.
    if (out.front().position != 0 || out.back().position != 1)
        throw BackendError("Skia 分段走字端点无效");
    // Sorted time/progress invariants are also enforced by the shared drawing
    // core, including callers that do not originate in JSON.
    return out;
}
}
