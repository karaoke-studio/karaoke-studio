#pragma once
#include "core/image_fill.h"
#include "../../model/paint_types.h"
#include <cstdint>
#include <map>
#include <string>
#include <tuple>

namespace krok::subtitle::native {
// Platform boundary for static image paints. Decodes each file once per
// configure, keyed like the existing GPU image cache (path, mtime, size), into
// premultiplied pixels for the Qt-free drawing core. A missing, undecodable or
// over-budget image yields an empty fill: the layer keeps its solid fallback.
class SkiaImageFillCache {
public:
    ::subtitle::skia::ImageFill fill(const PaintStyle& paint, float layoutScale);

private:
    using Key = std::tuple<std::wstring, std::uint64_t, std::uint64_t>;
    std::map<Key, sk_sp<SkImage>> images_;
    std::size_t bytes_ = 0;
};
}
