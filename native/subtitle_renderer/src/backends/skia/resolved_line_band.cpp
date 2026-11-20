#include "resolved_line_band.h"
#include "image_fill_cache.h"
#include "../render_backend.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColorFilter.h"
#include "include/core/SkPaint.h"
#include "include/core/SkSurface.h"
#include "include/effects/SkColorMatrix.h"
#include <utility>

namespace krok::subtitle::native {
namespace {
using Fit = ::subtitle::skia::LineBandTexture::Fit;

Fit fit(const std::string& mode) {
    if (mode == "cover") return Fit::Cover;
    if (mode == "repeat") return Fit::Repeat;
    return Fit::Stretch;
}

void drawStaffLayer(SkCanvas& canvas, const sk_sp<SkImage>& image, bool tint, const RgbaColor& color) {
    SkPaint paint;
    if (tint) {
        SkColorMatrix matrix;
        matrix.setScale(color.red / 255.f, color.green / 255.f, color.blue / 255.f);
        paint.setColorFilter(SkColorFilters::Matrix(matrix));
    }
    canvas.drawImage(image, 0, 0, SkSamplingOptions(), &paint);
}

sk_sp<SkImage> composeStaticStaff(const LineBandStyle& band, SkiaImageFillCache& images, sk_sp<SkImage> notes) {
    const auto lines = images.staticImage(band.linesPath, 0, 0);
    const auto clef = images.staticImage(band.clefPath, 0, 0);
    if (!notes || !lines || !clef) throw BackendError("Skia 五线谱背景带无法读取预设图层");
    const auto surface = SkSurfaces::Raster(SkImageInfo::MakeN32Premul(notes->width(), notes->height()));
    if (!surface) throw BackendError("Skia 五线谱背景带无法准备预设图层");
    auto& canvas = *surface->getCanvas();
    canvas.clear(SK_ColorTRANSPARENT);
    drawStaffLayer(canvas, lines, band.hasLineColor, band.lineColor);
    drawStaffLayer(canvas, clef, band.hasClefColor, band.clefColor);
    canvas.drawImage(notes, 0, 0);
    auto result = surface->makeImageSnapshot();
    if (!result) throw BackendError("Skia 五线谱背景带无法准备预设图层");
    return result;
}
}

::subtitle::skia::LineBandTexture resolveLineBandTexture(const LineBandStyle& band, SkiaImageFillCache& images) {
    auto image = images.staticImage(band.imagePath, 0, 0);
    if (!band.linesPath.empty() || !band.clefPath.empty()) image = composeStaticStaff(band, images, std::move(image));
    ::subtitle::skia::LineBandTexture texture{std::move(image), band.opacity, fit(band.fillMode)};
    if (band.fillMode == "three_slice") {
        texture.slices = ::subtitle::skia::LineBandTexture::Slices{
            band.capLeft, band.capRight, band.srcCapLeft, band.srcCapRight, fit(band.midMode)};
        texture.prepareMiddle();
    }
    return texture;
}
}
