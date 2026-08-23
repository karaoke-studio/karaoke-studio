#pragma once

#include "../protocol/render_config.h"

#include <optional>

class QJsonObject;

namespace krok::subtitle::native::runtime {

class RenderRuntime;

}  // namespace krok::subtitle::native::runtime

namespace krok::subtitle::native::commands {

QJsonObject handleConfigureGpu(
    const QJsonObject &request,
    const std::optional<protocol::RenderConfig> &config,
    runtime::RenderRuntime *runtime
);

QJsonObject handleResizeGpuTarget(
    const QJsonObject &request,
    std::optional<protocol::RenderConfig> *config,
    runtime::RenderRuntime *runtime
);

QJsonObject handleGpuDiagnostics(
    const QJsonObject &request,
    runtime::RenderRuntime *runtime
);

QJsonObject handleCloseGpuPreview(
    const QJsonObject &request,
    runtime::RenderRuntime *runtime
);

}  // namespace krok::subtitle::native::commands
