#include "raster/raster_surface.h"
#include "include/core/SkColorSpace.h"
#include <stdexcept>

namespace subtitle::skia {
sk_sp<SkSurface> makeRasterSurface(int width, int height) {
    auto surface = SkSurfaces::Raster(SkImageInfo::Make(width, height, kRGBA_8888_SkColorType,
        kPremul_SkAlphaType, SkColorSpace::MakeSRGB()));
    if (!surface) throw std::runtime_error("Skia Raster surface creation failed");
    return surface;
}
}
