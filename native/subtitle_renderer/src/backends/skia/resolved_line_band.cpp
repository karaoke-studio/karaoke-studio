#include "resolved_line_band.h"
#include "image_fill_cache.h"
#include "../render_backend.h"
#include "include/codec/SkGifDecoder.h"
#include "include/codec/SkWebpDecoder.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColorFilter.h"
#include "include/core/SkData.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPixmap.h"
#include "include/core/SkSurface.h"
#include "include/effects/SkColorMatrix.h"
#include <QtCore/QFile>
#include <algorithm>
#include <cstring>
#include <utility>

namespace krok::subtitle::native {
namespace {
using Fit = ::subtitle::skia::LineBandTexture::Fit;
using Frame = ::subtitle::skia::LineBandTexture::Frame;

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
    if (!images.reserveDecodedBytes(std::size_t(notes->width()) * notes->height() * 4))
        throw BackendError("Skia 五线谱背景带超过场景图片预算");
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

sk_sp<SkImage> loadStaffLayer(SkiaImageFillCache& images, const std::wstring& path, bool tint, RgbaColor color) {
    if (path.empty()) return {};
    auto image = images.staticImage(path, 0, 0);
    if (!image) throw BackendError("Skia 五线谱背景带无法读取预设图层");
    if (!tint) return image;
    if (!images.reserveDecodedBytes(std::size_t(image->width()) * image->height() * 4))
        throw BackendError("Skia 五线谱背景带超过场景图片预算");
    const auto surface = SkSurfaces::Raster(SkImageInfo::MakeN32Premul(image->width(), image->height()));
    if (!surface) throw BackendError("Skia 五线谱背景带无法准备预设图层");
    auto& canvas = *surface->getCanvas();
    canvas.clear(SK_ColorTRANSPARENT);
    drawStaffLayer(canvas, image, true, color);
    image = surface->makeImageSnapshot();
    if (!image) throw BackendError("Skia 五线谱背景带无法准备预设图层");
    return image;
}

sk_sp<SkImage> tintBandImage(sk_sp<SkImage> image, const LineBandStyle& band, SkiaImageFillCache& images) {
    if (!image || !band.hasTint || band.tintStrengthPct <= 0) return image;
    if (!images.reserveDecodedBytes(std::size_t(image->width()) * image->height() * 4))
        throw BackendError("Skia 行背景带超过场景图片预算");
    const auto surface = SkSurfaces::Raster(SkImageInfo::MakeN32Premul(image->width(), image->height()));
    if (!surface) throw BackendError("Skia 无法为行背景带着色");
    const float strength = band.tintStrengthPct / 100.f;
    SkColorMatrix matrix;
    matrix.setScale(1.f - strength, 1.f - strength, 1.f - strength, 1.f);
    matrix.postTranslate(band.tint.red * strength / 255.f, band.tint.green * strength / 255.f,
                         band.tint.blue * strength / 255.f, 0.f);
    SkPaint paint;
    paint.setColorFilter(SkColorFilters::Matrix(matrix));
    auto& canvas = *surface->getCanvas();
    canvas.clear(SK_ColorTRANSPARENT);
    canvas.drawImage(image, 0, 0, SkSamplingOptions(), &paint);
    image = surface->makeImageSnapshot();
    if (!image) throw BackendError("Skia 无法为行背景带着色");
    return image;
}

std::vector<Frame> loadBandAnimation(const std::wstring& path, SkiaImageFillCache& images) {
    QFile file(QString::fromStdWString(path));
    constexpr qint64 maxEncodedBytes = 64 * 1024 * 1024;
    if (!file.open(QIODevice::ReadOnly) || file.size() <= 0 || file.size() > maxEncodedBytes)
        throw BackendError("Skia 行背景带无法读取动画素材");
    const auto data = file.read(maxEncodedBytes + 1);
    if (data.isEmpty() || data.size() > maxEncodedBytes)
        throw BackendError("Skia 行背景带无法读取动画素材");
    const auto bytes = SkData::MakeWithCopy(data.constData(), data.size());
    if (!bytes) throw BackendError("Skia 行背景带动画分配失败");
    auto codec = SkGifDecoder::IsGif(bytes->data(), bytes->size()) ? SkGifDecoder::Decode(bytes, nullptr)
        : SkWebpDecoder::Decode(bytes, nullptr);
    if (!codec) throw BackendError("Skia 行背景带动画帧无效");
    const int frameCount = codec->getFrameCount();
    if (frameCount < 1 || frameCount > 4096)
        throw BackendError("Skia 行背景带动画帧无效");
    const auto info = codec->getInfo().makeColorType(kBGRA_8888_SkColorType).makeAlphaType(kPremul_SkAlphaType);
    const auto stride = info.minRowBytes();
    const auto frameBytes = info.computeByteSize(stride);
    const auto frames = codec->getFrameInfo();
    if (info.width() <= 0 || info.width() > 8192 || info.height() <= 0 || info.height() > 8192
        || (frameCount > 1 && frames.size() != std::size_t(frameCount))
        || !images.reserveDecodedBytes(frameBytes * frameCount))
        throw BackendError("Skia 行背景带动画超过场景图片预算");
    std::vector<Frame> result;
    result.reserve(frameCount);
    std::int64_t end = 0;
    for (int index = 0; index < frameCount; ++index) {
        auto pixels = SkData::MakeUninitialized(frameBytes);
        if (!pixels) throw BackendError("Skia 行背景带动画分配失败");
        SkCodec::Options options;
        options.fFrameIndex = index;
        const int previous = frameCount == 1 ? SkCodec::kNoFrame : frames[index].fRequiredFrame;
        if (previous >= 0) {
            SkPixmap prior;
            if (previous >= index || !result[previous].image->peekPixels(&prior) || prior.rowBytes() != stride)
                throw BackendError("Skia 行背景带动画帧依赖无效");
            std::memcpy(pixels->writable_data(), prior.addr(), frameBytes);
            options.fPriorFrame = previous;
        }
        if (codec->getPixels(info, pixels->writable_data(), stride, &options) != SkCodec::kSuccess)
            throw BackendError("Skia 行背景带动画帧解码失败");
        auto image = SkImages::RasterFromData(info, std::move(pixels), stride);
        if (!image) throw BackendError("Skia 行背景带动画无法准备图像");
        end += frameCount == 1 ? 10 : std::max(frames[index].fDuration, 10);
        result.push_back({std::move(image), end});
    }
    return result;
}
}

::subtitle::skia::LineBandTexture resolveLineBandTexture(const LineBandStyle& band, SkiaImageFillCache& images,
                                                        bool animated) {
    ::subtitle::skia::LineBandTexture texture{{}, band.opacity, fit(band.fillMode)};
    if (animated) {
        texture.staffLines = loadStaffLayer(images, band.linesPath, band.hasLineColor, band.lineColor);
        texture.staffClef = loadStaffLayer(images, band.clefPath, band.hasClefColor, band.clefColor);
        texture.animation = loadBandAnimation(band.imagePath, images);
        texture.image = texture.animation.front().image;
        if (texture.animation.size() == 1) texture.animation.clear();
    } else {
        texture.image = tintBandImage(images.staticImage(band.imagePath, 0, 0), band, images);
        if (!band.linesPath.empty() || !band.clefPath.empty())
            texture.image = composeStaticStaff(band, images, std::move(texture.image));
    }
    if (band.fillMode == "three_slice") {
        texture.slices = ::subtitle::skia::LineBandTexture::Slices{
            band.capLeft, band.capRight, band.srcCapLeft, band.srcCapRight, fit(band.midMode)};
        if (texture.animation.empty()) texture.prepareMiddle();
    }
    if (band.stroke.enabled) {
        const auto& color = band.stroke.color;
        texture.outerStroke = ::subtitle::skia::LineBandTexture::Stroke{
            band.stroke.size, SkColorSetARGB(color.alpha, color.red, color.green, color.blue)};
    }
    return texture;
}
}
