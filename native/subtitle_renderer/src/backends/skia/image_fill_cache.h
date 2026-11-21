#pragma once
#include "core/bitmap_guide.h"
#include "core/image_fill.h"
#include "core/scanline_image.h"
#include "../../model/paint_types.h"
#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <tuple>

namespace krok::subtitle::native {
struct TextStyle;

// Platform boundary for static image paints. Decodes each file once per
// configure, keyed like the existing GPU image cache (path, mtime, size), into
// premultiplied pixels for the Qt-free drawing core. A missing, undecodable or
// over-budget image yields an empty fill: the layer keeps its solid fallback.
class SkiaImageFillCache {
public:
    ::subtitle::skia::ImageFill fill(const PaintStyle& paint, float layoutScale);
    sk_sp<SkImage> staticImage(const std::wstring& path, std::uint64_t modifiedMs, std::uint64_t size);
    std::shared_ptr<const ::subtitle::skia::BitmapGuideAnimation> animatedGuide(
        const std::wstring& path, std::uint64_t modifiedMs, std::uint64_t size);
    bool reserveDecodedBytes(std::size_t bytes);
    std::shared_ptr<const ::subtitle::skia::ScanlineImage> scanline(const TextStyle&, int canvasHeight);

private:
    using Key = std::tuple<std::wstring, std::uint64_t, std::uint64_t>;
    struct ScanlineKey {
        Key file;
        int height;
        float scale, offsetX, offsetY, stroke, stroke2, glow;
        RgbaColor color;
        int tintStrength;
        bool tint, showBand;
        bool operator==(const ScanlineKey&) const = default;
    };
    std::map<Key, sk_sp<SkImage>> images_;
    std::map<Key, std::shared_ptr<const ::subtitle::skia::BitmapGuideAnimation>> guideAnimations_;
    std::vector<std::pair<ScanlineKey, std::shared_ptr<const ::subtitle::skia::ScanlineImage>>> scanlineImages_;
    std::size_t bytes_ = 0;
};
}
