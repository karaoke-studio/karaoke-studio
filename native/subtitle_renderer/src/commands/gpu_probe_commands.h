#pragma once

class QJsonObject;

namespace krok::subtitle::native::runtime {

class RenderRuntime;

}  // namespace krok::subtitle::native::runtime

namespace krok::subtitle::native::commands {

QJsonObject handleBackendInfo(
    const QJsonObject &request,
    runtime::RenderRuntime *runtime
);

QJsonObject handleRenderProbe(
    const QJsonObject &request,
    runtime::RenderRuntime *runtime
);

}  // namespace krok::subtitle::native::commands
