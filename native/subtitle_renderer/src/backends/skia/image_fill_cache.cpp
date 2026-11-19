#include "image_fill_cache.h"
#include "include/core/SkImageInfo.h"
#include "include/core/SkPixmap.h"
#include <QtCore/QString>
#include <QtGui/QImage>
#include <QtGui/QImageReader>

namespace krok::subtitle::native {
namespace {
// Scene-local bounds: a configure never holds more than these decoded pixels.
constexpr std::size_t kMaxImages = 64;
constexpr int kMaxSide = 8192;
constexpr std::size_t kMaxBytes = std::size_t(256) << 20;

sk_sp<SkImage> decode(const std::wstring& path) {
    // Frame zero, no orientation or colour-space conversion, premultiplied:
    // the same pixels the existing GPU uploads for an image fill.
    QImageReader reader(QString::fromStdWString(path));
    reader.setAutoTransform(false);
    const QSize size = reader.size();
    if (size.isValid() && (size.width() > kMaxSide || size.height() > kMaxSide)) return nullptr;
    const QImage image = reader.read().convertToFormat(QImage::Format_ARGB32_Premultiplied);
    if (image.isNull() || image.width() > kMaxSide || image.height() > kMaxSide) return nullptr;
    // Format_ARGB32_Premultiplied is native-endian 0xAARRGGBB, i.e. BGRA bytes.
    static_assert(Q_BYTE_ORDER == Q_LITTLE_ENDIAN, "Skia image fills assume little-endian ARGB32");
    const auto info = SkImageInfo::Make(image.width(), image.height(), kBGRA_8888_SkColorType, kPremul_SkAlphaType);
    return SkImages::RasterFromPixmapCopy(SkPixmap(info, image.constBits(), std::size_t(image.bytesPerLine())));
}
}

::subtitle::skia::ImageFill SkiaImageFillCache::fill(const PaintStyle& paint, float layoutScale) {
    ::subtitle::skia::ImageFill result;
    if (paint.mode != "image" || paint.imagePath.empty()) return result;
    result.scale = ::subtitle::skia::ImageFill::scaleFor(paint.imageScale, layoutScale);
    const Key key{paint.imagePath, paint.imageModifiedMs, paint.imageSize};
    if (const auto found = images_.find(key); found != images_.end()) {
        result.image = found->second;
        return result;
    }
    if (images_.size() >= kMaxImages) return result;
    auto image = decode(paint.imagePath);
    const std::size_t bytes = image ? std::size_t(image->width()) * std::size_t(image->height()) * 4 : 0;
    if (bytes > kMaxBytes - bytes_) image = nullptr;
    else bytes_ += bytes;
    // Failures are remembered too, so one configure decodes each key once.
    images_.emplace(key, image);
    result.image = image;
    return result;
}
}
