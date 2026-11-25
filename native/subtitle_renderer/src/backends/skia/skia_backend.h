#pragma once
#include "../backend_selection.h"
#include "../render_backend.h"
#include "core/resolved_scene.h"
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <vector>
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
    void configureResolved(const ::subtitle::skia::ResolvedSubtitleInput&);
    const char* profile() const;
    ProbeResult renderFrame(int, bool = false) override;
    // One straight-RGBA export frame. `rgba` is valid only during the sink call.
    struct SequenceFrame {
        const std::uint8_t* rgba = nullptr;
        int width = 0, height = 0;
        std::size_t stride = 0;
        double renderMs = 0, readbackMs = 0;
        bool empty = false;
    };
    // Renders `times` in order and hands each frame to `sink` in the same
    // order. GPU recording of the next frame overlaps the previous transfer;
    // frames that draw nothing skip the device. Stops before a frame once
    // `cancelled` returns true.
    void renderSequence(const std::vector<int>& times, const std::function<bool()>& cancelled,
                        const std::function<void(std::size_t, const SequenceFrame&)>& sink);
    NativePreviewResult presentFrame(int, const NativePreviewTarget&, int) override;
    NativeRenderOnlyResult renderFrameOnly(int, int) override;
    NativePreviewResult presentRendered(const NativePreviewTarget&, int, int) override;
    void closeNativePreview() override;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
    void requireOwner() const;
    void resetSequenceTargets();
    [[noreturn]] void unavailable() const;
};
}
