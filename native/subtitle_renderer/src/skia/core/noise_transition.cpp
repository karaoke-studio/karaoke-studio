#include "noise_transition.h"
#include "../../effects/noise.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkImage.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPixmap.h"
#include "include/core/SkShader.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <mutex>
#include <stdexcept>

namespace subtitle::skia {
namespace {
const sk_sp<SkImage>& noiseTile(int level, bool streaks) {
    struct Tile {
        std::once_flag once;
        sk_sp<SkImage> image;
    };
    static std::array<Tile, 65> tiles;
    auto& tile = tiles[streaks ? 64 : level];
    std::call_once(tile.once, [&] {
        const auto pixels = krok::subtitle::native::effects::noiseMaskPixels(level, streaks);
        const auto info = SkImageInfo::MakeN32Premul(128, 128);
        tile.image = SkImages::RasterFromPixmapCopy(SkPixmap(info, pixels.data(), info.minRowBytes()));
        if (!tile.image) throw std::runtime_error("Skia noise mask allocation failed");
    });
    return tile.image;
}
}

int NoiseTransition::levelAt(int time, int start, int end) const {
    if (entryMs <= 0 && exitMs <= 0) return 64;
    return krok::subtitle::native::effects::noiseLevel(
        "noise", "noise", entryMs, exitMs, start, end, singingEnd, time);
}

void NoiseTransition::composite(SkCanvas& canvas, const SkImage& source, int level) const {
    const auto rect = SkRect::MakeWH(float(source.width()), float(source.height()));
    const float cell = std::max(1.f, std::round(rect.height() / 1080.f));
    const auto maskScale = SkMatrix::Scale(cell, cell);
    const SkSamplingOptions nearest{SkFilterMode::kNearest};
    const auto composite = [&](const sk_sp<SkImage>& mask, float opacity, float offset) {
        auto maskShader = mask->makeShader(SkTileMode::kRepeat, SkTileMode::kRepeat, nearest, maskScale);
        auto pixels = source.makeShader(SkTileMode::kDecal, SkTileMode::kDecal, nearest,
                                        SkMatrix::Translate(0, offset));
        SkPaint paint;
        paint.setShader(SkShaders::Blend(SkBlendMode::kDstIn, std::move(pixels), std::move(maskShader)));
        paint.setAlphaf(opacity);
        canvas.drawRect(rect, paint);
    };
    const float coverage = float(level) / 64.f;
    const float envelope = 4 * coverage * (1 - coverage);
    constexpr float distances[] = {.05f, .10f, .16f, .23f};
    constexpr float alphas[] = {.50f, .30f, .18f, .10f};
    for (int index = 0; index < 4; ++index)
        composite(noiseTile(0, true), envelope * alphas[index], std::round(fontPx * distances[index]));
    composite(noiseTile(level, false), 1.f, 0.f);
}
}
