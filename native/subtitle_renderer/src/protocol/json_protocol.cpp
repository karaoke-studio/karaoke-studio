#include "json_protocol.h"

#include <QtCore/QJsonDocument>
#include <QtCore/QJsonParseError>

#include <iostream>
#include <mutex>

namespace krok::subtitle::native::protocol {

Command commandFromName(const QString &name) {
    if (name == QStringLiteral("backend_info")) {
        return Command::BackendInfo;
    }
    if (name == QStringLiteral("render_probe")) {
        return Command::RenderProbe;
    }
    if (name == QStringLiteral("gpu_configure")) {
        return Command::GpuConfigure;
    }
    if (name == QStringLiteral("gpu_resize_target")) {
        return Command::GpuResizeTarget;
    }
    if (name == QStringLiteral("gpu_render_frame")) {
        return Command::GpuRenderFrame;
    }
    if (name == QStringLiteral("gpu_present_frame")) {
        return Command::GpuPresentFrame;
    }
    if (name == QStringLiteral("gpu_preview_close")) {
        return Command::GpuPreviewClose;
    }
    if (name == QStringLiteral("gpu_diagnostics")) {
        return Command::GpuDiagnostics;
    }
    if (name == QStringLiteral("configure")) {
        return Command::Configure;
    }
    if (name == QStringLiteral("render_frame")) {
        return Command::RenderFrame;
    }
    if (name == QStringLiteral("render_frame_stats")) {
        return Command::RenderFrameStats;
    }
    if (name == QStringLiteral("render_range_stats")) {
        return Command::RenderRangeStats;
    }
    if (name == QStringLiteral("render_range")) {
        return Command::RenderRange;
    }
    if (name == QStringLiteral("cancel_generation")) {
        return Command::CancelGeneration;
    }
    if (name == QStringLiteral("shutdown")) {
        return Command::Shutdown;
    }
    return Command::Unknown;
}

QJsonObject response(bool ok, const QString &event) {
    QJsonObject out;
    out.insert(QStringLiteral("ok"), ok);
    out.insert(QStringLiteral("event"), event);
    return out;
}

QJsonObject parseErrorResponse(const QString &message) {
    QJsonObject out = response(false, QStringLiteral("parse_error"));
    out.insert(QStringLiteral("error"), message);
    return out;
}

std::optional<QJsonObject> parseRequestLine(
    const QString &line,
    QJsonObject *errorResponse
) {
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(
        line.toUtf8(),
        &parseError
    );
    if (parseError.error == QJsonParseError::NoError && document.isObject()) {
        return document.object();
    }
    if (errorResponse != nullptr) {
        *errorResponse = parseErrorResponse(parseError.errorString());
    }
    return std::nullopt;
}

void writeJson(const QJsonObject &object) {
    static std::mutex mutex;
    std::lock_guard<std::mutex> lock(mutex);
    const QJsonDocument document(object);
    std::cout << document.toJson(QJsonDocument::Compact).constData()
              << std::endl;
}

}  // namespace krok::subtitle::native::protocol
