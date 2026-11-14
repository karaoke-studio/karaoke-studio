#include "gpu/metal_borrowed.h"
#include "gpu/context_options.h"
#include "include/core/SkColorSpace.h"
#include "include/core/SkSurface.h"
#include "include/gpu/ganesh/GrBackendSurface.h"
#include "include/gpu/ganesh/GrDirectContext.h"
#include "include/gpu/ganesh/SkSurfaceGanesh.h"
#include "include/gpu/ganesh/mtl/GrMtlBackendContext.h"
#include "include/gpu/ganesh/mtl/GrMtlBackendSurface.h"
#include "include/gpu/ganesh/mtl/GrMtlDirectContext.h"
#include "include/gpu/ganesh/mtl/GrMtlTypes.h"
#include <stdexcept>

namespace subtitle::skia {
sk_sp<GrDirectContext> makeBorrowedMetalContext(const MetalBorrowedDevice& borrowed) {
    if (!borrowed.device || !borrowed.queue) throw std::runtime_error("Borrowed Metal device is incomplete");
    GrMtlBackendContext backend;
    // retain(): Skia holds its own references; the owner's references are untouched.
    backend.fDevice.retain(borrowed.device);
    backend.fQueue.retain(borrowed.queue);
    auto context = GrDirectContexts::MakeMetal(backend, subtitleContextOptions());
    if (!context) throw std::runtime_error("Skia Ganesh could not use the borrowed Metal device");
    return context;
}

sk_sp<SkSurface> wrapMetalTexture(GrDirectContext& context, const MetalTextureTarget& target) {
    if (!target.texture) throw std::runtime_error("Borrowed Metal texture is missing");
    GrMtlTextureInfo info;
    info.fTexture.retain(target.texture);
    const auto renderTarget = GrBackendRenderTargets::MakeMtl(target.width, target.height, info);
    auto surface = SkSurfaces::WrapBackendRenderTarget(
        &context, renderTarget, kTopLeft_GrSurfaceOrigin,
        target.bgra ? kBGRA_8888_SkColorType : kRGBA_8888_SkColorType, SkColorSpace::MakeSRGB(), nullptr);
    if (!surface) throw std::runtime_error("Skia could not wrap the borrowed Metal texture");
    return surface;
}

void submitMetal(GrDirectContext& context, SkSurface& surface) {
    context.flush(&surface, GrFlushInfo());
    if (!context.submit(GrSyncCpu::kNo) || context.abandoned())
        throw std::runtime_error("Skia could not commit to the borrowed Metal queue");
}
}
