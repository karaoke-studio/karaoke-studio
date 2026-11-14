#pragma once
#include "../backend_selection.h"
#include "../render_backend.h"
#include <memory>
namespace krok::subtitle::native {
// Device ownership/readback boundary. All subtitle paths and drawing live in the common core.
class SkiaBackend final : public RenderBackend {
public:
    explicit SkiaBackend(const SkiaOptions& options);
    ~SkiaBackend() override;
    BackendCaps capabilities() const override;
    BackendDiagnostics diagnostics() const override;
    ProbeResult renderProbe(const ProbeOptions&) override;
    void configure(const RenderScene&) override;
    ProbeResult renderFrame(int, bool = false) override;
    NativePreviewResult presentFrame(int, const NativePreviewTarget&, int) override;
    NativeRenderOnlyResult renderFrameOnly(int, int) override;
    NativePreviewResult presentRendered(const NativePreviewTarget&, int, int) override;
    void closeNativePreview() override;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
    void requireOwner() const;
    [[noreturn]] void unavailable() const;
};
}
