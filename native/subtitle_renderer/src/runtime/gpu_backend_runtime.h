#pragma once

class QString;

namespace krok::subtitle::native {

class RenderBackend;

namespace runtime {

class GpuPreviewWorkerPool;
class RenderRuntime;

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

}  // namespace runtime
}  // namespace krok::subtitle::native
