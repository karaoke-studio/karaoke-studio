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
    virtual bool realizationPathReady() const noexcept { return false; }
    virtual NativePreviewResult presentFrame(
        int tMs,
        const NativePreviewTarget &target,
        int generation
    ) = 0;
    virtual NativeRenderOnlyResult renderFrameOnly(int tMs, int generation) = 0;
    virtual NativePreviewResult presentRendered(
        const NativePreviewTarget &target,
        int generation,
        int tMs
    ) = 0;
    virtual void closeNativePreview() = 0;
    // 空闲心跳：只派发 DComp 子窗口积压的鼠标转发消息。
    virtual void pumpNativePreviewMessages() {}
};

}  // namespace krok::subtitle::native
