#pragma once

#include "../model/render_types.h"

namespace krok::subtitle::native {

class RenderBackend {
public:
    virtual ~RenderBackend() = default;
    virtual BackendCaps capabilities() const = 0;
    virtual ProbeResult renderProbe(const ProbeOptions &options) = 0;
    virtual BackendDiagnostics diagnostics() const = 0;
    virtual void configure(const RenderScene &scene) = 0;
    virtual ProbeResult renderFrame(int tMs, bool compactBands = false) = 0;
    virtual NativePreviewResult presentFrame(
        int tMs,
        const NativePreviewTarget &target
    ) = 0;
    virtual void closeNativePreview() = 0;
};

}  // namespace krok::subtitle::native
