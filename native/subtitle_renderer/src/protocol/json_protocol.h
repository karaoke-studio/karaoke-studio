#pragma once

#include <QtCore/QJsonObject>
#include <QtCore/QString>

#include <optional>

namespace krok::subtitle::native::protocol {

// Schema 2 adds the root ``vector_glyphs`` outline table; characters reference
// entries through ``vector_glyph_id`` instead of embedding a full outline copy.
inline constexpr int kRenderIrSchema = 2;

enum class Command {
    BackendInfo,
    RenderProbe,
    GpuConfigure,
    GpuResizeTarget,
    GpuRenderFrame,
    GpuPresentFrame,
    GpuPreviewClose,
    GpuDiagnostics,
    Configure,
    RenderFrame,
    RenderFrameStats,
    RenderRangeStats,
    RenderRange,
    CancelGeneration,
    Shutdown,
    Unknown,
};

Command commandFromName(const QString &name);
QJsonObject response(bool ok, const QString &event);
QJsonObject parseErrorResponse(const QString &message);
std::optional<QJsonObject> parseRequestLine(
    const QString &line,
    QJsonObject *errorResponse
);
void writeJson(const QJsonObject &object);

}  // namespace krok::subtitle::native::protocol
