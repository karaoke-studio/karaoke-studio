#include "skia_lower_projection.h"
#include "../model/backend_caps.h"
#include <QtCore/QJsonArray>
#include <cmath>
#include <set>

namespace krok::subtitle::native::scene {
namespace {
int integer(const QJsonValue& value, int low, int high) {
    const double n = value.toDouble();
    if (!value.isDouble() || !std::isfinite(n) || std::floor(n) != n || n < low || n > high)
        throw BackendError("Skia 下方注音来源索引或时间无效");
    return int(n);
}
}
void validateSkiaLowerLayout(const QJsonObject& ir) {
    const auto layout = ir.value("skia_layout").toObject();
    const auto features = ir.value("required_features").toArray();
    const bool enabled = features.contains("p05_lower_ruby");
    if (enabled && (layout.value("lower_ruby_schema").toDouble(-1) != 1
        || !features.contains("p05_resolved_ruby") || !features.contains("p05_overlap_wipe")))
        throw BackendError("Skia 下方注音缺少版本化布局或重叠走字能力");
    const auto track = ir.value("track").toObject();
    const auto sources = track.value("rubies").toArray(), lines = layout.value("lines").toArray();
    const auto sourceLines = track.value("lines").toArray();
    std::set<int> required, seen;
    for (int index = 0; index < sources.size(); ++index) {
        const auto source = sources[index].toObject();
        if (source.contains("lower") && !source.value("lower").isBool())
            throw BackendError("Skia 下方注音来源标记无效");
        if (source.value("lower").toBool()) required.insert(index);
    }
    for (int lineIndex = 0; lineIndex < lines.size(); ++lineIndex) {
        for (const auto& value : lines[lineIndex].toObject().value("ruby_groups").toArray()) {
            const auto group = value.toObject();
            if (group.contains("lower") && !group.value("lower").isBool())
                throw BackendError("Skia 下方注音组标记无效");
            if (!group.value("lower").toBool()) {
                if (group.contains("paint") || group.contains("source_index") || group.contains("fill_gradient"))
                    throw BackendError("Skia 上注音不能携带下方注音外观或来源");
                continue;
            }
            if (!enabled) throw BackendError("Skia 下方注音缺少能力声明");
            const int index = integer(group.value("source_index"), 0, sources.size()-1);
            if (!required.count(index) || !seen.insert(index).second)
                throw BackendError("Skia 下方注音来源缺失或重复使用");
            const auto source = sources[index].toObject();
            if (integer(source.value("lower_line_index"), 0, sourceLines.size()-1) != lineIndex)
                throw BackendError("Skia 下方注音来源跨行");
            const int count = sourceLines[lineIndex].toObject().value("chars").toArray().size();
            const int begin = integer(source.value("target_char_start"), 0, count-1);
            const int end = integer(source.value("target_char_end"), begin+1, count);
            integer(group.value("style_cell"), begin, end-1);
            const int startTime = integer(source.value("pos_start_ms"), -86400000, 86400000);
            const int endTime = integer(source.value("pos_end_ms"), startTime, 86400000);
            QString reading;
            for (const auto& unit : group.value("units").toArray()) {
                const auto cell = unit.toObject();
                reading += cell.value("text").toString();
                const auto times = cell.value("interval_ms").toArray();
                if (times.size() != 2) throw BackendError("Skia 下方注音缺少最终时钟");
                const int start = integer(times[0], startTime, endTime);
                integer(times[1], start, endTime);
            }
            if (reading.isEmpty() || reading != source.value("reading").toString())
                throw BackendError("Skia 下方注音最终文本与来源不一致");
        }
    }
    if ((!required.empty() && !enabled) || seen != required)
        throw BackendError("Skia 下方注音缺少完整逐行最终组");
}
}
