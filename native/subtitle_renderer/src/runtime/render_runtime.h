#pragma once

#include "gpu_backend_runtime.h"
#include "gpu_preview_worker_pool.h"
#include "render_job_runtime.h"
#include "shared_frame_ring.h"

#include "../backends/render_backend.h"

#include <QtCore/QString>

#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <thread>

namespace krok::subtitle::native::runtime {

struct GpuPreviewPoolCacheEntry {
    QString key;
    std::unique_ptr<GpuPreviewWorkerPool> pool;
};

class RenderRuntime {
public:
    bool generationCancelled(int generation);
    void cancelGeneration(int generation);
    void clearGenerationCancel(int generation);
    void rememberRenderJob(std::thread job);
    void joinRenderJobs();
    void requestShutdown() noexcept;

    int sharedFrameSlotCount() const;
    bool ensureSharedFrameRing(
        const QString &key,
        int ringSlotCount,
        int width,
        int height,
        QString *error
    );
    bool writeSharedRgbaSlot(
        const std::uint8_t *rgba,
        int width,
        int height,
        int stride,
        int generation,
        int frameIndex,
        int tMs,
        int slotIndex,
        SharedFrameRing *ringOut,
        int formatId = 1,
        const QString &pixelFormat = QStringLiteral("rgba8888")
    );
    bool writeSharedPackedRgbaSlot(
        const std::uint8_t *premultipliedBgra,
        int width,
        int height,
        int stride,
        int generation,
        int frameIndex,
        int tMs,
        int slotIndex,
        SharedFrameRing *ringOut
    );
    bool writeSharedBandSlot(
        const std::uint8_t *payloadData,
        int payloadBytes,
        int width,
        int height,
        int stride,
        int generation,
        int frameIndex,
        int tMs,
        int slotIndex,
        SharedFrameRing *ringOut
    );

private:
    friend RenderBackend *ensureGpuBackend(
        RenderRuntime *runtime,
        bool forceWarp,
        QString *error
    );
    friend GpuPreviewWorkerPool *gpuPreviewPool(
        RenderRuntime *runtime,
        bool forceWarp
    );
    friend bool gpuConfigured(RenderRuntime *runtime, bool forceWarp);
    friend void markGpuConfigured(RenderRuntime *runtime, bool forceWarp);
    friend void clearGpuPreviewPoolCaches(RenderRuntime *runtime);
    friend void resetGpuPreviewPool(RenderRuntime *runtime, bool forceWarp);
    friend GpuPreviewPoolConfiguration configureGpuPreviewPool(
        RenderRuntime *runtime,
        const RenderScene &scene,
        int workerCount,
        bool sharedResources,
        bool targetResize,
        bool waitRealizations,
        bool deferFollowers,
        GpuPreviewWorkerPool::Publish publish
    );

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

    RenderJobRuntime jobs_;
    SharedFrameRingBuffer sharedFrames_;
};

}  // namespace krok::subtitle::native::runtime
