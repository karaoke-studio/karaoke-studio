#include "skia_backend.h"
#include "../backend_factory.h"
#include "raster/raster_surface.h"
#ifdef KROK_SKIA_GPU_ENABLED
#include "gpu/gpu_session.h"
#endif
#include <thread>

namespace krok::subtitle::native {
struct SkiaBackend::Impl {
    std::thread::id owner = std::this_thread::get_id();
    BackendCaps caps;
    sk_sp<SkSurface> raster;
#ifdef KROK_SKIA_GPU_ENABLED
    std::unique_ptr<::subtitle::skia::GpuSession> gpu;
#endif
};
SkiaBackend::SkiaBackend(const SkiaOptions& options) : impl_(std::make_unique<Impl>()) {
    auto& caps = impl_->caps;
    caps.backend = "skia";
    caps.supportsTransparentSurface = true;
    caps.supportsStagingReadback = true;
    // Glyph/scene/probe protocol support is deliberately not advertised in P02.
    if (options.target == SkiaTarget::Raster) {
        impl_->raster = ::subtitle::skia::makeRasterSurface(1, 1);
        caps.adapterName = "Skia Raster CPU";
        caps.api = "raster";
        caps.executionTarget = "raster";
    } else if (options.target == SkiaTarget::Gpu) {
#ifdef KROK_SKIA_GPU_ENABLED
        impl_->gpu = ::subtitle::skia::makeGpuSession(options.requireHardware);
        const auto& device = impl_->gpu->info();
        caps.adapterName = device.device;
        caps.hardware = device.hardware;
        caps.api = device.api;
        caps.executionTarget = "gpu";
#else
        throw BackendError("此构建未启用 Skia GPU，不能隐式改用 CPU");
#endif
    } else throw BackendError("无效的 Skia 执行目标");
}
SkiaBackend::~SkiaBackend() = default;
void SkiaBackend::requireOwner() const {
    if (impl_->owner != std::this_thread::get_id()) throw BackendError("Skia 后端必须在所属线程调用");
}
[[noreturn]] void SkiaBackend::unavailable() const {
    requireOwner();
    throw BackendError("Skia 字幕协议绘制尚未实现（P04），不能使用其他后端伪装成功");
}
BackendCaps SkiaBackend::capabilities() const { requireOwner(); return impl_->caps; }
BackendDiagnostics SkiaBackend::diagnostics() const {
    requireOwner();
    BackendDiagnostics out;
    out.realizationEnabled = false;
    out.resourceCacheEnabled = false;
    out.glowDirtyRectEnabled = false;
    return out;
}
ProbeResult SkiaBackend::renderProbe(const ProbeOptions&) { unavailable(); }
void SkiaBackend::configure(const RenderScene&) { unavailable(); }
ProbeResult SkiaBackend::renderFrame(int, bool) { unavailable(); }
NativePreviewResult SkiaBackend::presentFrame(int, const NativePreviewTarget&, int) { unavailable(); }
NativeRenderOnlyResult SkiaBackend::renderFrameOnly(int, int) { unavailable(); }
NativePreviewResult SkiaBackend::presentRendered(const NativePreviewTarget&, int, int) { unavailable(); }
void SkiaBackend::closeNativePreview() { requireOwner(); }
std::unique_ptr<RenderBackend> makeBackend(const SkiaOptions& options) {
    return std::make_unique<SkiaBackend>(options);
}
}
