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

}  // namespace runtime
}  // namespace krok::subtitle::native
