#include "scanline_column.h"
#include "unit_hash.h"

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::effects {
namespace {

struct ColumnUnitRgb {
    float red = 1.0f;
    float green = 1.0f;
    float blue = 1.0f;
};

ColumnUnitRgb columnUnitRgb(const RgbaColor &color) {
    return ColumnUnitRgb{
        static_cast<float>(color.red) / 255.0f,
        static_cast<float>(color.green) / 255.0f,
        static_cast<float>(color.blue) / 255.0f,
    };
}

inline float smoothStep01(float value) {
    const float v = std::clamp(value, 0.0f, 1.0f);
    return v * v * (3.0f - 2.0f * v);
}

void writeColumnPixel(
    std::vector<std::uint8_t> &pixels,
    std::size_t offset,
    float red,
    float green,
    float blue,
    float alpha
) {
    const float safeAlpha = std::clamp(alpha, 0.0f, 1.0f);
    const auto channel = [](float value) {
        return static_cast<std::uint8_t>(std::lround(
            std::clamp(value, 0.0f, 1.0f) * 255.0f
        ));
    };
    // BGRA 预乘（DXGI_FORMAT_B8G8R8A8_UNORM + PREMULTIPLIED）。
    pixels[offset + 0] = channel(blue);
    pixels[offset + 1] = channel(green);
    pixels[offset + 2] = channel(red);
    pixels[offset + 3] = channel(safeAlpha);
}

struct ColumnBubble {
    float centerX = 0.0f;
    float centerY = 0.0f;
    float radiusPx = 1.0f;
    float fade = 0.0f;
};

}  // namespace

std::vector<std::uint8_t> scanlineColumnPixels(
    const std::string &shape,
    const RgbaColor &outer,
    const RgbaColor &inner,
    const RgbaColor &core,
    const PaintStyle *fill,
    int timeMs,
    unsigned width,
    unsigned height,
    float gradientLeft,
    float gradientWidth
) {
    const unsigned rasterWidth = std::max(width, 1u);
    const unsigned rasterHeight = std::max(height, 1u);
    std::vector<std::uint8_t> pixels(
        static_cast<std::size_t>(rasterWidth) * rasterHeight * 4, 0
    );
    // 水泡改为 backends 里逐帧椭圆粒子；这里只负责「烈焰棒」。
    if (shape != "flame") {
        return pixels;
    }
    const ColumnUnitRgb outerRgb = columnUnitRgb(outer);
    const ColumnUnitRgb innerRgb = columnUnitRgb(inner);
    const ColumnUnitRgb coreRgb = columnUnitRgb(core);
    const float t = static_cast<float>(timeMs) / 1000.0f;
    const float widthF = static_cast<float>(rasterWidth);
    const float heightF = static_cast<float>(rasterHeight);
    constexpr float kTwoPi = 6.28318530717958647692f;
    bool hasGradient = false;
    bool gradientHorizontal = false;
    std::vector<std::pair<float, ColumnUnitRgb>> gradientStops;
    if (fill != nullptr
        && (fill->mode == "gradient_horizontal"
            || fill->mode == "gradient_vertical"
            || fill->mode == "split_vertical")
        && fill->stops.size() >= 2) {
        gradientHorizontal = fill->mode == "gradient_horizontal";
        gradientStops.reserve(fill->stops.size());
        for (const PaintStop &stop : fill->stops) {
            gradientStops.emplace_back(
                std::clamp(stop.position, 0.0f, 1.0f),
                columnUnitRgb(stop.color)
            );
        }
        std::sort(
            gradientStops.begin(), gradientStops.end(),
            [](const auto &left, const auto &right) {
                return left.first < right.first;
            }
        );
        hasGradient = gradientStops.size() >= 2;
    }
    const auto gradientColorAt = [&](float coord) {
        coord = std::clamp(coord, 0.0f, 1.0f);
        if (fill->mode == "split_vertical") {
            std::size_t index = 0;
            for (std::size_t i = 0; i < gradientStops.size(); ++i) {
                if (gradientStops[i].first <= coord) {
                    index = i;
                }
            }
            return gradientStops[index].second;
        }
        if (coord <= gradientStops.front().first) {
            return gradientStops.front().second;
        }
        if (coord >= gradientStops.back().first) {
            return gradientStops.back().second;
        }
        for (std::size_t i = 1; i < gradientStops.size(); ++i) {
            if (coord > gradientStops[i].first) {
                continue;
            }
            const auto &left = gradientStops[i - 1];
            const auto &right = gradientStops[i];
            const float span = std::max(right.first - left.first, 1.0e-6f);
            const float mix = (coord - left.first) / span;
            return ColumnUnitRgb{
                left.second.red * (1.0f - mix) + right.second.red * mix,
                left.second.green * (1.0f - mix) + right.second.green * mix,
                left.second.blue * (1.0f - mix) + right.second.blue * mix
            };
        }
        return gradientStops.back().second;
    };
    const auto lightenUnit = [](const ColumnUnitRgb &color, float amount) {
        return ColumnUnitRgb{
            color.red + (1.0f - color.red) * amount,
            color.green + (1.0f - color.green) * amount,
            color.blue + (1.0f - color.blue) * amount
        };
    };

    struct RibbonSpec {
        int edgeLayer;
        int centerLayer;
        float turns;
        float phase;
        float amplitude;
        float half;
        float strength;
    };
    const RibbonSpec specs[4] = {
        {0, 1, 1.85f, 0.00f, 0.46f, 0.180f, 0.92f},
        {0, 1, 2.12f, 0.52f, 0.42f, 0.145f, 0.82f},
        {1, 2, 1.72f, 0.24f, 0.39f, 0.105f, 0.92f},
        {2, 2, 1.55f, 0.55f, 0.32f, 0.048f, 0.70f},
    };
    struct RibbonRow {
        float center = 0.0f;
        float half = 0.01f;
        float front = 0.0f;
        float back = 0.0f;
        float flicker = 1.0f;
        float burn = 1.0f;
    };

    for (unsigned y = 0; y < rasterHeight; ++y) {
        const float v = (static_cast<float>(y) + 0.5f) / heightF;
        const float vertical = smoothStep01((v - 0.03f) / 0.07f)
            * (1.0f - smoothStep01((v - 0.94f) / 0.05f));
        const float breath = 0.5f + 0.5f * std::sin(t * 2.8f);
        const float ribbonScale = 0.96f + 0.08f * breath;
        RibbonRow rows[4];
        for (int i = 0; i < 4; ++i) {
            const RibbonSpec &spec = specs[i];
            const float angle = v * spec.turns * kTwoPi
                + spec.phase * kTwoPi
                + t * (2.0f + 0.60f * spec.turns);
            const float depth = std::cos(angle);
            const float turb = std::sin(v * 13.0f + t * 5.0f + spec.phase * 6.0f)
                * std::sin(v * 7.0f - t * 2.5f + spec.phase * 3.0f);
            rows[i].center = spec.amplitude * ribbonScale * std::sin(angle)
                + 0.02f * turb;
            rows[i].front = std::clamp(depth * 2.6f, 0.0f, 1.0f);
            rows[i].back = std::clamp(-depth * 2.6f, 0.0f, 1.0f);
            rows[i].flicker = 0.88f
                + 0.12f * std::sin(angle * 2.0f - t * 3.1f + spec.phase * 5.0f);
            rows[i].burn = 0.85f
                + 0.15f * std::sin(v * 55.0f - t * 12.0f + spec.phase * 8.0f);
            const float swell = std::sin(
                angle * 2.0f + t * 2.3f + spec.phase * 7.0f
            );
            rows[i].half = std::max(
                spec.half * ribbonScale
                    * (0.45f + 0.55f * swell * swell)
                    * (0.85f + 0.25f * std::abs(turb)),
                0.008f
            );
        }

        const float rodHalf = 0.130f + 0.018f * std::sin(v * 18.0f + t * 6.0f);

        for (unsigned x = 0; x < rasterWidth; ++x) {
            const float u = (static_cast<float>(x) + 0.5f) / widthF
                * 2.0f - 1.0f;
            ColumnUnitRgb outerColor = outerRgb;
            ColumnUnitRgb innerColor = innerRgb;
            ColumnUnitRgb coreColor = coreRgb;
            if (hasGradient) {
                const float coord = gradientHorizontal
                    ? gradientLeft
                        + (u + 1.0f) * 0.5f * gradientWidth
                    : v;
                outerColor = gradientColorAt(coord);
                innerColor = lightenUnit(outerColor, 0.20f);
                coreColor = lightenUnit(outerColor, 0.50f);
            }
            const auto layerColor = [&](int layer) -> const ColumnUnitRgb & {
                return layer == 0 ? outerColor
                    : (layer == 1 ? innerColor : coreColor);
            };
            float rgb[3] = {0.0f, 0.0f, 0.0f};
            float alpha = 0.0f;
            const auto apply = [&](const ColumnUnitRgb &color, float a) {
                const float layer = std::clamp(a, 0.0f, 1.0f);
                rgb[0] = color.red * layer + rgb[0] * (1.0f - layer);
                rgb[1] = color.green * layer + rgb[1] * (1.0f - layer);
                rgb[2] = color.blue * layer + rgb[2] * (1.0f - layer);
                alpha = layer + alpha * (1.0f - layer);
            };

            // 后半圈缎带：先画，随后被柱身遮住。
            for (int i = 0; i < 4; ++i) {
                if (rows[i].back <= 0.0f) {
                    continue;
                }
                const float dx = (u - rows[i].center) / rows[i].half;
                const float band = std::exp(-std::pow(dx, 4.0f));
                const float glow = std::exp(
                    -std::pow(std::abs(dx) / 1.5f, 2.0f)
                ) * 0.12f;
                const float a = std::clamp(
                    (band + glow) * rows[i].back * rows[i].flicker
                        * rows[i].burn * vertical * specs[i].strength * 0.38f,
                    0.0f,
                    1.0f
                );
                const float mix = std::pow(
                    std::clamp(1.0f - std::abs(dx), 0.0f, 1.0f), 1.4f
                );
                const ColumnUnitRgb &edge = layerColor(specs[i].edgeLayer);
                const ColumnUnitRgb &center = layerColor(specs[i].centerLayer);
                const ColumnUnitRgb color{
                    (edge.red * (1.0f - mix)
                        + center.red * mix) * 0.40f
                        + innerColor.red * 0.60f,
                    (edge.green * (1.0f - mix)
                        + center.green * mix) * 0.40f
                        + innerColor.green * 0.60f,
                    (edge.blue * (1.0f - mix)
                        + center.blue * mix) * 0.40f
                        + innerColor.blue * 0.60f
                };
                apply(color, a);
            }

            // 中心高亮棒由 Direct2D 的普通扫字线按字形绘制；
            // 这里补一层无硬边的软实体光晕，给外围火圈体积感。
            const float haloHalf = 0.210f + 0.030f * std::sin(t * 3.1f);
            const float haloCore = std::exp(
                -std::pow(std::abs(u) / haloHalf, 2.2f)
            ) * vertical;
            const float haloWide = std::exp(
                -std::pow(std::abs(u) / (haloHalf * 2.6f), 2.0f)
            ) * vertical;
            const float haloAlpha = std::clamp(
                0.32f * haloCore + 0.14f * haloWide, 0.0f, 0.70f
            );
            const ColumnUnitRgb haloColor{
                innerColor.red * 0.85f + coreColor.red * 0.15f,
                innerColor.green * 0.85f + coreColor.green * 0.15f,
                innerColor.blue * 0.85f + coreColor.blue * 0.15f
            };
            apply(haloColor, haloAlpha);

            // 前半圈缎带：压在柱身上。
            for (int i = 0; i < 4; ++i) {
                if (rows[i].front <= 0.0f) {
                    continue;
                }
                const float dx = (u - rows[i].center) / rows[i].half;
                const float band = std::exp(-std::pow(dx, 4.0f));
                const float glow = std::exp(
                    -std::pow(std::abs(dx) / 1.5f, 2.0f)
                ) * 0.12f;
                const float a = std::clamp(
                    (band + glow) * rows[i].front * rows[i].flicker
                        * rows[i].burn * vertical * specs[i].strength,
                    0.0f,
                    1.0f
                );
                const float mix = std::pow(
                    std::clamp(1.0f - std::abs(dx), 0.0f, 1.0f), 1.4f
                );
                const ColumnUnitRgb &edge = layerColor(specs[i].edgeLayer);
                const ColumnUnitRgb &center = layerColor(specs[i].centerLayer);
                const ColumnUnitRgb color{
                    edge.red * (1.0f - mix) + center.red * mix,
                    edge.green * (1.0f - mix) + center.green * mix,
                    edge.blue * (1.0f - mix) + center.blue * mix
                };
                apply(color, a);
            }

            writeColumnPixel(
                pixels,
                (static_cast<std::size_t>(y) * rasterWidth + x) * 4,
                rgb[0],
                rgb[1],
                rgb[2],
                alpha
            );
        }
    }
    return pixels;
}


}  // namespace krok::subtitle::native::effects
