#include <QtCore/QByteArray>
#include <QtCore/QJsonObject>
#include <QtCore/QIODevice>
#include <QtCore/QTextStream>
#include <QtWidgets/QApplication>

#include "commands/gpu_lifecycle_commands.h"
#include "commands/gpu_frame_commands.h"
#include "commands/gpu_probe_commands.h"
#include "commands/qt_frame_commands.h"
#include "commands/qt_range_commands.h"
#include "protocol/json_protocol.h"
#include "protocol/json_value.h"
#include "protocol/render_config.h"
#include "runtime/render_runtime.h"

#include <optional>

namespace {

using krok::subtitle::native::protocol::kRenderIrSchema;
using krok::subtitle::native::protocol::Command;
using krok::subtitle::native::protocol::commandFromName;
using krok::subtitle::native::protocol::intValue;
using krok::subtitle::native::protocol::parseRequestLine;
using krok::subtitle::native::protocol::RenderConfig;
using krok::subtitle::native::protocol::response;
using krok::subtitle::native::protocol::stringValue;
using krok::subtitle::native::protocol::writeJson;
using krok::subtitle::native::commands::handleConfigure;
using krok::subtitle::native::commands::handleBackendInfo;
using krok::subtitle::native::commands::handleCloseGpuPreview;
using krok::subtitle::native::commands::handleConfigureGpu;
using krok::subtitle::native::commands::handleGpuDiagnostics;
using krok::subtitle::native::commands::handlePresentGpuFrame;
using krok::subtitle::native::commands::handleRenderGpuFrame;
using krok::subtitle::native::commands::handleRenderProbe;
using krok::subtitle::native::commands::handleResizeGpuTarget;
using krok::subtitle::native::commands::handleRenderFrame;
using krok::subtitle::native::commands::handleRenderFrameStats;
using krok::subtitle::native::commands::handleRenderRange;
using krok::subtitle::native::commands::handleRenderRangeStats;
using krok::subtitle::native::runtime::RenderRuntime;

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


QJsonObject handleCancelGeneration(const QJsonObject &request, RenderRuntime *runtime) {
    const int generation = intValue(request, QStringLiteral("generation"), 0);
    cancelGeneration(runtime, generation);
    QJsonObject out = response(true, QStringLiteral("generation_cancelled"));
    out.insert(QStringLiteral("generation"), generation);
    return out;
}

}  // namespace

int main(int argc, char **argv) {
#if !defined(Q_OS_WIN)
    qputenv("QT_QPA_PLATFORM", qgetenv("QT_QPA_PLATFORM").isEmpty() ? QByteArray("offscreen") : qgetenv("QT_QPA_PLATFORM"));
#endif
    QApplication app(argc, argv);

    QJsonObject ready = response(true, QStringLiteral("ready"));
    ready.insert(QStringLiteral("schema"), kRenderIrSchema);
    ready.insert(QStringLiteral("gpu_protocol"), 1);
    ready.insert(QStringLiteral("native_preview_protocol"), 1);
    ready.insert(QStringLiteral("qt"), QString::fromLatin1(qVersion()));
    writeJson(ready);

    std::optional<RenderConfig> config;
    RenderRuntime runtime;
    QTextStream input(stdin, QIODevice::ReadOnly);
    while (!input.atEnd()) {
        const QString line = input.readLine().trimmed();
        if (line.isEmpty()) {
            continue;
        }

        QJsonObject parseError;
        const auto request = parseRequestLine(line, &parseError);
        if (!request.has_value()) {
            writeJson(parseError);
            continue;
        }

        const QString commandName = stringValue(*request, QStringLiteral("cmd"));
        switch (commandFromName(commandName)) {
        case Command::BackendInfo:
            writeJson(handleBackendInfo(*request, &runtime));
            break;
        case Command::RenderProbe:
            writeJson(handleRenderProbe(*request, &runtime));
            break;
        case Command::GpuConfigure:
            writeJson(handleConfigureGpu(*request, config, &runtime));
            break;
        case Command::GpuResizeTarget:
            writeJson(handleResizeGpuTarget(*request, &config, &runtime));
            break;
        case Command::GpuRenderFrame:
            if (auto out = handleRenderGpuFrame(*request, config, &runtime)) {
                writeJson(*out);
            }
            break;
        case Command::GpuPresentFrame:
            writeJson(handlePresentGpuFrame(*request, config, &runtime));
            break;
        case Command::GpuPreviewClose:
            writeJson(handleCloseGpuPreview(*request, &runtime));
            break;
        case Command::GpuDiagnostics:
            writeJson(handleGpuDiagnostics(*request, &runtime));
            break;
        case Command::Configure:
            writeJson(handleConfigure(*request, &config));
            break;
        case Command::RenderFrame:
            writeJson(handleRenderFrame(*request, config));
            break;
        case Command::RenderFrameStats:
            writeJson(handleRenderFrameStats(*request, config));
            break;
        case Command::RenderRangeStats:
            writeJson(handleRenderRangeStats(*request, config));
            break;
        case Command::RenderRange:
            writeJson(handleRenderRange(*request, config, &runtime));
            break;
        case Command::CancelGeneration:
            writeJson(handleCancelGeneration(*request, &runtime));
            break;
        case Command::Shutdown:
            runtime.requestShutdown();
            joinRenderJobs(&runtime);
            writeJson(response(true, QStringLiteral("shutdown")));
            return 0;
        case Command::Unknown: {
            QJsonObject out = response(false, QStringLiteral("unknown_command"));
            out.insert(
                QStringLiteral("error"),
                QStringLiteral("unknown command: ") + commandName
            );
            writeJson(out);
            break;
        }
        }
    }

    runtime.requestShutdown();
    joinRenderJobs(&runtime);
    return 0;
}
