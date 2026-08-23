#pragma once

#include "render_job_runtime.h"
#include "shared_frame_ring.h"

#include <QtCore/QString>

#include <cstdint>
#include <memory>
#include <thread>

namespace krok::subtitle::native::runtime {

class GpuBackendRuntimeAccess;
class GpuRuntimeState;

class RenderRuntime {
public:
    RenderRuntime();
    ~RenderRuntime();

    RenderRuntime(const RenderRuntime &) = delete;
    RenderRuntime &operator=(const RenderRuntime &) = delete;

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
    friend class GpuBackendRuntimeAccess;

    std::unique_ptr<GpuRuntimeState> gpu_;
    RenderJobRuntime jobs_;
    SharedFrameRingBuffer sharedFrames_;
};

}  // namespace krok::subtitle::native::runtime
