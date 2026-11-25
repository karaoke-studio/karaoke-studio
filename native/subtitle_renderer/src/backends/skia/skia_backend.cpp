#include "skia_backend.h"
#include "../backend_factory.h"
#include "raster/raster_surface.h"
#include "scene_input.h"
#include "include/core/SkColorSpace.h"
#include <array>
#include <chrono>
#include <deque>
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
    // Straight-RGBA conversion targets for the pipelined export readback.
    std::array<sk_sp<SkSurface>, 2> straight;
    bool asyncReadUnsupported = false;
#endif
    std::vector<std::uint8_t> zeros;
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
    resetSequenceTargets();
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
    resetSequenceTargets();
}
ProbeResult SkiaBackend::renderFrame(int tMs, bool compactBands) {
    requireOwner();
    if (!impl_->prepared) throw BackendError("Skia scene is not configured");
    if (compactBands) throw BackendError("Skia P04 does not support compact bands");
    using Clock = std::chrono::steady_clock;
    const auto begin = Clock::now();
    impl_->prepared->draw(*impl_->surface->getCanvas(), tMs);
    const auto rendered = Clock::now();
    ProbeResult result;
    auto& out = result.surface;
    out.width = impl_->prepared->width();
    out.height = impl_->prepared->height();
    out.stride = out.width * 4;
    out.bytes.resize(std::size_t(out.stride) * out.height);
    auto info = SkImageInfo::Make(out.width, out.height, kRGBA_8888_SkColorType,
                                  kUnpremul_SkAlphaType, SkColorSpace::MakeSRGB());
    // Ganesh flushes and synchronizes the producing commands during this
    // read. Avoid a separate synchronous submission before the transfer.
    const bool read = impl_->surface->readPixels(info, out.bytes.data(), out.stride, 0, 0);
#ifdef KROK_SKIA_GPU_ENABLED
    if (impl_->gpu) {
        impl_->gpu->checkDevice();
        if (!read) throw ::subtitle::skia::GpuDeviceError("Skia GPU RGBA readback failed");
    }
#endif
    if (!read) throw BackendError("Skia RGBA readback failed");
    result.renderMs = std::chrono::duration<double, std::milli>(rendered - begin).count();
    result.readbackMs = std::chrono::duration<double, std::milli>(Clock::now() - rendered).count();
    return result;
}
void SkiaBackend::resetSequenceTargets() {
#ifdef KROK_SKIA_GPU_ENABLED
    // Pending reads never outlive renderSequence(), so the targets are idle here.
    impl_->straight = {};
#endif
    impl_->zeros.clear();
}

void SkiaBackend::renderSequence(const std::vector<int>& times, const std::function<bool()>& cancelled,
                                 const std::function<void(std::size_t, const SequenceFrame&)>& sink) {
    requireOwner();
    if (!impl_->prepared) throw BackendError("Skia scene is not configured");
    const auto& prepared = *impl_->prepared;
    const int width = prepared.width(), height = prepared.height();
    const std::size_t stride = std::size_t(width) * 4;
    using Clock = std::chrono::steady_clock;
    const auto elapsed = [](Clock::time_point from) {
        return std::chrono::duration<double, std::milli>(Clock::now() - from).count();
    };
    // A frame that only receives the transparent clear reads back as zeros.
    const auto emitEmpty = [&](std::size_t index) {
        if (impl_->zeros.size() != stride * std::size_t(height)) impl_->zeros.assign(stride * std::size_t(height), 0);
        sink(index, {impl_->zeros.data(), width, height, stride, 0, 0, true});
    };
    const auto emitSynchronous = [&](std::size_t index, int tMs) {
        auto result = renderFrame(tMs);
        const auto& surface = result.surface;
        sink(index, {surface.bytes.data(), surface.width, surface.height, std::size_t(surface.stride),
                     result.renderMs, result.readbackMs, false});
    };
#ifdef KROK_SKIA_GPU_ENABLED
    if (impl_->gpu && !impl_->asyncReadUnsupported) {
        auto& gpu = *impl_->gpu;
        for (auto& target : impl_->straight)
            if (!target || target->width() != width || target->height() != height)
                target = gpu.makeSurface(width, height);
        struct Pending {
            std::size_t index;
            int tMs;
            bool empty;
            int slot;
            double renderMs;
        };
        std::array<::subtitle::skia::AsyncStraightRead, 2> reads;
        // Outstanding callbacks reference `reads`; complete them on every exit.
        struct Drain {
            ::subtitle::skia::GpuSession& gpu;
            ~Drain() {
                try { gpu.drain(); } catch (...) {}
            }
        } drain{gpu};
        std::deque<Pending> pending;
        int nextSlot = 0;
        const auto deliver = [&](const Pending& frame) {
            if (frame.empty) return emitEmpty(frame.index);
            auto& read = reads[std::size_t(frame.slot)];
            const auto waiting = Clock::now();
            gpu.wait(read);
            const double waited = elapsed(waiting);
            if (!read.result || read.result->count() != 1) {
                // Transfer buffers unavailable: keep the synchronous readback.
                read = {};
                impl_->asyncReadUnsupported = true;
                return emitSynchronous(frame.index, frame.tMs);
            }
            sink(frame.index, {static_cast<const std::uint8_t*>(read.result->data(0)), width, height,
                               read.result->rowBytes(0), frame.renderMs, waited, false});
            read = {};
        };
        for (std::size_t index = 0; index < times.size(); ++index) {
            if (cancelled()) return;
            Pending frame{index, times[index], !prepared.hasContent(times[index]), -1, 0};
            if (!frame.empty) {
                const auto begin = Clock::now();
                prepared.draw(*impl_->surface->getCanvas(), frame.tMs);
                frame.slot = nextSlot;
                nextSlot ^= 1;
                gpu.readStraightAsync(*impl_->surface, *impl_->straight[std::size_t(frame.slot)],
                                      reads[std::size_t(frame.slot)]);
                frame.renderMs = elapsed(begin);
            }
            pending.push_back(frame);
            // Record this frame before waiting on the previous transfer.
            if (pending.size() > 1) {
                const auto oldest = pending.front();
                pending.pop_front();
                deliver(oldest);
            }
        }
        while (!pending.empty()) {
            const auto oldest = pending.front();
            pending.pop_front();
            deliver(oldest);
        }
        return;
    }
#endif
    for (std::size_t index = 0; index < times.size(); ++index) {
        if (cancelled()) return;
        if (!prepared.hasContent(times[index])) emitEmpty(index);
        else emitSynchronous(index, times[index]);
    }
}

NativePreviewResult SkiaBackend::presentFrame(int, const NativePreviewTarget&, int) { unavailable(); }
NativeRenderOnlyResult SkiaBackend::renderFrameOnly(int, int) { unavailable(); }
NativePreviewResult SkiaBackend::presentRendered(const NativePreviewTarget&, int, int) { unavailable(); }
void SkiaBackend::closeNativePreview() { requireOwner(); }
std::unique_ptr<RenderBackend> makeBackend(const SkiaOptions& options) {
    return std::make_unique<SkiaBackend>(options);
}
}
