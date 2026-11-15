#include "resolved_wipe.h"
#include "../render_backend.h"
#include <QtCore/QJsonArray>
#include <cmath>

namespace krok::subtitle::native {
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
    // Sorted time/progress and endpoint invariants are also enforced by the
    // shared drawing core, including callers that do not originate in JSON.
    return out;
}
}
