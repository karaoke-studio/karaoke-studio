#include "skia_backend.h"
#include "../backend_factory.h"
#include "raster/raster_surface.h"
#include "scene_input.h"
#include "include/core/SkColorSpace.h"
#include <chrono>
#ifdef KROK_SKIA_GPU_ENABLED
#include "gpu/gpu_session.h"
#endif
#include <thread>

namespace krok::subtitle::native {
struct SkiaBackend::Impl {
    std::thread::id owner = std::this_thread::get_id();
    BackendCaps caps;
#ifdef KROK_SKIA_GPU_ENABLED
    std::unique_ptr<::subtitle::skia::GpuSession> gpu;
#endif
    // Reverse destruction: prepared paths, surface, then the owning GPU session.
    sk_sp<SkSurface> surface;
    std::unique_ptr<::subtitle::skia::PreparedScene> prepared;
    const char* profile = "p04_ascii_solid_wipe";
};
SkiaBackend::SkiaBackend(const SkiaOptions& options) : impl_(std::make_unique<Impl>()) {
    auto& caps = impl_->caps;
    caps.backend = "skia";
    caps.supportsGlyphs = true;  // Limited by the explicit P04 input validator.
    caps.supportsTransparentSurface = true;
    caps.supportsStagingReadback = true;
    if (options.target == SkiaTarget::Raster) {
        impl_->surface = ::subtitle::skia::makeRasterSurface(1, 1);
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
    throw BackendError("Skia P04 尚不支持此直画/探针操作");
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
void SkiaBackend::configure(const RenderScene& scene) {
    requireOwner();
    // Transactional prepare: a rejected configure preserves the previous scene.
    auto prepared = std::make_unique<::subtitle::skia::PreparedScene>(skiaSceneInput(scene));
    sk_sp<SkSurface> surface;
#ifdef KROK_SKIA_GPU_ENABLED
    if (impl_->gpu) surface = impl_->gpu->makeSurface(scene.width, scene.height);
    else
#endif
        surface = ::subtitle::skia::makeRasterSurface(scene.width, scene.height);
    impl_->surface = std::move(surface);
    impl_->prepared = std::move(prepared);
    impl_->profile = "p04_ascii_solid_wipe";
}
const char* SkiaBackend::profile() const { requireOwner(); return impl_->profile; }
void SkiaBackend::configureResolved(const ::subtitle::skia::ResolvedSubtitleInput& input) {
    requireOwner();
    auto prepared = std::make_unique<::subtitle::skia::PreparedScene>(input);
    sk_sp<SkSurface> surface;
#ifdef KROK_SKIA_GPU_ENABLED
    if (impl_->gpu) surface = impl_->gpu->makeSurface(input.width, input.height);
    else
#endif
        surface = ::subtitle::skia::makeRasterSurface(input.width, input.height);
    impl_->surface = std::move(surface);
    impl_->prepared = std::move(prepared);
    impl_->profile = "p05_resolved_horizontal";
}
ProbeResult SkiaBackend::renderFrame(int tMs, bool compactBands) {
    requireOwner();
    if (!impl_->prepared) throw BackendError("Skia scene is not configured");
    if (compactBands) throw BackendError("Skia P04 does not support compact bands");
    using Clock = std::chrono::steady_clock;
    const auto begin = Clock::now();
    impl_->prepared->draw(*impl_->surface->getCanvas(), tMs);
#ifdef KROK_SKIA_GPU_ENABLED
    if (impl_->gpu) impl_->gpu->finish(*impl_->surface);
#endif
    const auto rendered = Clock::now();
    ProbeResult result;
    auto& out = result.surface;
    out.width = impl_->prepared->width();
    out.height = impl_->prepared->height();
    out.stride = out.width * 4;
    out.bytes.resize(std::size_t(out.stride) * out.height);
    auto info = SkImageInfo::Make(out.width, out.height, kRGBA_8888_SkColorType,
                                  kUnpremul_SkAlphaType, SkColorSpace::MakeSRGB());
    if (!impl_->surface->readPixels(info, out.bytes.data(), out.stride, 0, 0))
        throw BackendError("Skia RGBA readback failed");
    result.renderMs = std::chrono::duration<double, std::milli>(rendered - begin).count();
    result.readbackMs = std::chrono::duration<double, std::milli>(Clock::now() - rendered).count();
    return result;
}
NativePreviewResult SkiaBackend::presentFrame(int, const NativePreviewTarget&, int) { unavailable(); }
NativeRenderOnlyResult SkiaBackend::renderFrameOnly(int, int) { unavailable(); }
NativePreviewResult SkiaBackend::presentRendered(const NativePreviewTarget&, int, int) { unavailable(); }
void SkiaBackend::closeNativePreview() { requireOwner(); }
std::unique_ptr<RenderBackend> makeBackend(const SkiaOptions& options) {
    return std::make_unique<SkiaBackend>(options);
}
}
