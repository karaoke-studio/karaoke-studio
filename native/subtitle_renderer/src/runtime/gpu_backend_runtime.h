#pragma once

#include "gpu_preview_worker_pool.h"

class QString;

namespace krok::subtitle::native {

class RenderBackend;
struct RenderScene;

namespace runtime {

class RenderRuntime;

struct GpuPreviewPoolConfiguration {
    GpuPreviewWorkerPool *pool = nullptr;
    bool targetCacheHit = false;
};

RenderBackend *ensureGpuBackend(
    RenderRuntime *runtime,
    bool forceWarp,
    QString *error
);

GpuPreviewWorkerPool *gpuPreviewPool(
    RenderRuntime *runtime,
    bool forceWarp
);

bool gpuConfigured(
    RenderRuntime *runtime,
    bool forceWarp
);

void markGpuConfigured(
    RenderRuntime *runtime,
    bool forceWarp
);

void clearGpuPreviewPoolCaches(RenderRuntime *runtime);

void resetGpuPreviewPool(
    RenderRuntime *runtime,
    bool forceWarp
);

GpuPreviewPoolConfiguration configureGpuPreviewPool(
    RenderRuntime *runtime,
    const RenderScene &scene,
    int workerCount,
    bool sharedResources,
    bool targetResize,
    bool waitRealizations,
    bool deferFollowers,
    GpuPreviewWorkerPool::Publish publish
);

}  // namespace runtime
}  // namespace krok::subtitle::native
