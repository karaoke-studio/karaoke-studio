#include "raster/raster_surface.h"
#include "include/core/SkColorSpace.h"
#include <stdexcept>

namespace subtitle::skia {
sk_sp<SkSurface> makeRasterSurface(int width, int height) {
    // Skia draws simple paints on its native 32-bit layout (kN32: BGRA on
    // Windows, RGBA elsewhere) with legacy blitters that truncate every blend.
    // The other byte order always goes through the raster pipeline, which
    // rounds like the GPU, so faint stacked layers agree on every platform.
    constexpr auto colorType = kN32_SkColorType == kRGBA_8888_SkColorType
        ? kBGRA_8888_SkColorType : kRGBA_8888_SkColorType;
    auto surface = SkSurfaces::Raster(SkImageInfo::Make(width, height, colorType,
        kPremul_SkAlphaType, SkColorSpace::MakeSRGB()));
    if (!surface) throw std::runtime_error("Skia Raster surface creation failed");
    return surface;
}
}
