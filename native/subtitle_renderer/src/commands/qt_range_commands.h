#pragma once

#include "../protocol/render_config.h"

#include <optional>

class QJsonObject;

namespace krok::subtitle::native::runtime {

class RenderRuntime;

}  // namespace krok::subtitle::native::runtime

namespace krok::subtitle::native::commands {

QJsonObject handleRenderRangeStats(
    const QJsonObject &request,
    const std::optional<protocol::RenderConfig> &config
);

QJsonObject handleRenderRange(
    const QJsonObject &request,
    const std::optional<protocol::RenderConfig> &config,
    runtime::RenderRuntime *runtime
);

}  // namespace krok::subtitle::native::commands
