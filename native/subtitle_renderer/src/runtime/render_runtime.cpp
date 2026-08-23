#include "render_runtime.h"

#include <utility>

namespace krok::subtitle::native::runtime {

bool RenderRuntime::generationCancelled(int generation) {
    return jobs_.generationCancelled(generation);
}

void RenderRuntime::cancelGeneration(int generation) {
    jobs_.cancelGeneration(generation);
}

void RenderRuntime::clearGenerationCancel(int generation) {
    jobs_.clearGenerationCancel(generation);
}

void RenderRuntime::rememberRenderJob(std::thread job) {
    jobs_.remember(std::move(job));
}

void RenderRuntime::joinRenderJobs() {
    jobs_.joinAll();
}

void RenderRuntime::requestShutdown() noexcept {
    jobs_.requestShutdown();
}

int RenderRuntime::sharedFrameSlotCount() const {
    return sharedFrames_.slotCount();
}

bool RenderRuntime::ensureSharedFrameRing(
    const QString &key,
    int ringSlotCount,
    int width,
    int height,
    QString *error
) {
    return sharedFrames_.ensure(key, ringSlotCount, width, height, error);
}

bool RenderRuntime::writeSharedRgbaSlot(
    const std::uint8_t *rgba,
    int width,
    int height,
    int stride,
    int generation,
    int frameIndex,
    int tMs,
    int slotIndex,
    SharedFrameRing *ringOut,
    int formatId,
    const QString &pixelFormat
) {
    return sharedFrames_.writeRgba(
        rgba,
        width,
        height,
        stride,
        generation,
        frameIndex,
        tMs,
        slotIndex,
        ringOut,
        formatId,
        pixelFormat
    );
}

bool RenderRuntime::writeSharedPackedRgbaSlot(
    const std::uint8_t *premultipliedBgra,
    int width,
    int height,
    int stride,
    int generation,
    int frameIndex,
    int tMs,
    int slotIndex,
    SharedFrameRing *ringOut
) {
    return sharedFrames_.writePremultipliedBgra(
        premultipliedBgra,
        width,
        height,
        stride,
        generation,
        frameIndex,
        tMs,
        slotIndex,
        ringOut
    );
}

bool RenderRuntime::writeSharedBandSlot(
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
) {
    return sharedFrames_.writeBands(
        payloadData,
        payloadBytes,
        width,
        height,
        stride,
        generation,
        frameIndex,
        tMs,
        slotIndex,
        ringOut
    );
}

}  // namespace krok::subtitle::native::runtime
