#pragma once

class QImage;
class QJsonObject;

namespace krok::subtitle::native::legacy_qt {

struct RenderDiagnostics;

}  // namespace krok::subtitle::native::legacy_qt

namespace krok::subtitle::native::diagnostics {

void appendQtFrameDiagnostics(
    QJsonObject *out,
    int tMs,
    const QImage &image,
    const legacy_qt::RenderDiagnostics &diagnostics,
    double renderMs
);

}  // namespace krok::subtitle::native::diagnostics
