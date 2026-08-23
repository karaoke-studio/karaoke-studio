#include "command_router.h"

#include "gpu_frame_commands.h"
#include "gpu_lifecycle_commands.h"
#include "gpu_probe_commands.h"
#include "qt_frame_commands.h"
#include "qt_range_commands.h"
#include "../protocol/json_protocol.h"
#include "../protocol/json_value.h"
#include "../protocol/render_config.h"
#include "../runtime/render_runtime.h"

#include <utility>

namespace krok::subtitle::native::commands {

using protocol::Command;
using protocol::RenderConfig;
using protocol::commandFromName;
using protocol::intValue;
using protocol::response;
using protocol::stringValue;
using runtime::RenderRuntime;

namespace {

void cancelGeneration(RenderRuntime *runtime, int generation) {
    if (runtime == nullptr) {
        return;
    }
    runtime->cancelGeneration(generation);
}

void joinRenderJobs(RenderRuntime *runtime) {
    if (runtime == nullptr) {
        return;
    }
    runtime->joinRenderJobs();
}

QJsonObject handleCancelGeneration(
    const QJsonObject &request,
    RenderRuntime *runtime
) {
    const int generation = intValue(request, QStringLiteral("generation"), 0);
    cancelGeneration(runtime, generation);
    QJsonObject out = response(true, QStringLiteral("generation_cancelled"));
    out.insert(QStringLiteral("generation"), generation);
    return out;
}

CommandDispatchResult output(QJsonObject value) {
    return CommandDispatchResult{std::move(value), false};
}

}  // namespace

struct CommandRouter::Impl {
    std::optional<RenderConfig> config;
    RenderRuntime runtime;
};

CommandRouter::CommandRouter()
    : impl_(std::make_unique<Impl>()) {}

CommandRouter::~CommandRouter() = default;

CommandDispatchResult CommandRouter::dispatch(const QJsonObject &request) {
    const QString commandName = stringValue(request, QStringLiteral("cmd"));
    switch (commandFromName(commandName)) {
    case Command::BackendInfo:
        return output(handleBackendInfo(request, &impl_->runtime));
    case Command::RenderProbe:
        return output(handleRenderProbe(request, &impl_->runtime));
    case Command::GpuConfigure:
        return output(handleConfigureGpu(request, impl_->config, &impl_->runtime));
    case Command::GpuResizeTarget:
        return output(handleResizeGpuTarget(request, &impl_->config, &impl_->runtime));
    case Command::GpuRenderFrame:
        return CommandDispatchResult{
            handleRenderGpuFrame(request, impl_->config, &impl_->runtime),
            false,
        };
    case Command::GpuPresentFrame:
        return output(handlePresentGpuFrame(request, impl_->config, &impl_->runtime));
    case Command::GpuPreviewClose:
        return output(handleCloseGpuPreview(request, &impl_->runtime));
    case Command::GpuDiagnostics:
        return output(handleGpuDiagnostics(request, &impl_->runtime));
    case Command::Configure:
        return output(handleConfigure(request, &impl_->config));
    case Command::RenderFrame:
        return output(handleRenderFrame(request, impl_->config));
    case Command::RenderFrameStats:
        return output(handleRenderFrameStats(request, impl_->config));
    case Command::RenderRangeStats:
        return output(handleRenderRangeStats(request, impl_->config));
    case Command::RenderRange:
        return output(handleRenderRange(request, impl_->config, &impl_->runtime));
    case Command::CancelGeneration:
        return output(handleCancelGeneration(request, &impl_->runtime));
    case Command::Shutdown:
        impl_->runtime.requestShutdown();
        joinRenderJobs(&impl_->runtime);
        return CommandDispatchResult{
            response(true, QStringLiteral("shutdown")),
            true,
        };
    case Command::Unknown: {
        QJsonObject out = response(false, QStringLiteral("unknown_command"));
        out.insert(
            QStringLiteral("error"),
            QStringLiteral("unknown command: ") + commandName
        );
        return output(std::move(out));
    }
    }
    return output(response(false, QStringLiteral("unknown_command")));
}

void CommandRouter::shutdown() {
    impl_->runtime.requestShutdown();
    joinRenderJobs(&impl_->runtime);
}

}  // namespace krok::subtitle::native::commands
