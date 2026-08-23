#pragma once

#include "../protocol/render_config.h"

#include <optional>

class QJsonObject;

namespace krok::subtitle::native::commands {

QJsonObject handleConfigure(
    const QJsonObject &request,
    std::optional<protocol::RenderConfig> *config
);

QJsonObject handleRenderFrame(
    const QJsonObject &request,
    const std::optional<protocol::RenderConfig> &config
);

QJsonObject handleRenderFrameStats(
    const QJsonObject &request,
    const std::optional<protocol::RenderConfig> &config
);

}  // namespace krok::subtitle::native::commands
