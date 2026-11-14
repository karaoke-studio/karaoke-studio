#include "backends/backend_factory.h"
#include "runtime/owner_backend_session.h"
#include <atomic>
#include <future>
#include <iostream>
#include <string>

using namespace krok::subtitle::native;
using namespace krok::subtitle::native::runtime;
int main(int argc, char** argv) {
    try {
        SkiaOptions options{SkiaTarget::Raster};
        for (int i = 1; i < argc; ++i) {
            if (std::string(argv[i]) == "--gpu") options.target = SkiaTarget::Gpu;
            else if (std::string(argv[i]) == "--allow-software") options.requireHardware = false;
            else throw std::runtime_error("unknown argument");
        }
        OwnerBackendSession session([options] { return makeBackend(options); });
        BackendCaps caps;
        std::promise<JobCompletion> done;
        auto future = done.get_future();
        const bool accepted = session.submit(1, [&](RenderBackend& backend) {
            caps = backend.capabilities();
            if (caps.backend != "skia" || caps.warp || !caps.supportsGlyphs || caps.supportsNativePreview
                || !caps.supportsTransparentSurface || !caps.supportsStagingReadback)
                throw std::runtime_error("incorrect advertised capabilities");
            if (options.target == SkiaTarget::Raster && (caps.api != "raster" || caps.hardware))
                throw std::runtime_error("Raster identity");
            backend.configure({});
            auto blank = backend.renderFrame(0);
            if (blank.surface.width != 1920 || blank.surface.bytes.size() != 1920u * 1080 * 4)
                throw std::runtime_error("empty scene readback");
            bool unsupported = false;
            RenderScene invalid;
            invalid.viewportRotation = 30;
            try { backend.configure(invalid); } catch (const BackendError&) { unsupported = true; }
            if (!unsupported) throw std::runtime_error("unsupported scene must fail explicitly");
            if (backend.renderFrame(0).surface.bytes != blank.surface.bytes)
                throw std::runtime_error("failed configure changed the scene");
            const auto diagnostics = backend.diagnostics();
            if (diagnostics.realizationEnabled || diagnostics.realizationSupported || diagnostics.resourceCacheEnabled)
                throw std::runtime_error("Direct2D cache features must not leak into Skia diagnostics");
        }, [&](JobCompletion r) { done.set_value(r); });
        if (!accepted) throw std::runtime_error("fresh session rejected its first job");
        auto result = future.get();
        if (result.error) std::rethrow_exception(result.error);
        if (result.outcome != JobOutcome::Completed) throw std::runtime_error("unexpected cancellation");
        session.close();
        // Cross-thread calls are rejected before any Skia/GPU access.
        auto backend = makeBackend(SkiaOptions{SkiaTarget::Raster});
        std::atomic<bool> rejected{false};
        std::thread foreign([&] { try { backend->capabilities(); } catch (const BackendError&) { rejected = true; } });
        foreign.join();
        if (!rejected) throw std::runtime_error("owner guard");
        std::cout << "backend=skia api=" << caps.api << " target=" << caps.executionTarget
                  << " hardware=" << caps.hardware << " adapter=" << caps.adapterName << '\n';
        return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
