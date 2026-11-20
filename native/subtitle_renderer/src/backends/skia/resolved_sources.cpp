#include "resolved_input.h"
#include "../../model/backend_caps.h"
#include <QtCore/QJsonArray>
#include <algorithm>
#include <iterator>

namespace krok::subtitle::native {
namespace {
void countSource(const QJsonObject& source, const QJsonValue& screen, qsizetype& lines, qsizetype& cells) {
    if (source.value("screen") != screen || source.contains("skia_extra_sources"))
        throw BackendError("Skia 多轨尺寸不一致或包含嵌套来源");
    // These features carry only source-local geometry. Other combinations need
    // their own validation of draw order before sharing the scene.
    static const QJsonArray allowed{
        "p05_resolved_horizontal", "p05_single_line_placement", "p05_inline_roles",
        "p05_fixed_row_grid", "p05_page_collisions", "p05_smart_horizontal",
        "p05_segmented_wipe", "p05_overlap_wipe", "p05_font_fallback",
        "p05_mixed_fonts", "p05_reverse_wipe", "p05_reverse_overlap",
        "p07_no_wipe", "p07_line_fade", "p07_line_motion", "p08_viewport"
    };
    for (const auto& feature : source.value("required_features").toArray())
        if (!allowed.contains(feature)) throw BackendError("Skia 多轨暂不支持此绘制组合");
    const auto layout = source.value("skia_layout").toObject().value("lines").toArray();
    if ((lines += layout.size()) > 4096) throw BackendError("Skia 多轨行数超限");
    for (const auto& line : layout)
        if ((cells += line.toObject().value("cells").toArray().size()) > 65536)
            throw BackendError("Skia 多轨字形单元总数超限");
}
}

::subtitle::skia::ResolvedSubtitleInput resolvedSkiaInput(const QJsonObject& ir) {
    auto features = ir.value("required_features").toArray();
    const auto feature = std::find(features.begin(), features.end(), QJsonValue("p08_multiple_sources"));
    if (feature == features.end()) return resolvedSkiaSource(ir);

    const auto input = ir.value("skia_extra_sources");
    const auto extras = input.toArray();
    if (!input.isArray() || extras.isEmpty() || extras.size() > 15)
        throw BackendError("Skia 多轨配置缺少附加轨道或数量超限");
    features.erase(feature);
    auto primary = ir;
    primary.remove("skia_extra_sources");
    primary.insert("required_features", features);

    const auto screen = primary.value("screen");
    qsizetype lineCount = 0, cellCount = 0;
    countSource(primary, screen, lineCount, cellCount);
    for (const auto& value : extras) {
        if (!value.isObject()) throw BackendError("Skia 附加轨道格式无效");
        countSource(value.toObject(), screen, lineCount, cellCount);
    }

    auto result = resolvedSkiaSource(primary);
    result.lines.reserve(lineCount);
    for (const auto& value : extras) {
        auto source = resolvedSkiaSource(value.toObject());
        if (source.viewport != result.viewport)
            throw BackendError("Skia 多轨字幕视口不一致");
        result.lines.insert(result.lines.end(), std::make_move_iterator(source.lines.begin()),
                            std::make_move_iterator(source.lines.end()));
    }
    return result;
}
}
