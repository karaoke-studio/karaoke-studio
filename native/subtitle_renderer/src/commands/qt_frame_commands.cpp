#include "qt_frame_commands.h"

#include "../backends/qt/qt_frame_renderer.h"
#include "../backends/qt/qt_render_cache.h"
#include "../diagnostics/qt_frame_diagnostics_json.h"
#include "../protocol/json_protocol.h"
#include "../protocol/json_value.h"
#include "../protocol/render_config_parser.h"

#include <QtCore/QElapsedTimer>
#include <QtCore/QJsonObject>
#include <QtGui/QImage>

namespace krok::subtitle::native::commands {

using diagnostics::appendQtFrameDiagnostics;
using legacy_qt::RenderResult;
using legacy_qt::clearGlowBitmapCache;
using legacy_qt::clearLayoutCache;
using legacy_qt::clearTextLayerCache;
using legacy_qt::renderFrame;
using protocol::RenderConfig;
using protocol::intValue;
using protocol::parseRenderConfig;
using protocol::response;
using protocol::stringValue;

QJsonObject handleConfigure(
    const QJsonObject &request,
    std::optional<RenderConfig> *config
) {
    QString error;
    auto parsed = parseRenderConfig(
        request.value(QStringLiteral("ir")).toObject(), &error
    );
    if (!parsed.has_value()) {
        QJsonObject out = response(false, QStringLiteral("configure"));
        out.insert(QStringLiteral("error"), error);
        return out;
    }
    *config = parsed;
    clearGlowBitmapCache();
    clearTextLayerCache();
    clearLayoutCache();
    QJsonObject out = response(true, QStringLiteral("configured"));
    out.insert(QStringLiteral("width"), parsed->width);
    out.insert(QStringLiteral("height"), parsed->height);
    out.insert(QStringLiteral("fps"), parsed->fps);
    out.insert(QStringLiteral("dpr"), parsed->dpr);
    out.insert(QStringLiteral("physical_width"), parsed->physicalWidth());
    out.insert(QStringLiteral("physical_height"), parsed->physicalHeight());
    out.insert(QStringLiteral("line_count"), static_cast<int>(parsed->lines.size()));
    out.insert(QStringLiteral("ruby_count"), static_cast<int>(parsed->rubies.size()));
    return out;
}

QJsonObject handleRenderFrame(
    const QJsonObject &request,
    const std::optional<RenderConfig> &config
) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("render_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }

    const int tMs = intValue(request, QStringLiteral("t_ms"), 0);
    const QString outputPath = stringValue(request, QStringLiteral("output_path"));
    if (outputPath.isEmpty()) {
        QJsonObject out = response(false, QStringLiteral("render_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("output_path is required for native smoke render"));
        return out;
    }

    QElapsedTimer timer;
    timer.start();
    RenderResult rendered = renderFrame(*config, tMs);
    const double renderMs = static_cast<double>(timer.nsecsElapsed()) / 1000000.0;
    QImage &image = rendered.image;
    const bool saved = image.save(outputPath);
    QJsonObject out = response(saved, QStringLiteral("frame_ready"));
    out.insert(QStringLiteral("output_path"), outputPath);
    appendQtFrameDiagnostics(&out, tMs, image, rendered.diagnostics, renderMs);
    if (!saved) {
        out.insert(QStringLiteral("error"), QStringLiteral("failed to save output image"));
    }
    return out;
}

QJsonObject handleRenderFrameStats(
    const QJsonObject &request,
    const std::optional<RenderConfig> &config
) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("render_frame_stats"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }

    const int tMs = intValue(request, QStringLiteral("t_ms"), 0);
    QElapsedTimer timer;
    timer.start();
    RenderResult rendered = renderFrame(*config, tMs);
    const double renderMs = static_cast<double>(timer.nsecsElapsed()) / 1000000.0;
    QJsonObject out = response(true, QStringLiteral("frame_stats"));
    appendQtFrameDiagnostics(&out, tMs, rendered.image, rendered.diagnostics, renderMs);
    return out;
}

}  // namespace krok::subtitle::native::commands
