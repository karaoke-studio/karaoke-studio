#include "gpu_backend_runtime.h"

#include "render_runtime.h"
#include "../backends/direct2d/d2d_backend.h"

#include <QtCore/QString>

#include <algorithm>
#include <deque>
#include <iostream>
#include <memory>
#include <mutex>

namespace krok::subtitle::native::runtime {

struct GpuPreviewPoolCacheEntry {
    QString key;
    std::unique_ptr<GpuPreviewWorkerPool> pool;
};

class GpuRuntimeState {
public:
    std::mutex backendMutex;
    std::unique_ptr<RenderBackend> hardwareBackend;
    std::unique_ptr<RenderBackend> warpBackend;
    bool hardwareConfigured = false;
    bool warpConfigured = false;
    std::unique_ptr<GpuPreviewWorkerPool> hardwarePreviewPool;
    std::unique_ptr<GpuPreviewWorkerPool> warpPreviewPool;
    QString hardwarePreviewPoolKey;
    QString warpPreviewPoolKey;
    std::deque<GpuPreviewPoolCacheEntry> hardwarePreviewPoolCache;
    std::deque<GpuPreviewPoolCacheEntry> warpPreviewPoolCache;
};

class GpuBackendRuntimeAccess {
public:
    static GpuRuntimeState *state(RenderRuntime *runtime) {
        return runtime == nullptr ? nullptr : runtime->gpu_.get();
    }
};

RenderRuntime::RenderRuntime()
    : gpu_(std::make_unique<GpuRuntimeState>()) {}

RenderRuntime::~RenderRuntime() = default;

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
    auto *state = GpuBackendRuntimeAccess::state(runtime);
    std::lock_guard<std::mutex> lock(state->backendMutex);
    auto &backend = forceWarp ? state->warpBackend : state->hardwareBackend;
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
    auto *state = GpuBackendRuntimeAccess::state(runtime);
    return forceWarp
        ? state->warpPreviewPool.get()
        : state->hardwarePreviewPool.get();
}

bool gpuConfigured(
    RenderRuntime *runtime,
    bool forceWarp
) {
    if (runtime == nullptr) {
        return false;
    }
    auto *state = GpuBackendRuntimeAccess::state(runtime);
    return forceWarp ? state->warpConfigured : state->hardwareConfigured;
}

void markGpuConfigured(
    RenderRuntime *runtime,
    bool forceWarp
) {
    if (runtime == nullptr) {
        return;
    }
    auto *state = GpuBackendRuntimeAccess::state(runtime);
    if (forceWarp) {
        state->warpConfigured = true;
    } else {
        state->hardwareConfigured = true;
    }
}

void clearGpuPreviewPoolCaches(RenderRuntime *runtime) {
    if (runtime == nullptr) {
        return;
    }
    auto *state = GpuBackendRuntimeAccess::state(runtime);
    state->hardwarePreviewPoolCache.clear();
    state->warpPreviewPoolCache.clear();
}

void resetGpuPreviewPool(
    RenderRuntime *runtime,
    bool forceWarp
) {
    if (runtime == nullptr) {
        return;
    }
    auto *state = GpuBackendRuntimeAccess::state(runtime);
    if (forceWarp) {
        state->warpPreviewPool.reset();
        state->warpPreviewPoolKey.clear();
    } else {
        state->hardwarePreviewPool.reset();
        state->hardwarePreviewPoolKey.clear();
    }
}

GpuPreviewPoolConfiguration configureGpuPreviewPool(
    RenderRuntime *runtime,
    const RenderScene &scene,
    int workerCount,
    bool sharedResources,
    bool targetResize,
    bool waitRealizations,
    bool deferFollowers,
    GpuPreviewWorkerPool::Publish publish
) {
    if (runtime == nullptr) {
        return {};
    }
    auto *state = GpuBackendRuntimeAccess::state(runtime);
    auto &pool = state->hardwarePreviewPool;
    auto &poolKey = state->hardwarePreviewPoolKey;
    auto &poolCache = state->hardwarePreviewPoolCache;
    const QString targetKey = QStringLiteral("%1x%2@%3:w%4:s%5:r%6")
        .arg(scene.width)
        .arg(scene.height)
        .arg(static_cast<double>(scene.layoutReferenceScale), 0, 'f', 6)
        .arg(workerCount)
        .arg(sharedResources ? 1 : 0)
        .arg(scene.realizationEnabled ? 1 : 0);
    bool targetCacheHit = false;
    if (targetResize && pool != nullptr && poolKey == targetKey) {
        targetCacheHit = true;
    } else if (targetResize && pool != nullptr && !poolKey.isEmpty()) {
        pool->pause();
        poolCache.erase(
            std::remove_if(
                poolCache.begin(), poolCache.end(),
                [&](const GpuPreviewPoolCacheEntry &entry) {
                    return entry.key == poolKey;
                }
            ),
            poolCache.end()
        );
        poolCache.push_front({poolKey, std::move(pool)});
        const auto cached = std::find_if(
            poolCache.begin(), poolCache.end(),
            [&](const GpuPreviewPoolCacheEntry &entry) {
                return entry.key == targetKey;
            }
        );
        if (cached != poolCache.end()) {
            pool = std::move(cached->pool);
            poolCache.erase(cached);
            pool->resume(scene, deferFollowers);
            targetCacheHit = true;
        }
        while (poolCache.size() > 2) {
            poolCache.pop_back();
        }
    }
    if (pool == nullptr || pool->workerCount() != workerCount
        || pool->sharedResources() != sharedResources) {
        pool = std::make_unique<GpuPreviewWorkerPool>(
            false, workerCount, sharedResources, std::move(publish)
        );
    }
    if (!targetCacheHit) {
        pool->configure(scene, waitRealizations, deferFollowers);
    }
    poolKey = targetKey;
    return {pool.get(), targetCacheHit};
}

}  // namespace krok::subtitle::native::runtime
