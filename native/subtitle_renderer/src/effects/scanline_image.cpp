#include "scanline_image.h"

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::effects {
namespace {

void dilateAlphaInPlace(
    std::vector<float> &alpha, int width, int height, int radius
) {
    if (radius <= 0) {
        return;
    }
    std::vector<float> temp(alpha.size());
    for (int y = 0; y < height; ++y) {
        const float *row = alpha.data() + static_cast<std::size_t>(y) * width;
        float *out = temp.data() + static_cast<std::size_t>(y) * width;
        for (int x = 0; x < width; ++x) {
            float best = 0.0f;
            for (int k = -radius; k <= radius; ++k) {
                const int xx = std::clamp(x + k, 0, width - 1);
                best = std::max(best, row[xx]);
            }
            out[x] = best;
        }
    }
    for (int x = 0; x < width; ++x) {
        for (int y = 0; y < height; ++y) {
            float best = 0.0f;
            for (int k = -radius; k <= radius; ++k) {
                const int yy = std::clamp(y + k, 0, height - 1);
                best = std::max(
                    best,
                    temp[static_cast<std::size_t>(yy) * width + x]
                );
            }
            alpha[static_cast<std::size_t>(y) * width + x] = best;
        }
    }
}

void boxBlurAlphaInPlace(
    std::vector<float> &alpha, int width, int height, int radius
) {
    if (radius <= 0) {
        return;
    }
    const auto window = static_cast<float>(radius * 2 + 1);
    std::vector<float> temp(alpha.size());
    for (int pass = 0; pass < 3; ++pass) {
        for (int y = 0; y < height; ++y) {
            const float *row = alpha.data() + static_cast<std::size_t>(y) * width;
            float *out = temp.data() + static_cast<std::size_t>(y) * width;
            float sum = 0.0f;
            for (int k = -radius; k <= radius; ++k) {
                sum += row[std::clamp(k, 0, width - 1)];
            }
            for (int x = 0; x < width; ++x) {
                out[x] = sum / window;
                const int add = std::clamp(x + radius + 1, 0, width - 1);
                const int sub = std::clamp(x - radius, 0, width - 1);
                sum += row[add] - row[sub];
            }
        }
        for (int x = 0; x < width; ++x) {
            float sum = 0.0f;
            for (int k = -radius; k <= radius; ++k) {
                sum += temp[static_cast<std::size_t>(std::clamp(k, 0, height - 1))
                             * width + x];
            }
            for (int y = 0; y < height; ++y) {
                alpha[static_cast<std::size_t>(y) * width + x]
                    = sum / window;
                const int add = std::clamp(y + radius + 1, 0, height - 1);
                const int sub = std::clamp(y - radius, 0, height - 1);
                sum += temp[static_cast<std::size_t>(add) * width + x]
                    - temp[static_cast<std::size_t>(sub) * width + x];
            }
        }
    }
}

}  // namespace

ScanlineImageComposite scanlineImageCompositePixels(
    const std::uint8_t *frame,
    int bytesPerLine,
    int width,
    int height,
    const ScanlineImageDecor &decor
) {
    ScanlineImageComposite out;
    if (frame == nullptr || width <= 0 || height <= 0 || bytesPerLine <= 0) {
        return out;
    }
    const int sourceStride = bytesPerLine;

    int margin = 0;
    if (decor.glow && decor.glowRadius > 0) {
        margin = std::max(margin, decor.glowRadius * 3);
    }
    if (decor.shadow) {
        margin = std::max(
            {margin,
             static_cast<int>(std::ceil(std::abs(decor.shadowOffsetX) + 1.0f)),
             static_cast<int>(std::ceil(std::abs(decor.shadowOffsetY) + 1.0f))}
        );
    }
    if (decor.stroke2Width > 0) {
        margin = std::max(margin, decor.stroke2Width + 1);
    }
    if (decor.strokeWidth > 0) {
        margin = std::max(margin, decor.strokeWidth + 1);
    }

    const int canvasWidth = width + margin * 2;
    const int canvasHeight = height + margin * 2;
    std::vector<float> base(static_cast<std::size_t>(canvasWidth) * canvasHeight, 0.0f);
    for (int y = 0; y < height; ++y) {
        const std::uint8_t *row = frame
            + static_cast<std::size_t>(y) * sourceStride;
        float *outRow = base.data()
            + static_cast<std::size_t>(y + margin) * canvasWidth + margin;
        for (int x = 0; x < width; ++x) {
            outRow[x] = static_cast<float>(row[x * 4 + 3]);
        }
    }
    out.pixels.assign(
        static_cast<std::size_t>(canvasWidth) * canvasHeight * 4, 0
    );
    out.width = canvasWidth;
    out.height = canvasHeight;
    out.margin = margin;

    // 预乘 source-over：把一层「alpha × 纯色」的剪影按 (shiftX, shiftY)
    // 平移后叠进画布（图层顺序与 Python 一致：发光 → 阴影 → 描边2 →
    // 描边 → 本体）。
    const auto overlayLayer = [&](
        const std::vector<float> &layer,
        const RgbaColor &color,
        int shiftX,
        int shiftY
    ) {
        const float colorAlpha = static_cast<float>(color.alpha) / 255.0f;
        for (int y = 0; y < canvasHeight; ++y) {
            const int ty = y + shiftY;
            if (ty < 0 || ty >= canvasHeight) {
                continue;
            }
            for (int x = 0; x < canvasWidth; ++x) {
                const int tx = x + shiftX;
                if (tx < 0 || tx >= canvasWidth) {
                    continue;
                }
                const float value = std::clamp(
                    layer[static_cast<std::size_t>(y) * canvasWidth + x],
                    0.0f,
                    255.0f
                ) / 255.0f * colorAlpha;
                if (value <= 0.0f) {
                    continue;
                }
                std::uint8_t *dst = out.pixels.data()
                    + (static_cast<std::size_t>(ty) * canvasWidth + tx) * 4;
                const float keep = 1.0f - value;
                dst[0] = static_cast<std::uint8_t>(std::lround(
                    color.blue * value + dst[0] * keep
                ));
                dst[1] = static_cast<std::uint8_t>(std::lround(
                    color.green * value + dst[1] * keep
                ));
                dst[2] = static_cast<std::uint8_t>(std::lround(
                    color.red * value + dst[2] * keep
                ));
                dst[3] = static_cast<std::uint8_t>(std::lround(
                    255.0f * value + dst[3] * keep
                ));
            }
        }
    };

    if (decor.glow && decor.glowRadius > 0) {
        std::vector<float> glow = base;
        boxBlurAlphaInPlace(glow, canvasWidth, canvasHeight, decor.glowRadius);
        overlayLayer(glow, decor.glowColor, 0, 0);
    }
    if (decor.shadow) {
        overlayLayer(
            base,
            decor.shadowColor,
            static_cast<int>(std::lround(decor.shadowOffsetX)),
            static_cast<int>(std::lround(decor.shadowOffsetY))
        );
    }
    if (decor.stroke2Width > 0) {
        std::vector<float> stroke2 = base;
        dilateAlphaInPlace(
            stroke2, canvasWidth, canvasHeight, decor.stroke2Width
        );
        overlayLayer(stroke2, decor.stroke2Color, 0, 0);
    }
    if (decor.strokeWidth > 0) {
        std::vector<float> stroke = base;
        dilateAlphaInPlace(
            stroke, canvasWidth, canvasHeight, decor.strokeWidth
        );
        overlayLayer(stroke, decor.strokeColor, 0, 0);
    }
    // 本体：实色上色 = 按强度混合（100 = 纯目标色剪影；部分强度保留
    // 原色，alpha 始终用素材原值——与 Python _tinted 同公式）。混合在
    // 预乘空间进行：out = src*(1-k) + tintRGB*k*sa。
    if (decor.tint) {
        if (decor.tintStrength >= 100) {
            overlayLayer(base, decor.tintColor, 0, 0);
            return out;
        }
        const float k = static_cast<float>(
            std::clamp(decor.tintStrength, 0, 100)
        ) / 100.0f;
        const float keep = 1.0f - k;
        for (int y = 0; y < height; ++y) {
            const std::uint8_t *row = frame
                + static_cast<std::size_t>(y) * sourceStride;
            std::uint8_t *dstRow = out.pixels.data()
                + (static_cast<std::size_t>(y + margin) * canvasWidth
                   + margin) * 4;
            for (int x = 0; x < width; ++x) {
                const std::uint8_t sa = row[x * 4 + 3];
                if (sa == 0) {
                    continue;
                }
                for (int channel = 0; channel < 3; ++channel) {
                    const float source = row[x * 4 + channel];
                    const float tintChannel = channel == 0
                        ? decor.tintColor.blue
                        : (channel == 1
                               ? decor.tintColor.green
                               : decor.tintColor.red);
                    dstRow[x * 4 + channel] = static_cast<std::uint8_t>(
                        std::lround(
                            source * keep + tintChannel * k
                        )
                    );
                }
                dstRow[x * 4 + 3] = sa;
            }
        }
        return out;
    }
    for (int y = 0; y < height; ++y) {
        const std::uint8_t *row = frame
            + static_cast<std::size_t>(y) * sourceStride;
        std::uint8_t *dstRow = out.pixels.data()
            + (static_cast<std::size_t>(y + margin) * canvasWidth + margin) * 4;
        for (int x = 0; x < width; ++x) {
            const std::uint8_t sa = row[x * 4 + 3];
            if (sa == 0) {
                continue;
            }
            const float keep = 1.0f
                - static_cast<float>(sa) / 255.0f;
            for (int channel = 0; channel < 4; ++channel) {
                dstRow[x * 4 + channel] = static_cast<std::uint8_t>(
                    std::lround(
                        row[x * 4 + channel]
                        + dstRow[x * 4 + channel] * keep
                    )
                );
            }
        }
    }
    return out;
}


}  // namespace krok::subtitle::native::effects
