#include "gpu_backend_runtime.h"

#include "render_runtime.h"
#include "../backends/direct2d/d2d_backend.h"

#include <QtCore/QString>

#include <iostream>
#include <memory>
#include <mutex>

namespace krok::subtitle::native::runtime {

RenderBackend *ensureGpuBackend(
    RenderRuntime *runtime,
    bool forceWarp,
    QString *error
) {
    if (runtime == nullptr) {
        if (error != nullptr) {
            *error = QStringLiteral("render runtime is unavailable");
        }
        return nullptr;
    }
    std::lock_guard<std::mutex> lock(runtime->gpuBackendMutex);
    auto &backend = forceWarp ? runtime->warpGpuBackend : runtime->hardwareGpuBackend;
    if (backend == nullptr) {
        try {
            backend = std::make_unique<krok::subtitle::native::Direct2DGpuBackend>(forceWarp);
            const auto caps = backend->capabilities();
            std::cerr
                << "gpu_backend=direct2d adapter=\"" << caps.adapterName
                << "\" feature_level=" << caps.featureLevel
                << " warp=" << (caps.warp ? 1 : 0) << std::endl;
        } catch (const std::exception &exception) {
            if (error != nullptr) {
                *error = QString::fromUtf8(exception.what());
            }
            return nullptr;
        }
    }
    return backend.get();
}

GpuPreviewWorkerPool *gpuPreviewPool(
    RenderRuntime *runtime,
    bool forceWarp
) {
    if (runtime == nullptr) {
        return nullptr;
    }
    return forceWarp
        ? runtime->warpGpuPreviewPool.get()
        : runtime->hardwareGpuPreviewPool.get();
}

bool gpuConfigured(
    RenderRuntime *runtime,
    bool forceWarp
) {
    if (runtime == nullptr) {
        return false;
    }
    return forceWarp
        ? runtime->warpGpuConfigured
        : runtime->hardwareGpuConfigured;
}

void markGpuConfigured(
    RenderRuntime *runtime,
    bool forceWarp
) {
    if (runtime == nullptr) {
        return;
    }
    if (forceWarp) {
        runtime->warpGpuConfigured = true;
    } else {
        runtime->hardwareGpuConfigured = true;
    }
}

void clearGpuPreviewPoolCaches(RenderRuntime *runtime) {
    if (runtime == nullptr) {
        return;
    }
    runtime->hardwareGpuPreviewPoolCache.clear();
    runtime->warpGpuPreviewPoolCache.clear();
}

void resetGpuPreviewPool(
    RenderRuntime *runtime,
    bool forceWarp
) {
    if (runtime == nullptr) {
        return;
    }
    if (forceWarp) {
        runtime->warpGpuPreviewPool.reset();
        runtime->warpGpuPreviewPoolKey.clear();
    } else {
        runtime->hardwareGpuPreviewPool.reset();
        runtime->hardwareGpuPreviewPoolKey.clear();
    }
}

}  // namespace krok::subtitle::native::runtime
