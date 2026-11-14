#pragma once
#include "include/core/SkRefCnt.h"

class GrDirectContext;
class SkSurface;

namespace subtitle::skia {
// An MTLDevice and MTLCommandQueue owned by someone else (the Qt Quick scene graph). Handles are
// Objective-C object pointers passed opaquely, so C++ callers need no Objective-C. The owner keeps
// them alive for the context's whole life; every call happens on the owner's render thread.
struct MetalBorrowedDevice {
    const void* device = nullptr;  // id<MTLDevice>
    const void* queue = nullptr;   // id<MTLCommandQueue>
};

// Ganesh context on the borrowed device and queue. Destroy it before the owner releases them.
sk_sp<GrDirectContext> makeBorrowedMetalContext(const MetalBorrowedDevice& device);

// A render-target texture the owner allocated; Skia draws into it but never frees it.
struct MetalTextureTarget {
    const void* texture = nullptr;  // id<MTLTexture>
    int width = 0;
    int height = 0;
    bool bgra = false;
};
sk_sp<SkSurface> wrapMetalTexture(GrDirectContext& context, const MetalTextureTarget& target);

// Commits Skia's command buffer to the shared queue now. Metal runs command buffers in enqueue
// order and orders tracked-resource writes before later reads, so work the owner commits later
// on that queue sees Skia's pixels.
void submitMetal(GrDirectContext& context, SkSurface& surface);
}
