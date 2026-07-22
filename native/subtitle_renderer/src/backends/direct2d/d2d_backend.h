#pragma once

#include "../render_backend.h"
#include "d2d_device.h"
#include "native_preview_surface.h"

#include <memory>

namespace krok::subtitle::native {

class Direct2DGpuBackend final : public RenderBackend {
public:
    explicit Direct2DGpuBackend(bool forceWarp);
    Direct2DGpuBackend(
        bool forceWarp,
        std::shared_ptr<D2DDeviceResources> sharedDeviceResources
    );
    ~Direct2DGpuBackend() override;

    BackendCaps capabilities() const override;
    BackendDiagnostics diagnostics() const override;
    ProbeResult renderProbe(const ProbeOptions &options) override;
    void configure(const RenderScene &scene) override;
    ProbeResult renderFrame(int tMs, bool compactBands = false) override;
    NativePreviewResult presentFrame(
        int tMs,
        const NativePreviewTarget &target
    ) override;
    void closeNativePreview() override;

    std::shared_ptr<D2DDeviceResources> sharedDeviceResources() const noexcept;
    void waitForRealizationPrewarm();
    void adoptSharedGlyphResources(const Direct2DGpuBackend &source);

private:
    ProbeResult renderFrameInternal(int tMs, bool compactBands, bool readback);

    struct Impl;
    D2DDevice device_;
    std::unique_ptr<Impl> impl_;
    NativePreviewSurface previewSurface_;
};

}  // namespace krok::subtitle::native
