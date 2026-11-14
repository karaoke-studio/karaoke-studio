#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace krok::subtitle::native {

struct RgbaColor {
    std::uint8_t red = 255;
    std::uint8_t green = 255;
    std::uint8_t blue = 255;
    std::uint8_t alpha = 255;
    bool operator==(const RgbaColor &) const = default;
};

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
    bool operator==(const PaintStyle &) const = default;
};

}  // namespace krok::subtitle::native
