#include "image_fill_cache.h"
#include "../../effects/scanline_image.h"
#include "../../model/text_style.h"
#include "include/core/SkImageInfo.h"
#include "include/core/SkPixmap.h"
#include <QtCore/QString>
#include <QtGui/QImage>
#include <QtGui/QImageReader>
#include <algorithm>
#include <cmath>

namespace krok::subtitle::native {
namespace {
// Scene-local bounds: a configure never holds more than these decoded pixels.
constexpr std::size_t kMaxImages = 64;
constexpr int kMaxSide = 8192;
constexpr std::size_t kMaxBytes = std::size_t(256) << 20;
constexpr int kMaxImageFrames = 4096;
constexpr int kGuideLoopFrames = 60; // Existing GPU loops over the first 60 guide frames.

sk_sp<SkImage> copyPremultiplied(const QImage& image) {
    if (image.isNull() || image.width() > kMaxSide || image.height() > kMaxSide) return nullptr;
    // Format_ARGB32_Premultiplied is native-endian 0xAARRGGBB, i.e. BGRA bytes.
    static_assert(Q_BYTE_ORDER == Q_LITTLE_ENDIAN, "Skia image fills assume little-endian ARGB32");
    const auto info = SkImageInfo::Make(image.width(), image.height(), kBGRA_8888_SkColorType, kPremul_SkAlphaType);
    return SkImages::RasterFromPixmapCopy(SkPixmap(info, image.constBits(), std::size_t(image.bytesPerLine())));
}

sk_sp<SkImage> decode(const std::wstring& path) {
    // Frame zero, no orientation or colour-space conversion, premultiplied:
    // the same pixels the existing GPU uploads for an image fill.
    QImageReader reader(QString::fromStdWString(path));
    reader.setAutoTransform(false);
    const QSize size = reader.size();
    if (size.isValid() && (size.width() > kMaxSide || size.height() > kMaxSide)) return nullptr;
    return copyPremultiplied(reader.read().convertToFormat(QImage::Format_ARGB32_Premultiplied));
}

std::size_t imageBytes(int width, int height) {
    return std::size_t(width) * std::size_t(height) * 4;
}
}

::subtitle::skia::ImageFill SkiaImageFillCache::fill(const PaintStyle& paint, float layoutScale) {
    ::subtitle::skia::ImageFill result;
    if (paint.mode != "image" || paint.imagePath.empty()) return result;
    result.scale = ::subtitle::skia::ImageFill::scaleFor(paint.imageScale, layoutScale);
    result.image = staticImage(paint.imagePath, paint.imageModifiedMs, paint.imageSize);
    return result;
}

bool SkiaImageFillCache::reserveDecodedBytes(std::size_t bytes) {
    if (bytes > kMaxBytes - bytes_) return false;
    bytes_ += bytes;
    return true;
}

sk_sp<SkImage> SkiaImageFillCache::staticImage(const std::wstring& path, std::uint64_t modifiedMs, std::uint64_t size) {
    if (path.empty()) return {};
    const Key key{path, modifiedMs, size};
    if (const auto found = images_.find(key); found != images_.end()) return found->second;
    if (images_.size() + scanlineImages_.size() + guideAnimations_.size() >= kMaxImages) return {};
    auto image = decode(path);
    const std::size_t bytes = image ? imageBytes(image->width(), image->height()) : 0;
    if (!reserveDecodedBytes(bytes)) image = nullptr;
    // Failures are remembered too, so one configure decodes each key once.
    images_.emplace(key, image);
    return image;
}

std::shared_ptr<const ::subtitle::skia::BitmapGuideAnimation> SkiaImageFillCache::animatedGuide(
        const std::wstring& path, std::uint64_t modifiedMs, std::uint64_t size) {
    const Key key{path, modifiedMs, size};
    if (const auto found = guideAnimations_.find(key); found != guideAnimations_.end()) return found->second;
    if (images_.size() + scanlineImages_.size() + guideAnimations_.size() >= kMaxImages) return {};
    QImageReader reader(QString::fromStdWString(path));
    reader.setAutoTransform(false);
    const auto dimensions = reader.size();
    const int frameCount = std::min(reader.imageCount(), kGuideLoopFrames);
    if (frameCount <= 1 || dimensions.isEmpty()
        || dimensions.width() > kMaxSide || dimensions.height() > kMaxSide)
        return {};
    auto sequence = std::make_shared<::subtitle::skia::BitmapGuideAnimation>();
    sequence->reserve(frameCount);
    std::size_t bytes = 0;
    std::int64_t endMs = 0;
    for (int index = 0; index < frameCount; ++index) {
        const auto frame = reader.read().convertToFormat(QImage::Format_ARGB32_Premultiplied);
        if (frame.isNull() || frame.size() != dimensions) return {};
        bytes += imageBytes(frame.width(), frame.height());
        if (bytes > kMaxBytes - bytes_) return {};
        auto image = copyPremultiplied(frame);
        if (!image) return {};
        endMs += std::max(reader.nextImageDelay(), 10);
        sequence->push_back({std::move(image), endMs});
    }
    bytes_ += bytes;
    guideAnimations_.emplace(key, sequence);
    return sequence;
}

std::shared_ptr<const ::subtitle::skia::ScanlineImage> SkiaImageFillCache::scanline(
        const TextStyle& style, int canvasHeight) {
    if (style.scanlineImagePath.empty()) return {};
    const ScanlineKey key{{style.scanlineImagePath, style.scanlineImageModifiedMs, style.scanlineImageSize},
        canvasHeight, style.scanlineImageScale, style.scanlineImageOffsetX, style.scanlineImageOffsetY,
        style.scanlineImageStroke ? style.strokeWidth : 0.f,
        style.scanlineImageStroke ? style.stroke2Width : 0.f,
        style.scanlineImageDecor && (style.decorationKind == "glow" || style.decorationKind == "both")
            && style.glowConcentrationLevel >= 0
            ? style.glowAfterRadius : 0.f,
        style.scanlineColor, style.scanlineImageTintStrength, style.scanlineImageTint, style.scanlineImageBandEnabled};
    for (const auto& [cached, image] : scanlineImages_)
        if (cached == key) return image;
    if (images_.size() + scanlineImages_.size() + guideAnimations_.size() >= kMaxImages) return {};

    using ::subtitle::skia::ScanlineImage;
    const auto load = [&]() -> std::shared_ptr<const ScanlineImage> {
        QImageReader reader(QString::fromStdWString(style.scanlineImagePath));
        reader.setAutoTransform(false);
        const auto initialSize = reader.size();
        if (initialSize.isValid() && (initialSize.width() > kMaxSide || initialSize.height() > kMaxSide)) return {};
        auto result = std::make_shared<ScanlineImage>();
        result->offsetX = key.offsetX;
        result->offsetY = key.offsetY;
        result->showBand = key.showBand;
        const float scale = std::max(key.scale, .1f);
        const float sceneScale = std::max(key.height / 1080.f * scale, .01f);
        std::size_t bytes = 0;
        std::int64_t endMs = 0;
        for (int index = 0; index < kMaxImageFrames; ++index) {
            const auto frame = reader.read().convertToFormat(QImage::Format_ARGB32_Premultiplied);
            if (frame.isNull()) break;
            const int width = frame.width(), height = frame.height();
            if (width > kMaxSide || height > kMaxSide) return {};
            const float targetW = std::max(width * sceneScale, 1.f), targetH = std::max(height * sceneScale, 1.f);
            const float drawScale = targetW / width;
            const float effectRatio = std::max(width, height) * scale / 1080.f;
            const auto srcWidth = [&](float pixels) {
                if (pixels <= 0) return 0;
                return std::clamp(int(std::lround(std::ceil(pixels * effectRatio) / drawScale)),
                                  1, std::max(height / 2, 4));
            };
            effects::ScanlineImageDecor decor;
            decor.tint = key.tint;
            decor.tintStrength = key.tintStrength;
            decor.tintColor = decor.strokeColor = decor.stroke2Color = decor.glowColor = key.color;
            decor.strokeWidth = srcWidth(key.stroke);
            decor.stroke2Width = srcWidth(key.stroke2);
            decor.glowRadius = srcWidth(key.glow);
            decor.glow = decor.glowRadius > 0;
            const int margin = std::max({decor.glowRadius * 3,
                decor.strokeWidth ? decor.strokeWidth + 1 : 0, decor.stroke2Width ? decor.stroke2Width + 1 : 0});
            const auto frameBytes = imageBytes(width + 2 * margin, height + 2 * margin);
            if (frameBytes > kMaxBytes - bytes_ - bytes) return {};
            effects::ScanlineImageComposite composite;
            if (decor.tint || margin > 0)
                composite = effects::scanlineImageCompositePixels(frame.constBits(), int(frame.bytesPerLine()),
                                                                  width, height, decor);
            const bool decorated = !composite.pixels.empty();
            const auto info = SkImageInfo::Make(decorated ? composite.width : width,
                decorated ? composite.height : height, kBGRA_8888_SkColorType, kPremul_SkAlphaType);
            auto image = SkImages::RasterFromPixmapCopy(SkPixmap(info,
                decorated ? composite.pixels.data() : frame.constBits(),
                decorated ? std::size_t(composite.width) * 4 : std::size_t(frame.bytesPerLine())));
            if (!image) return {};
            const float padding = composite.margin * drawScale;
            const auto box = SkRect::MakeXYWH(-targetW * .5f - padding, -targetH * .5f - padding,
                decorated ? composite.width * drawScale : targetW, decorated ? composite.height * drawScale : targetH);
            endMs += std::max(reader.nextImageDelay(), 10);
            result->frames.push_back({std::move(image), box, endMs});
            bytes += frameBytes;
        }
        if (result->frames.empty()) return {};
        bytes_ += bytes;
        return result;
    };
    auto image = load();
    scanlineImages_.emplace_back(key, image);
    return image;
}
}
