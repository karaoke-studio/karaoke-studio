#pragma once

#include <cstdint>
#include <string>
#include <vector>
#include <array>
#include <optional>
#include <cmath>

namespace krok::subtitle::native {

struct RgbaColor {
    std::uint8_t red = 255;
    std::uint8_t green = 255;
    std::uint8_t blue = 255;
    std::uint8_t alpha = 255;
    std::optional<std::array<float, 4>> linear;
    bool operator==(const RgbaColor &) const = default;
};

inline float linearToSrgb(float value) {
    return value <= .0031308f ? value * 12.92f : 1.055f * std::pow(value, 1.f / 2.4f) - .055f;
}
inline std::array<float, 4> linearColor(const RgbaColor& color) {
    if (color.linear) return *color.linear;
    const auto decode = [](float c) { return c <= .04045f ? c / 12.92f : std::pow((c + .055f) / 1.055f, 2.4f); };
    return {decode(color.red / 255.f), decode(color.green / 255.f), decode(color.blue / 255.f), color.alpha / 255.f};
}
inline std::array<float, 4> encodedColor(const RgbaColor& color) {
    if (!color.linear) return {color.red / 255.f, color.green / 255.f, color.blue / 255.f, color.alpha / 255.f};
    const auto& c = *color.linear;
    return {linearToSrgb(c[0]), linearToSrgb(c[1]), linearToSrgb(c[2]), c[3]};
}

struct PaintStop {
    float position = 0.0f;
    RgbaColor color;
    bool operator==(const PaintStop &) const = default;
};

struct PaintStyle {
    std::string mode = "solid";
    RgbaColor color;
    std::vector<PaintStop> stops;
    std::wstring imagePath;
    float imageScale = 1.0f;
    std::uint64_t imageModifiedMs = 0;
    std::uint64_t imageSize = 0;
    std::optional<float> gradientAngleDeg;
    std::string gradientSpace = "line";
    float radialCenterX = 0.5f;
    float radialCenterY = 0.5f;
    float radialRadiusX = 0.5f;
    float radialRadiusY = 0.5f;
    bool operator==(const PaintStyle &) const = default;
};

inline bool isGradientMode(const std::string& mode) {
    return mode == "gradient_horizontal" || mode == "gradient_vertical"
        || mode == "gradient_radial" || mode == "split_vertical";
}

}  // namespace krok::subtitle::native
