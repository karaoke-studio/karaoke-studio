#pragma once

#include "../backends/render_backend.h"

class QJsonObject;

namespace krok::subtitle::native::diagnostics {

QJsonObject backendCapsJson(const BackendCaps &caps);

void appendGpuDiagnostics(
    QJsonObject *out,
    const BackendDiagnostics &diagnostics
);

void appendGpuFrameDiagnostics(
    QJsonObject *out,
    const ProbeResult::FrameDiagnostics &diagnostics
);

}  // namespace krok::subtitle::native::diagnostics
