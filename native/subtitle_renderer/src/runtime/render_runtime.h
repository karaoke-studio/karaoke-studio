#pragma once

#include "gpu_preview_worker_pool.h"
#include "render_job_runtime.h"
#include "shared_frame_ring.h"

#include "../backends/render_backend.h"

#include <QtCore/QString>

#include <deque>
#include <memory>
#include <mutex>

namespace krok::subtitle::native::runtime {

struct GpuPreviewPoolCacheEntry {
    QString key;
    std::unique_ptr<GpuPreviewWorkerPool> pool;
};

struct RenderRuntime {
    RenderJobRuntime jobs;
    SharedFrameRingBuffer sharedFrames;
    std::mutex gpuBackendMutex;
    std::unique_ptr<RenderBackend> hardwareGpuBackend;
    std::unique_ptr<RenderBackend> warpGpuBackend;
    bool hardwareGpuConfigured = false;
    bool warpGpuConfigured = false;
    std::unique_ptr<GpuPreviewWorkerPool> hardwareGpuPreviewPool;
    std::unique_ptr<GpuPreviewWorkerPool> warpGpuPreviewPool;
    QString hardwareGpuPreviewPoolKey;
    QString warpGpuPreviewPoolKey;
    std::deque<GpuPreviewPoolCacheEntry> hardwareGpuPreviewPoolCache;
    std::deque<GpuPreviewPoolCacheEntry> warpGpuPreviewPoolCache;
};

}  // namespace krok::subtitle::native::runtime
