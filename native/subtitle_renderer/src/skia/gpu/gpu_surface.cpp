#include "gpu/gpu_session.h"
#include "include/core/SkColorSpace.h"
#include "include/gpu/GpuTypes.h"
#include "include/gpu/ganesh/GrDirectContext.h"
#include "include/gpu/ganesh/SkSurfaceGanesh.h"
#include <stdexcept>

namespace subtitle::skia {
GpuSession::GpuSession() = default;
GpuSession::~GpuSession() = default;
sk_sp<SkSurface> GpuSession::makeSurface(int width, int height) {
    auto surface = SkSurfaces::RenderTarget(context_.get(), skgpu::Budgeted::kYes,
        SkImageInfo::Make(width, height, kRGBA_8888_SkColorType, kPremul_SkAlphaType,
                          SkColorSpace::MakeSRGB()), 0, kTopLeft_GrSurfaceOrigin, nullptr);
    if (!surface) throw GpuDeviceError("Ganesh render target creation failed");
    return surface;
}
void GpuSession::finish(SkSurface& surface) {
    context_->flushAndSubmit(&surface, GrSyncCpu::kYes);
    if (context_->abandoned()) throw GpuDeviceError("Ganesh GPU device lost");
}
}
